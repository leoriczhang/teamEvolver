"""OpenViking-backed Memory Change ledger for DreamCycle mutations."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..storage import (
    InMemoryObjectStore,
    OpenVikingSnapshotClient,
    SnapshotError,
    SnapshotNotFoundError,
    build_object_store,
)
from ..storage.memory import is_memory_endpoint
from .config import OpenVikingConfig
from .tools.viking import maintained_space_root

logger = logging.getLogger(__name__)

MEMORY_CHANGE_SCHEMA_V1 = "teamevolver.memory-change.v1"
_LEDGER_PREFIX = "memory-changes"
_REPLAY_PREFIX = "memory-replays"
_MAX_ERROR_CHARS = 1000


@dataclass
class PreparedMemoryChange:
    change_id: str
    run_id: str
    job_name: str
    action: str
    target_paths: list[str]
    source_refs: list[dict[str, str]]
    reason: str
    before_path: str
    after_path: str
    started_at: str
    before_oid: str = ""
    before_hash: str = ""
    before_exists: bool = False
    snapshot_errors: list[dict[str, str]] = field(default_factory=list)


class MemoryChangeLedger:
    """Capture before/after Snapshot refs and persist immutable change records."""

    def __init__(
        self,
        *,
        snapshot_client: OpenVikingSnapshotClient | None,
        object_store: Any,
        maintained_root: str,
        owner_user: str,
        account_hash: str,
        branch: str = "main",
        actor: str = "teamEvolver:dreamcycle",
    ) -> None:
        self._snapshot = snapshot_client
        self._store = object_store
        self._maintained_root = str(maintained_root).rstrip("/")
        self._owner_user = str(owner_user or "")
        self._account_hash = str(account_hash)
        self._branch = str(branch or "main")
        self._actor = str(actor)
        self._lock = threading.RLock()
        self._run_id = ""
        self._job_name = ""
        self._records: list[dict[str, Any]] = []

    @classmethod
    def from_config(cls, config: OpenVikingConfig) -> "MemoryChangeLedger":
        endpoint = str(config.endpoint or "")
        store = (
            build_object_store(
                backend="viking",
                endpoint=endpoint,
                viking_account=config.account or "default",
                viking_user=config.agent_id or "default",
                viking_agent=config.agent or "teamEvolver-dreamcycle",
                viking_api_key=config.api_key,
                viking_root_prefix="team-skill-evolver",
            )
            if endpoint
            else InMemoryObjectStore("unconfigured-dreamcycle-ledger")
        )
        snapshot = (
            None
            if not endpoint or is_memory_endpoint(endpoint)
            else OpenVikingSnapshotClient(
                endpoint=endpoint,
                api_key=config.api_key,
                account=config.account or "default",
                user=config.agent_id or "default",
                agent=config.agent or "teamEvolver-dreamcycle",
            )
        )
        account_hash = hashlib.sha256(
            str(config.account or "default").encode("utf-8")
        ).hexdigest()
        return cls(
            snapshot_client=snapshot,
            object_store=store,
            maintained_root=maintained_space_root(config.customer_id),
            owner_user=config.agent_id,
            account_hash=account_hash,
            actor=f"teamEvolver:{config.agent or 'dreamcycle'}",
        )

    def begin_round(self, run_id: str | None = None) -> str:
        with self._lock:
            self._run_id = str(run_id or self._new_id("dcr"))
            self._job_name = ""
            self._records = []
            return self._run_id

    def begin_job(self, job_name: str) -> int:
        with self._lock:
            if not self._run_id:
                self.begin_round()
            self._job_name = str(job_name or "unknown")
            return len(self._records)

    def summaries_since(self, cursor: int) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._summary(record)
                for record in self._records[max(0, int(cursor)) :]
            ]

    def prepare(
        self,
        *,
        action: str,
        target_paths: Iterable[str],
        source_refs: Iterable[str] = (),
        reason: str = "",
        before_path: str = "",
        after_path: str = "",
    ) -> PreparedMemoryChange:
        with self._lock:
            if not self._run_id:
                self.begin_round()
            run_id = self._run_id
            job_name = self._job_name or "unknown"
        paths = self._normalize_paths(target_paths)
        primary_before = str(before_path or (paths[0] if paths else ""))
        primary_after = str(after_path or primary_before)
        token = PreparedMemoryChange(
            change_id=self._new_id("mch"),
            run_id=run_id,
            job_name=job_name,
            action=str(action or "update"),
            target_paths=paths,
            source_refs=self._safe_source_refs(source_refs),
            reason=str(reason or "").strip(),
            before_path=primary_before,
            after_path=primary_after,
            started_at=self._now(),
        )
        if self._snapshot is None:
            token.snapshot_errors.append(
                {
                    "stage": "before",
                    "code": "SNAPSHOT_UNAVAILABLE",
                    "message": "Snapshot is unavailable for the configured test store",
                }
            )
            return token
        try:
            before_paths = (
                [self._maintained_root]
                if token.action == "create"
                else token.target_paths or [self._maintained_root]
            )
            commit = self._snapshot.commit(
                message=(
                    f"teamEvolver Memory Change {token.change_id} before "
                    f"{job_name}:{token.action}"
                ),
                paths=before_paths,
            )
            token.before_oid = str(commit["commit_oid"])
            (
                token.before_hash,
                token.before_exists,
            ) = self._blob_hash(token.before_oid, token.before_path)
        except SnapshotError as exc:
            token.snapshot_errors.append(self._snapshot_error("before", exc))
        except Exception as exc:  # noqa: BLE001 - ledger must not break a write.
            token.snapshot_errors.append(self._unexpected_error("before", exc))
        return token

    def finish(
        self,
        token: PreparedMemoryChange,
        *,
        result: str,
        error: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        outcome = str(result or "failed")
        after_oid = ""
        after_hash = ""
        after_exists = False
        diff_hash = ""
        change_type = ""
        snapshot_errors = list(token.snapshot_errors)

        if outcome in {"applied", "partial"} and self._snapshot is not None:
            try:
                commit = self._snapshot.commit(
                    message=(
                        f"teamEvolver Memory Change {token.change_id} after "
                        f"{token.job_name}:{token.action}"
                    ),
                    paths=token.target_paths,
                )
                after_oid = str(commit["commit_oid"])
                after_hash, after_exists = self._blob_hash(
                    after_oid,
                    token.after_path,
                )
                if (
                    token.before_oid
                    and token.before_path
                    and token.before_path == token.after_path
                ):
                    diff = self._snapshot.diff(
                        path=token.after_path,
                        from_ref=token.before_oid,
                        to_ref=after_oid,
                        raw=False,
                    )
                    diff_text = str(diff.get("diff_text") or "")
                    diff_hash = hashlib.sha256(
                        diff_text.encode("utf-8")
                    ).hexdigest()
                    change_type = str(diff.get("change_type") or "")
            except SnapshotError as exc:
                snapshot_errors.append(self._snapshot_error("after", exc))
            except Exception as exc:  # noqa: BLE001 - ledger is best effort.
                snapshot_errors.append(self._unexpected_error("after", exc))
        elif outcome == "noop":
            after_oid = token.before_oid
            after_hash = token.before_hash
            after_exists = token.before_exists

        snapshot_status = self._snapshot_status(
            outcome=outcome,
            before_oid=token.before_oid,
            after_oid=after_oid,
            errors=snapshot_errors,
        )
        completed_at = self._now()
        record: dict[str, Any] = {
            "schema_version": MEMORY_CHANGE_SCHEMA_V1,
            "change_id": token.change_id,
            "run_id": token.run_id,
            "job_name": token.job_name,
            "action": token.action,
            "source_refs": token.source_refs,
            "target_paths": token.target_paths,
            "before_path": token.before_path,
            "after_path": token.after_path,
            "account_hash": self._account_hash,
            "branch": self._branch,
            "before_oid": token.before_oid,
            "after_oid": after_oid,
            "before_hash": token.before_hash,
            "after_hash": after_hash,
            "before_exists": token.before_exists,
            "after_exists": after_exists,
            "diff_hash": diff_hash,
            "change_type": change_type,
            "snapshot_status": snapshot_status,
            "snapshot_errors": snapshot_errors,
            "risk_level": "unclassified",
            "policy_reasons": [token.reason] if token.reason else [],
            "decision": (
                "automatic_applied"
                if outcome in {"applied", "partial"}
                else "not_applied"
            ),
            "actor": self._actor,
            "started_at": token.started_at,
            "completed_at": completed_at,
            "result": outcome,
            "error": str(error or "")[:_MAX_ERROR_CHARS],
            "metadata": self._safe_metadata(metadata or {}),
        }
        key = self._record_key(completed_at, token.change_id)
        record["record_key"] = key
        record["ledger_status"] = "persisted"
        try:
            self._persist(key, record)
        except Exception as exc:  # noqa: BLE001 - preserve mutation result.
            logger.exception(
                "[DreamCycle] failed to persist Memory Change %s",
                token.change_id,
            )
            record["ledger_status"] = "failed"
            record["ledger_error"] = (
                f"{type(exc).__name__}: {exc}"
            )[:_MAX_ERROR_CHARS]
        with self._lock:
            self._records.append(record)
        return self._summary(record)

    def list_changes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in self._store.iter_objects(f"{_LEDGER_PREFIX}/"):
            key = str(item.key)
            if not key.endswith(".json"):
                continue
            try:
                value = json.loads(
                    self._store.get_object(key).read().decode("utf-8")
                )
            except (FileNotFoundError, UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
        records.sort(
            key=lambda item: str(item.get("completed_at") or ""),
            reverse=True,
        )
        return records[: max(1, int(limit))]

    def load_change(self, change_id: str) -> dict[str, Any] | None:
        wanted = str(change_id or "").strip()
        if not wanted:
            return None
        for record in self.list_changes(limit=10000):
            if str(record.get("change_id") or "") == wanted:
                return record
        return None

    def read_snapshot_text(self, *, oid: str, path: str) -> str:
        if self._snapshot is None:
            raise RuntimeError("OpenViking Snapshot is unavailable")
        try:
            blob = self._snapshot.show_blob(oid, path=path, raw=False)
        except SnapshotNotFoundError:
            return ""
        return blob.content.decode("utf-8")

    def save_replay(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        replay = dict(payload)
        change_id = str(replay.get("change_id") or "").strip()
        replay_id = str(replay.get("replay_id") or "").strip()
        if not change_id or not replay_id:
            raise ValueError("memory replay requires change_id and replay_id")
        key = f"{_REPLAY_PREFIX}/{change_id}/{replay_id}.json"
        replay["record_key"] = key
        replay["ledger_status"] = "persisted"
        self._persist(key, replay)
        return replay

    def list_replays(
        self,
        *,
        change_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        wanted = str(change_id or "").strip()
        prefix = (
            f"{_REPLAY_PREFIX}/{wanted}/"
            if wanted
            else f"{_REPLAY_PREFIX}/"
        )
        records: list[dict[str, Any]] = []
        for item in self._store.iter_objects(prefix):
            key = str(item.key)
            if not key.endswith(".json"):
                continue
            try:
                value = json.loads(
                    self._store.get_object(key).read().decode("utf-8")
                )
            except (FileNotFoundError, UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
        records.sort(
            key=lambda item: str(item.get("completed_at") or ""),
            reverse=True,
        )
        return records[: max(1, int(limit))]

    def close(self) -> None:
        if self._snapshot is not None:
            self._snapshot.close()

    def _persist(self, key: str, record: Mapping[str, Any]) -> None:
        body = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        batch_write = getattr(self._store, "batch_write", None)
        if callable(batch_write):
            batch_write(
                {key: body},
                preconditions={key: {"kind": "create_if_absent"}},
                wait=True,
                telemetry=True,
            )
            return
        self._store.put_object(key, body)

    def _blob_hash(self, oid: str, path: str) -> tuple[str, bool]:
        if not oid or not path or self._snapshot is None:
            return "", False
        try:
            blob = self._snapshot.show_blob(oid, path=path, raw=False)
        except SnapshotNotFoundError:
            return "", False
        return blob.sha256, True

    def _safe_source_refs(
        self,
        source_refs: Iterable[str],
    ) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        for raw in source_refs:
            value = str(raw or "").strip()
            if not value:
                continue
            item = {
                "ref_hash": hashlib.sha256(value.encode("utf-8")).hexdigest()
            }
            if self._is_maintained_ref(value):
                item["uri"] = value
                item["scope"] = "team_memory"
            else:
                item["scope"] = "opaque_source"
            if item not in output:
                output.append(item)
        return output

    def _is_maintained_ref(self, uri: str) -> bool:
        root = self._maintained_root
        if uri == root or uri.startswith(root + "/"):
            return True
        if not self._owner_user or not root.startswith("viking://user/"):
            return False
        relative = root.removeprefix("viking://user/")
        canonical = f"viking://user/{self._owner_user}/{relative}"
        return uri == canonical or uri.startswith(canonical + "/")

    @staticmethod
    def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "archived_count",
            "failed_count",
            "sanitized_fields",
            "write_status",
        }
        return {
            str(key): value
            for key, value in metadata.items()
            if str(key) in allowed
            and isinstance(value, (str, int, float, bool, type(None)))
        }

    @staticmethod
    def _normalize_paths(paths: Iterable[str]) -> list[str]:
        output: list[str] = []
        for raw in paths:
            value = str(raw or "").strip().rstrip("/")
            if value.startswith("viking://") and value not in output:
                output.append(value)
        return output

    @staticmethod
    def _snapshot_status(
        *,
        outcome: str,
        before_oid: str,
        after_oid: str,
        errors: list[dict[str, str]],
    ) -> str:
        if errors:
            return "partial" if before_oid or after_oid else "failed"
        if outcome in {"applied", "partial"}:
            return "complete" if before_oid and after_oid else "failed"
        return "before_only" if before_oid else "not_captured"

    @staticmethod
    def _snapshot_error(stage: str, exc: SnapshotError) -> dict[str, str]:
        return {
            "stage": stage,
            "code": exc.code,
            "message": str(exc)[:_MAX_ERROR_CHARS],
        }

    @staticmethod
    def _unexpected_error(stage: str, exc: Exception) -> dict[str, str]:
        return {
            "stage": stage,
            "code": type(exc).__name__,
            "message": str(exc)[:_MAX_ERROR_CHARS],
        }

    @staticmethod
    def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "change_id",
                "action",
                "result",
                "snapshot_status",
                "before_oid",
                "after_oid",
                "target_paths",
                "record_key",
                "ledger_status",
            )
        }

    @staticmethod
    def _record_key(completed_at: str, change_id: str) -> str:
        date = completed_at[:10].replace("-", "/")
        return f"{_LEDGER_PREFIX}/{date}/{change_id}.json"

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
