"""Tenant/user-bound Context state. Opaque IDs never confer ownership."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..storage.admin_kv import read_kv, write_kv
from ..tenants.registry import current_tenant_id
from .agent_principal import AgentPrincipal

_STATE_LOCK = threading.RLock()
_DEFAULT_REF_TTL_SECONDS = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if isinstance(
        value, (dict, list)
    ) else str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _state_dir(config: Any) -> Path:
    path = str(getattr(config, "users_registry_path", "") or getattr(config, "_config_file", "") or "")
    return Path(path).expanduser().parent if path else Path.home() / ".teamEvolver"


class ContextStateStore:
    """Every state operation checks both tenant and user, including mutations."""

    def __init__(
        self, config: Any, *, tenant_id: str | None = None, legacy_agent_id: str = "",
    ) -> None:
        self._config = config
        self._legacy_agent_id = legacy_agent_id if getattr(config, "agent_protocol_identity_mode", "dual") == "dual" else ""
        self.tenant_id = tenant_id or current_tenant_id()
        root = _state_dir(config)
        if self.tenant_id != "default":
            from ..storage.pg_store import validate_tenant_id

            root = root / "tenants" / validate_tenant_id(self.tenant_id)
        self.path = root / "agent_context_state.json"
        self.audit_path = root / "agent_context_audit.jsonl"

    def _load(self) -> dict[str, Any]:
        # No PG-to-file fallback: an unavailable store cannot change the scope.
        data = read_kv(self._config, "agent_context_state.json", self.path, tenant_id=self.tenant_id)
        for name in ("refs", "sessions", "snapshots"):
            if not isinstance(data.get(name), dict):
                data[name] = {}
        data.setdefault("schema_version", 2)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        write_kv(self._config, "agent_context_state.json", self.path, data, tenant_id=self.tenant_id)

    def _subject(self, principal: AgentPrincipal) -> dict[str, str]:
        if principal.tenant_id != self.tenant_id or not principal.user_id:
            raise ValueError("context tenant binding conflict")
        return {
            "tenant_id": principal.tenant_id, "user_id": principal.user_id,
            **({"agent_id": self._legacy_agent_id} if self._legacy_agent_id else {}),
        }

    def _owns(self, record: Any, principal: AgentPrincipal) -> bool:
        self._subject(principal)
        if not isinstance(record, dict) or record.get("user_id") != principal.user_id:
            return False
        tenant_id = record.get("tenant_id")
        if tenant_id is None:
            # Read-only compatibility. PG row scope or the default file proves
            # tenancy; migration later persists the actual tenant on each record.
            return True  # The explicit store scope proves tenancy across the strict-mode cutover.
        return tenant_id == principal.tenant_id

    def _session(self, data: dict, session_id: str, principal: AgentPrincipal) -> dict:
        session = data["sessions"].get(session_id)
        if not self._owns(session, principal):
            raise KeyError("context session not found")
        return session

    def issue_ref(
        self, *, principal: AgentPrincipal, session_id: str, scope: str, uri: str,
        kind: str, version: str = "", ttl_seconds: int = _DEFAULT_REF_TTL_SECONDS,
    ) -> tuple[str, dict[str, Any]]:
        ref_id = "ctx_" + secrets.token_urlsafe(24)
        expires = time.time() + max(60, min(3600, int(ttl_seconds)))
        record = {
            **self._subject(principal), "session_id": session_id, "scope": scope,
            "uri": uri, "uri_hash": stable_hash(uri), "kind": kind, "version": version,
            "created_at": _now(), "expires_at_epoch": expires,
        }
        with _STATE_LOCK:
            data = self._load()
            if session_id:
                self._session(data, session_id, principal)
            data["refs"][ref_id] = record
            self._save(data)
        public = {key: value for key, value in record.items() if key not in {"uri", "expires_at_epoch"}}
        public["expires_at"] = datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z")
        return ref_id, public

    def resolve_ref(self, ref_id: str, *, principal: AgentPrincipal) -> dict[str, Any] | None:
        with _STATE_LOCK:
            record = self._load()["refs"].get(ref_id)
            if (
                not self._owns(record, principal) or record.get("revoked")
                or float(record.get("expires_at_epoch") or 0) <= time.time()
            ):
                return None
            return copy.deepcopy(record)

    def revoke_ref(self, ref_id: str, *, principal: AgentPrincipal) -> None:
        with _STATE_LOCK:
            data = self._load()
            if not self._owns(data["refs"].get(ref_id), principal):
                raise KeyError("context reference not found")
            data["refs"][ref_id]["revoked"] = True
            for snapshot in data["snapshots"].values():
                if self._owns(snapshot, principal) and any(
                    isinstance(item, dict) and item.get("context_ref") == ref_id
                    for item in snapshot.get("items", [])
                ):
                    snapshot["revoked"] = True
            self._save(data)

    def save_snapshot(
        self, *, snapshot_id: str, principal: AgentPrincipal, session_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with _STATE_LOCK:
            data = self._load()
            if session_id:
                self._session(data, session_id, principal)
            existing = data["snapshots"].get(snapshot_id)
            if existing is not None and not self._owns(existing, principal):
                raise ValueError("context snapshot subject binding conflict")
            private_items = []
            for item in items:
                record = self.resolve_ref(str(item.get("context_ref") or ""), principal=principal)
                if record is None or record.get("session_id", "") != session_id:
                    raise ValueError("invalid snapshot reference")
                private_items.append({
                    **{key: record.get(key, "") for key in ("scope", "kind", "uri", "uri_hash", "version")},
                    **{key: item.get(key, "") for key in ("context_ref", "title", "l0", "l1", "content_hash")},
                    "expanded": {level: str(item[level]) for level in ("l0", "l1") if item.get(level)},
                })
            snapshot = {
                "schema_version": "teamevolver.context-snapshot.v2", "snapshot_id": snapshot_id,
                **self._subject(principal),
                "subject": {"tenant_id": principal.tenant_id, "user_id": principal.user_id},
                "session_id": session_id,
                "items": private_items, "manifest_hash": stable_hash(private_items), "created_at": _now(),
                "revoked": False,
            }
            data["snapshots"][snapshot_id] = snapshot
            self._save(data)
            return copy.deepcopy(snapshot)

    def record_snapshot_read(
        self, *, ref_id: str, principal: AgentPrincipal, level: str, value: Any,
    ) -> None:
        with _STATE_LOCK:
            if self.resolve_ref(ref_id, principal=principal) is None:
                raise KeyError("context reference not found")
            data = self._load()
            for snapshot in data["snapshots"].values():
                if not self._owns(snapshot, principal) or snapshot.get("revoked"):
                    continue
                for item in snapshot.get("items", []):
                    if item.get("context_ref") == ref_id:
                        expanded = item.setdefault("expanded", {})
                        expanded[level] = value
                        item["expanded_hash"] = stable_hash(expanded)
                        snapshot["manifest_hash"] = stable_hash(snapshot["items"])
            self._save(data)

    def load_snapshot(self, snapshot_id: str, *, principal: AgentPrincipal) -> dict[str, Any] | None:
        with _STATE_LOCK:
            snapshot = self._load()["snapshots"].get(snapshot_id)
            if not self._owns(snapshot, principal) or snapshot.get("revoked"):
                return None
            return copy.deepcopy(snapshot)

    def start_session(
        self, *, principal: AgentPrincipal, external_session_id: str,
    ) -> tuple[dict[str, Any], bool]:
        subject = self._subject(principal)
        digest = stable_hash({
            "tenant_id": principal.tenant_id, "user_id": principal.user_id, "external_session_id": external_session_id,
        })[:32]
        context_session_id = f"ctxs_{digest}"
        with _STATE_LOCK:
            data = self._load()
            existing = data["sessions"].get(context_session_id)
            if existing is not None:
                return copy.deepcopy(self._session(data, context_session_id, principal)), False
            # Preserve existing IDs after migration, even in strict mode.
            for session in data["sessions"].values():
                if (
                    self._owns(session, principal)
                    and session.get("external_session_id_hash") == stable_hash(external_session_id)
                ):
                    return copy.deepcopy(session), False
            record = {
                **subject, "context_session_id": context_session_id, "openviking_session_id": f"agent-{digest}",
                "external_session_id_hash": stable_hash(external_session_id), "last_sequence": 0,
                "events": {}, "submitted_usage_keys": [], "openviking_created": False, "committed": False,
                "created_at": _now(), "updated_at": _now(),
            }
            data["sessions"][context_session_id] = record
            self._save(data)
            return copy.deepcopy(record), True

    def get_session(self, context_session_id: str, *, principal: AgentPrincipal) -> dict[str, Any] | None:
        with _STATE_LOCK:
            record = self._load()["sessions"].get(context_session_id)
            return copy.deepcopy(record) if self._owns(record, principal) else None

    def _update_session(self, session_id: str, principal: AgentPrincipal, **values: Any) -> dict:
        with _STATE_LOCK:
            data = self._load()
            session = self._session(data, session_id, principal)
            session.update(values, updated_at=_now())
            self._save(data)
            return copy.deepcopy(session)

    def mark_openviking_created(self, context_session_id: str, *, principal: AgentPrincipal) -> dict:
        return self._update_session(context_session_id, principal, openviking_created=True)

    def event_status(
        self, context_session_id: str, *, principal: AgentPrincipal,
        event_id: str, event_hash: str, sequence: int,
    ) -> str:
        with _STATE_LOCK:
            session = self._session(self._load(), context_session_id, principal)
            if session.get("committed"):
                raise ValueError("context session is already committed")
            existing = session.get("events", {}).get(event_id)
            if existing:
                if existing.get("hash") != event_hash or existing.get("sequence") != sequence:
                    raise ValueError("event id was reused with a different payload")
                return "duplicate"
            expected = int(session.get("last_sequence") or 0) + 1
            if sequence != expected:
                raise ValueError(f"context event sequence must be {expected}, got {sequence}")
            return "new"

    def record_event(
        self, context_session_id: str, *, principal: AgentPrincipal,
        event_id: str, event_hash: str, sequence: int,
    ) -> dict:
        with _STATE_LOCK:
            self.event_status(
                context_session_id, principal=principal, event_id=event_id, event_hash=event_hash, sequence=sequence,
            )
            data = self._load()
            session = self._session(data, context_session_id, principal)
            session.setdefault("events", {})[event_id] = {
                "hash": event_hash, "sequence": sequence, "recorded_at": _now(),
            }
            session.update(last_sequence=sequence, updated_at=_now())
            self._save(data)
            return copy.deepcopy(session)

    def resolve_session_usage_refs(
        self, context_session_id: str, *, principal: AgentPrincipal, ref_ids: list[str],
    ) -> list[dict[str, Any]]:
        requested = list(dict.fromkeys(str(item) for item in ref_ids if item))
        if len(requested) > 200:
            raise ValueError("at most 200 used_context_refs are allowed")
        with _STATE_LOCK:
            data = self._load()
            self._session(data, context_session_id, principal)
            found = {}
            for snapshot in data["snapshots"].values():
                if (
                    not self._owns(snapshot, principal) or snapshot.get("revoked")
                    or snapshot.get("session_id") != context_session_id
                ):
                    continue
                for item in snapshot.get("items", []):
                    if (
                        item.get("context_ref") in requested and item.get("uri")
                        and item.get("kind") in {"memory", "skill"} and item.get("expanded")
                    ):
                        found[item["context_ref"]] = copy.deepcopy(item)
            missing = set(requested) - set(found)
            if missing:
                raise ValueError(f"used context reference is invalid for this session: {sorted(missing)[0]}")
            return [found[ref_id] for ref_id in requested]

    def mark_usage_submitted(
        self, context_session_id: str, *, principal: AgentPrincipal, usage_key: str,
    ) -> dict:
        with _STATE_LOCK:
            data = self._load()
            session = self._session(data, context_session_id, principal)
            submitted = session.setdefault("submitted_usage_keys", [])
            if usage_key not in submitted:
                submitted.append(usage_key)
                session["updated_at"] = _now()
                self._save(data)
            return copy.deepcopy(session)

    def mark_committed(
        self, context_session_id: str, *, principal: AgentPrincipal, result_hash: str,
    ) -> dict:
        return self._update_session(
            context_session_id, principal, committed=True, commit_result_hash=result_hash, committed_at=_now(),
        )

    def audit(
        self, *, action: str, principal: AgentPrincipal, session_id: str = "",
        scope: str = "", uri_hash: str = "", result: str = "ok",
    ) -> None:
        entry = {
            **self._subject(principal), "timestamp": _now(), "action": action,
            "session_id": session_id, "scope": scope, "uri_hash": uri_hash, "result": result,
        }
        with _STATE_LOCK:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def verify_context_usage(
    config: Any, *, principal: AgentPrincipal, turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    store = ContextStateStore(config, tenant_id=principal.tenant_id)
    verified_turns = []
    for turn in turns:
        usage = turn.get("context_usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("context_usage must be an object")
        snapshot_id = str(usage.get("context_snapshot_id") or "")
        snapshot = store.load_snapshot(snapshot_id, principal=principal) if snapshot_id else None
        if snapshot_id and snapshot is None:
            raise ValueError("invalid context snapshot")
        verified = {
            "context_snapshot_id": snapshot_id, "memory_refs": [], "skill_refs": [],
            "feedback": dict(usage.get("feedback") or {}), "verified": True,
        }
        for source_key, kind in (("memory_refs", "memory"), ("skill_refs", "skill")):
            for item in usage.get(source_key) or []:
                if not isinstance(item, dict):
                    raise ValueError(f"context_usage.{source_key} item must be an object")
                ref_id = str(item.get("context_ref") or item.get("receipt_id") or "")
                record = store.resolve_ref(ref_id, principal=principal)
                if record is None or record.get("kind") != kind:
                    raise ValueError(f"invalid or expired context reference: {ref_id or '<empty>'}")
                if snapshot and ref_id not in {ref.get("context_ref") for ref in snapshot.get("items", [])}:
                    raise ValueError("context reference does not belong to snapshot")
                operation = str(item.get("operation") or "retrieved")
                if operation not in {"retrieved", "injected", "read", "selected"}:
                    raise ValueError(f"unsupported context usage operation: {operation}")
                entry = {
                    "context_ref": ref_id, "operation": operation,
                    **{key: record.get(key, "") for key in ("scope", "uri_hash", "version")},
                }
                if kind == "skill":
                    uri = str(record.get("uri") or "")
                    name = uri.split("/skills/", 1)[1].split("/", 1)[0] if "/skills/" in uri else uri.rsplit("/", 1)[-1]
                    prefix = "personal" if record.get("scope") == "personal_skills" else "team"
                    entry["qualified_skill_id"] = f"{prefix}:{name}"
                verified[source_key].append(entry)
        verified_turns.append({**turn, "context_usage": verified})
    return verified_turns
