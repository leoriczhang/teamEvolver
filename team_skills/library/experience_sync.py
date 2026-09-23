"""Incremental OV projection with explicit historical Session import.

Automatic scanning remains per-Skill JSON only; there are no Judge hooks.
PostgreSQL remains authoritative. Statistics are
intentionally excluded from the published document and its content digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from teamEvolver.config import resolve_viking_endpoint
from teamEvolver.logging_runtime import event

LOG = logging.getLogger(__name__)
ROOT = "viking://resources/agent_knowledge_workspace/input/proven_experiences"
PREFIX = "successful_experience_sync/v1/"
SOURCE_PATTERN = r"(^|/)experience_library/[^./][^/]*[.]json$"
LOCK = "successful-experience-sync-v1"


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def segment(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else "sha256-" + digest(value.encode())


class SyncError(Exception):
    def __init__(self, code: str, status: int = 0):
        self.code, self.status = code, status
        super().__init__(code)


def resource_directory(value: str) -> str:
    """Accept a concrete resources directory, never a scope root or traversal."""
    if not isinstance(value, str):
        raise SyncError("INVALID_SYNC_TARGET_DIRECTORY")
    value = value.rstrip("/")
    prefix = "viking://resources/"
    if (not value.startswith(prefix) or len(value) > 1024
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(c in value for c in ("\\", "%", "?", "#"))
            or any(part in {"", ".", ".."} for part in value[len(prefix):].split("/"))):
        raise SyncError("INVALID_SYNC_TARGET_DIRECTORY")
    return value


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    target_directory: str = ROOT
    interval_seconds: int = 30
    batch_size: int = 100
    full_scan_interval_seconds: int = 86400
    import_max_source_mb: int = 64

    @classmethod
    def from_config(cls, config):
        raw = getattr(config, "experience_sync", {}) or {}
        if not isinstance(raw, dict) or not isinstance(raw.get("enabled", False), bool):
            raise SyncError("INVALID_SYNC_CONFIG")
        try:
            maximum = int(raw.get("import_max_source_mb", 64))
            if not 1 <= maximum <= 1024:
                raise ValueError()
            return cls(
                import_max_source_mb=maximum,
                enabled=raw.get("enabled", False),
                target_directory=resource_directory(raw.get("target_directory", ROOT)),
                interval_seconds=max(1, int(raw.get("interval_seconds", 30))),
                batch_size=max(1, min(100, int(raw.get("batch_size", 100)))),
                full_scan_interval_seconds=max(60, int(raw.get("full_scan_interval_seconds", 86400))),
            )
        except (ValueError, TypeError):
            raise SyncError("INVALID_SYNC_CONFIG") from None


@dataclass(frozen=True)
class Target:
    endpoint: str
    account: str
    user: str
    api_key: str = field(repr=False)
    target_directory: str = ROOT

    def __post_init__(self):
        object.__setattr__(self, "target_directory", resource_directory(self.target_directory))

    @classmethod
    def from_config(cls, config):
        if not getattr(config, "storage_pg_enabled", False):
            raise SyncError("PG_REQUIRED")
        if not getattr(config, "sharing_enabled", False):
            raise SyncError("SHARING_DISABLED")
        endpoint = resolve_viking_endpoint(config.sharing_viking_deployment, config.sharing_viking_endpoint).rstrip("/")
        try:
            url = urlsplit(endpoint)
        except ValueError:
            raise SyncError("INVALID_OV_ENDPOINT") from None
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise SyncError("INVALID_OV_ENDPOINT")
        account = str(config.sharing_viking_account or "").strip()
        user = str(config.sharing_viking_user or "").strip()
        key = config.sharing_viking_team_api_key or config.sharing_viking_api_key
        if not account or not user or not key:
            raise SyncError("OV_IDENTITY_OR_KEY_MISSING")
        return cls(endpoint, account, user, key, Settings.from_config(config).target_directory)

    @property
    def identity(self):
        # Key rotation doesn't trigger a backfill; destination/identity changes do.
        return digest(canonical([self.endpoint, self.account, self.user, self.target_directory]))


def documents(source_key: str, body: bytes):
    """Yield stable business documents; reject ambiguous duplicate identities."""
    if not re.search(SOURCE_PATTERN, source_key):
        return
    try:
        source = json.loads(body)
        skill = source["skill_name"]
        entries = source["experiences"]
        if not isinstance(skill, str) or not skill.strip() or not isinstance(entries, list):
            raise ValueError()
        if len(entries) > 1000:
            raise ValueError()
        seen = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("kind") != "exemplary":
                continue
            description = entry.get("description")
            key = entry.get("experience_key")
            if not isinstance(description, str) or not description.strip() or not isinstance(key, str) or not key:
                raise ValueError()
            identity = entry.get("id") or digest(canonical([skill, "exemplary", key]))
            if not isinstance(identity, str):
                raise ValueError()
            doc = {
                "schema_version": 1, "skill_name": skill, "experience_id": identity,
                "experience_key": key, "kind": "exemplary", "source_key": source_key,
                "description": description.replace("\r\n", "\n").replace("\r", "\n").strip(),
            }
            path = f"{digest(source_key.encode())}/{segment(identity)}.json"
            if path in seen and seen[path] != doc:
                raise ValueError()
            seen[path] = doc
        yield from seen.items()
    except (ValueError, TypeError, KeyError):
        raise SyncError("INVALID_EXPERIENCE_DOCUMENT") from None


class OVWriter:
    """Strict native HTTP adapter; no write-on-error fallback or append."""

    def __init__(self, target: Target, client=None):
        self.target = target
        self.directories = set()
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.headers = {"X-API-Key": target.api_key, "X-OpenViking-Account": target.account,
                        "X-OpenViking-User": target.user, "X-OpenViking-Agent": "team-skill-evolver"}

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        if getattr(self, "guard", None):
            self.guard()
        started = time.monotonic()
        response = None
        code = None
        stage = {"mkdir": "mkdir", "stat": "stat", "write": "write", "download": "readback"}.get(
            path.rsplit("/", 1)[-1], "request")
        uri = (kwargs.get("json") or kwargs.get("params") or {}).get("uri")
        try:
            response = self.client.request(method, self.target.endpoint + path, headers=self.headers, **kwargs)
            if response.status_code >= 400:
                try:
                    code = response.json().get("error", {}).get("code", "")
                except (ValueError, AttributeError):
                    code = ""
                allowed = {"NOT_FOUND", "RESOURCE_NOT_FOUND", "ALREADY_EXISTS", "CONFLICT",
                           "FORBIDDEN", "UNAUTHENTICATED", "DEADLINE_EXCEEDED"}
                code = code if isinstance(code, str) and code in allowed else f"OV_HTTP_{response.status_code}"
                raise SyncError(code, response.status_code)
            if not 200 <= response.status_code < 300:
                code = "OV_UNEXPECTED_RESPONSE"
                raise SyncError(code, response.status_code)
            return response
        except httpx.TimeoutException:
            code = "OV_TIMEOUT"
            raise SyncError(code) from None
        except httpx.RequestError:
            code = "OV_NETWORK_ERROR"
            raise SyncError(code) from None
        finally:
            self.last_operation = {
                "stage": stage, "host": urlsplit(self.target.endpoint).hostname, "path": path,
                "method": method, "uri": uri, "status": response.status_code if response is not None else None,
                "ov_request_id": response.headers.get("x-request-id", "")[:128] if response is not None else None,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
            event(LOG, "experience_sync.ov_request", logging.WARNING if code else logging.DEBUG,
                  **self.last_operation, code=code, **getattr(self, "diagnostic_context", {}))

    def require_type(self, uri, *, directory):
        response = self.request("GET", "/api/v1/fs/stat", params={"uri": uri})
        try:
            data = response.json()
            result = data["result"]
            if data.get("status") != "ok" or result.get("uri") != uri or type(result.get("isDir")) is not bool:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SyncError("OV_INVALID_STAT") from None
        if result["isDir"] != directory:
            raise SyncError("OV_PATH_TYPE_CONFLICT", 409)

    def matches(self, uri, content_digest):
        try:
            response = self.request("GET", "/api/v1/content/download", params={"uri": uri})
        except SyncError as exc:
            if exc.status == 404 and exc.code in {"NOT_FOUND", "RESOURCE_NOT_FOUND"}:
                return False
            raise
        try:
            return digest(canonical(response.json())) == content_digest
        except ValueError:
            return False

    def write(self, uri, content):
        relative = uri.removeprefix("viking://resources/")
        for index in range(1, len(relative.split("/"))):
            parent = "viking://resources/" + "/".join(relative.split("/")[:index])
            if parent in self.directories:
                continue
            try:
                self.request("POST", "/api/v1/fs/mkdir", json={"uri": parent})
            except SyncError as exc:
                if exc.status != 409 or exc.code not in {"ALREADY_EXISTS", "CONFLICT"}:
                    raise
                self.require_type(parent, directory=True)
            self.directories.add(parent)
        payload = {"uri": uri, "content": content.decode(), "mode": "replace", "wait": True, "timeout": 25}
        try:
            response = self.request("POST", "/api/v1/content/write", json=payload)
        except SyncError as exc:
            if exc.status != 404 or exc.code not in {"NOT_FOUND", "RESOURCE_NOT_FOUND"}:
                raise
            payload["mode"] = "create"
            try:
                response = self.request("POST", "/api/v1/content/write", json=payload)
            except SyncError as race:
                if race.status != 409 or race.code not in {"ALREADY_EXISTS", "CONFLICT"}:
                    raise
                self.require_type(uri, directory=False)
                payload["mode"] = "replace"
                response = self.request("POST", "/api/v1/content/write", json=payload)
        try:
            data = response.json()
            result = data.get("result") or {}
            if data.get("status") != "ok" or result.get("uri") != uri or result.get("content_updated") is not True:
                raise ValueError()
            statuses = {"complete", "failed", "queued", "skipped"}
            return {"content_written": True, **{
                name: result.get(name) if isinstance(result.get(name), str) and result[name] in statuses else "unknown"
                for name in ("semantic_status", "vector_status")
            }}
        except (ValueError, AttributeError):
            raise SyncError("OV_UNCONFIRMED_WRITE") from None


class ExperienceSync:
    """One bounded tenant pass; all entrypoints execute off the event loop."""

    def __init__(self, store, target, settings, writer, *, stop=None, clock=time.time):
        self.store, self.target, self.settings, self.writer = store, target, settings, writer
        self.stop = stop or threading.Event()
        self.clock = clock
        self.base = PREFIX + target.identity + "/"
        self.meta_key = self.base + "scan.json"
        self.items = self.base + "items/"

    def load(self, key, default=None):
        try:
            return json.loads(self.store.get_object(key).read())
        except FileNotFoundError:
            return {} if default is None else default

    def save(self, key, value):
        self.store.check_background_lock(LOCK)
        self.store.put_object(key, canonical(value))

    def request_run(self, operation="sync"):
        """Persist a bounded reconciliation intent, without making any OV calls."""
        if not self.settings.enabled:
            raise SyncError("SYNC_DISABLED", 409)
        if operation not in {"sync", "import_all"}:
            raise SyncError("INVALID_SYNC_OPERATION", 400)
        if self.stop.is_set():
            raise SyncError("SYNC_STOPPING", 503)
        if not self.store.try_background_lock(LOCK):
            raise SyncError("SYNC_ALREADY_RUNNING", 409)
        try:
            meta = self.load(self.meta_key)
            manual = meta.get("manual", {})
            if manual.get("state") in {"queued", "running"}:
                if manual.get("operation", "sync") != operation:
                    raise SyncError("SYNC_OTHER_OPERATION_RUNNING", 409)
                return manual
            manual = {"request_id": str(uuid4()), "state": "queued", "requested_at": self.clock(),
                      "operation": operation}
            if operation == "import_all":
                manual["import"] = {"phase": "objects", "until": self.store.database_time(),
                                    "processed_sources": 0, "eligible_records": 0, "prepared_documents": 0,
                                    "rejected_sources": 0, "last_error": None}
            meta.update(manual=manual, delivery_cursor="", scan={
                "after_time": "-infinity", "after_key": "", "until": self.store.database_time(), "full": True,
            })
            self.save(self.meta_key, meta)
            return manual
        finally:
            self.store.release_background_lock(LOCK)

    def discover(self, row, *, verify=False):
        if row["content"] is None:
            raise SyncError("SOURCE_TOO_LARGE")
        for path, doc in documents(row["key"], row["content"]):
            if self.stop.is_set():
                raise SyncError("SYNC_STOPPING")
            self.enqueue(path, doc, verify=verify)

    def enqueue(self, path, doc, *, verify=False):
        key = self.items + path
        old = self.load(key)
        desired = digest(canonical(doc))
        if old.get("desired_digest") == desired:
            if verify and old.get("state") == "synced":
                old.update(state="pending", verify_only=True, next_attempt=0)
                self.save(key, old)
            return False
        value = {"document": doc, "desired_digest": desired, "synced_digest": old.get("synced_digest"),
                 "endpoint": self.target.endpoint, "account": self.target.account,
                 "uri": f"{self.target.target_directory}/{path}",
                 "state": "pending", "attempts": 0, "next_attempt": 0, "last_error": None,
                 "content_written": False, "semantic_status": "unknown", "vector_status": "unknown"}
        self.save(key, value)
        return True

    def migrate_destination(self, key, value):
        """Move legacy tenant-prefixed delivery state without deleting remote files."""
        path = key.removeprefix(self.items)
        legacy = f"{self.target.target_directory}/{segment(self.store.tenant_id)}/{path}"
        if value.get("uri") != legacy:
            return value
        previous = {name: value.get(name) for name in (
            "uri", "state", "attempts", "next_attempt", "synced_digest", "last_error", "last_attempt_at",
        )}
        moved = {**value, "uri": f"{self.target.target_directory}/{path}", "previous_destination": previous,
                 "state": "pending", "attempts": 0, "next_attempt": 0, "last_error": None,
                 "last_failure": None, "last_attempt_at": None, "synced_digest": None,
                 "content_written": False, "semantic_status": "unknown", "vector_status": "unknown",
                 "verify_only": True}
        self.store.check_background_lock(LOCK)
        self.store.batch_write({key: canonical(moved)}, preconditions={key: {
            "kind": "replace_if_hash", "base_hash": "sha256:" + digest(canonical(value)),
        }})
        event(LOG, "experience_sync.destination_migrated", tenant=self.store.tenant_id,
              previous_uri=legacy, uri=moved["uri"], experience_id=value["document"]["experience_id"])
        return moved

    def scan(self, meta):
        scan = meta.get("scan")
        if not scan:
            full = self.clock() - meta.get("last_full_scan", 0) >= self.settings.full_scan_interval_seconds
            after = "-infinity"
            if not full and meta.get("watermark"):
                after = (datetime.fromisoformat(meta["watermark"]) - timedelta(seconds=120)).isoformat()
            scan = {"after_time": after, "after_key": "", "until": self.store.database_time(), "full": full}
            meta["scan"] = scan
            self.save(self.meta_key, meta)
        rows = self.store.changed_objects_page(pattern=SOURCE_PATTERN, after_time=scan["after_time"],
                                               after_key=scan["after_key"], until=scan["until"],
                                               limit=self.settings.batch_size)
        for row in rows:
            if self.stop.is_set():
                return True
            try:
                self.discover(row, verify=scan["full"])
                if meta.get("last_error_source") == row["key"]:
                    meta.pop("last_error", None)
                    meta.pop("last_error_source", None)
                    meta.pop("last_error_at", None)
            except SyncError as exc:
                if exc.code == "SYNC_STOPPING":
                    return True
                meta["last_error"] = exc.code
                meta["last_error_source"] = row["key"]
                meta["last_error_at"] = self.clock()
                event(LOG, "experience_sync.source_rejected", logging.WARNING,
                      tenant=self.store.tenant_id, source_key=row["key"], code=exc.code)
            # All desired records are durable before acknowledging this source.
            scan.update(after_time=row["updated_at"], after_key=row["key"])
            self.save(self.meta_key, meta)
        if len(rows) < self.settings.batch_size:
            meta["watermark"] = scan["until"]
            meta["last_scan"] = self.clock()
            if scan["full"]:
                meta["last_full_scan"] = self.clock()
            meta.pop("scan", None)
            self.save(self.meta_key, meta)
            return False
        return True

    def deliver(self, key, value):
        started = time.monotonic()
        expected = digest(canonical(value))
        self.store.check_background_lock(LOCK)
        value = dict(value)
        value["last_attempt_at"] = self.clock()
        self.writer.last_operation = None
        self.writer.diagnostic_context = {"tenant": self.store.tenant_id,
                                          "experience_id": value["document"]["experience_id"]}
        event(LOG, "experience_sync.delivery_started", tenant=self.store.tenant_id,
              source_key=value["document"]["source_key"], experience_id=value["document"]["experience_id"],
              uri=value["uri"], attempt=value.get("attempts", 0) + 1)
        try:
            verified = False
            if value.get("attempts", 0) or value.get("verify_only"):
                verified = self.writer.matches(value["uri"], value["desired_digest"])
                value["content_written"] = verified
            # Even a matching file alone does not prove its indexes are ready.
            if not (verified and value.get("verify_only")
                    and value["semantic_status"] == "complete" and value["vector_status"] == "complete"):
                value.update(self.writer.write(value["uri"], canonical(value["document"])))
            if value["semantic_status"] != "complete" or value["vector_status"] != "complete":
                event(LOG, "experience_sync.index_pending", logging.WARNING, tenant=self.store.tenant_id,
                      uri=value["uri"], stage="index", semantic_status=value["semantic_status"],
                      vector_status=value["vector_status"])
                raise SyncError("OV_INDEX_NOT_READY")
            if not self.writer.matches(value["uri"], value["desired_digest"]):
                raise SyncError("OV_CONTENT_MISMATCH")
            value.update(state="synced", synced_digest=value["desired_digest"], last_error=None, next_attempt=0,
                         synced_at=self.clock(), verify_only=False, attempts=0, last_failure=None)
        except SyncError as exc:
            attempts = value.get("attempts", 0) + 1
            failure = dict(getattr(self.writer, "last_operation", None) or {})
            if exc.code == "OV_INDEX_NOT_READY":
                failure["stage"] = "index"
            failure.update(code=exc.code, at=self.clock())
            if failure.get("stage") == "write" and exc.code in {
                "OV_TIMEOUT", "OV_NETWORK_ERROR", "DEADLINE_EXCEEDED", "OV_HTTP_504", "OV_UNCONFIRMED_WRITE",
            }:
                # A write timeout can follow durable content persistence. Verify
                # the exact URI without inferring semantic/vector completion.
                try:
                    value["content_written"] = self.writer.matches(value["uri"], value["desired_digest"])
                    failure["content_confirmed"] = value["content_written"]
                except SyncError as reconcile:
                    failure["reconciliation_error"] = reconcile.code
                value.update(semantic_status="unknown", vector_status="unknown")
                event(LOG, "experience_sync.write_reconciled", tenant=self.store.tenant_id,
                      uri=value["uri"], content_confirmed=failure.get("content_confirmed"),
                      code=exc.code, reconciliation_error=failure.get("reconciliation_error"),
                      semantic_status="unknown", vector_status="unknown")
            value.update(state="retry", attempts=attempts, last_error=exc.code, last_failure=failure,
                         next_attempt=self.clock() + min(900, 5 * 2 ** min(attempts - 1, 8)))
        self.store.check_background_lock(LOCK)
        # Check exactly the state sent, inside the PG transaction. A superseded
        # delivery cannot acknowledge a newer desired document.
        self.store.batch_write({key: canonical(value)}, preconditions={key: {
            "kind": "replace_if_hash", "base_hash": "sha256:" + expected,
        }})
        event(LOG, "experience_sync.delivery", logging.INFO if value["state"] == "synced" else logging.WARNING,
              tenant=self.store.tenant_id, source_key=value["document"]["source_key"],
              experience_id=value["document"]["experience_id"], content_digest=value["desired_digest"],
              uri=value["uri"], state=value["state"], code=value["last_error"],
              attempts=value["attempts"], next_attempt_at=value["next_attempt"] or None,
              failure_stage=(value.get("last_failure") or {}).get("stage"),
              duration_ms=round((time.monotonic() - started) * 1000, 1))

    def delivery_pass(self, meta, summary):
        """Persist a bounded page heartbeat; summarize unchanged waiting once/minute."""
        summary["finished_at"] = self.clock()
        previous = meta.get("last_delivery_pass", {})
        meta["last_delivery_pass"] = summary
        changed = any(summary.get(k) != previous.get(k) for k in ("attempted", "deferred", "examined"))
        if summary["attempted"] or (summary["deferred"] and (
            changed or self.clock() - meta.get("delivery_reported_at", 0) >= 60
        )):
            event(LOG, "experience_sync.delivery_pass", tenant=self.store.tenant_id,
                  sync_request_id=meta.get("manual", {}).get("request_id"), **summary)
            meta["delivery_reported_at"] = self.clock()
        self.save(self.meta_key, meta)

    def run_once(self):
        self.outcome = "idle"
        if not self.settings.enabled or self.stop.is_set():
            return False
        if not self.store.try_background_lock(LOCK):
            self.outcome = "lock_busy"
            return False
        try:
            meta = self.load(self.meta_key)
            manual = meta.get("manual", {})
            active = manual.get("state") in {"queued", "running"}
            if active and manual["state"] == "queued":
                manual.update(state="running", started_at=self.clock())
                self.save(self.meta_key, meta)
                event(LOG, "experience_sync.manual_started", tenant=self.store.tenant_id,
                      sync_request_id=manual["request_id"])
            if active and manual.get("operation") == "import_all":
                from team_skills.library.experience_import import advance_import

                if advance_import(self, meta):
                    return True
            more = False
            if not (active and manual.get("scan_complete")):
                more = self.scan(meta)
                if active and not more:
                    manual["scan_complete"] = True
                    # Discoveries may sort before an earlier delivery cursor.
                    # Finish with one complete sweep after discovery is done.
                    meta["delivery_cursor"] = ""
                    self.save(self.meta_key, meta)
            rows = self.store.object_page(prefix=self.items, after_key=meta.get("delivery_cursor", ""),
                                          limit=self.settings.batch_size)
            deadline = time.monotonic() + 30
            summary = {"started_at": self.clock(), "examined": 0, "attempted": 0, "deferred": 0,
                       "next_attempt_at": None}
            for row in rows:
                if self.stop.is_set() or time.monotonic() >= deadline:
                    self.delivery_pass(meta, summary)
                    return True
                value = self.migrate_destination(row["key"], json.loads(row["content"]))
                summary["examined"] += 1
                if value["state"] != "synced":
                    if value["next_attempt"] <= self.clock():
                        self.deliver(row["key"], value)
                        summary["attempted"] += 1
                    else:
                        summary["deferred"] += 1
                        summary["next_attempt_at"] = min(summary["next_attempt_at"] or value["next_attempt"],
                                                         value["next_attempt"])
                meta["delivery_cursor"] = row["key"]
                self.save(self.meta_key, meta)
            if len(rows) < self.settings.batch_size:
                meta["delivery_cursor"] = ""
                if active and manual.get("scan_complete"):
                    manual.update(state="completed", finished_at=self.clock())
                    event(LOG, "experience_sync.manual_completed", tenant=self.store.tenant_id,
                          sync_request_id=manual["request_id"], attempted_in_last_page=summary["attempted"],
                          deferred_in_last_page=summary["deferred"], next_attempt_at=summary["next_attempt_at"])
                self.save(self.meta_key, meta)
            self.delivery_pass(meta, summary)
            return more or len(rows) == self.settings.batch_size
        finally:
            self.store.release_background_lock(LOCK)

    def status(self):
        meta = self.load(self.meta_key)
        counts = {"pending": 0, "synced": 0, "retry": 0}
        last_error = meta.get("last_error")
        last_error_at = meta.get("last_error_at", 0)
        index_pending = 0
        retry = {"due": 0, "deferred": 0, "next_attempt_at": None, "last_attempt_at": None,
                 "error_counts": {}, "samples": []}
        now = self.clock()
        cursor = ""
        while True:
            rows = self.store.object_page(prefix=self.items, after_key=cursor, limit=100)
            for row in rows:
                value = json.loads(row["content"])
                counts[value["state"]] += 1
                if value["state"] == "retry":
                    next_attempt = value.get("next_attempt", 0)
                    retry["due" if next_attempt <= now else "deferred"] += 1
                    retry["next_attempt_at"] = (next_attempt if retry["next_attempt_at"] is None else
                                                min(retry["next_attempt_at"], next_attempt))
                    retry["last_attempt_at"] = max(
                        retry["last_attempt_at"] or 0, (value.get("last_attempt_at") or 0)) or None
                    code = value.get("last_error") or "UNKNOWN"
                    retry["error_counts"][code] = retry["error_counts"].get(code, 0) + 1
                    if len(retry["samples"]) < 5:
                        retry["samples"].append({"uri": value["uri"], "source_key": value["document"]["source_key"],
                            "experience_id": value["document"]["experience_id"], "code": code,
                            "attempts": value.get("attempts", 0), "last_attempt_at": value.get("last_attempt_at"),
                            "next_attempt_at": next_attempt, "failure": value.get("last_failure")})
                if value.get("content_written") and (value.get("semantic_status") != "complete"
                                                    or value.get("vector_status") != "complete"):
                    index_pending += 1
                if value.get("last_error") and (value.get("last_attempt_at") or 0) >= last_error_at:
                    last_error = value["last_error"]
                    last_error_at = value.get("last_attempt_at", 0)
                cursor = row["key"]
            if len(rows) < 100:
                break
        return {"enabled": self.settings.enabled,
                "target_directory": self.target.target_directory,
                "account": self.target.account, "last_scan": meta.get("last_scan"),
                "last_full_scan": meta.get("last_full_scan"), "counts": counts, "last_error": last_error,
                "index_pending": index_pending, "retry": retry, "server_time": now,
                "last_error_at": last_error_at or None, "last_delivery_pass": meta.get("last_delivery_pass"),
                "manual": meta.get("manual")}
