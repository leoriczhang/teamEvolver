"""Private state and helpers for the Agent Context Workspace."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STATE_LOCK = threading.RLock()
_DEFAULT_REF_TTL_SECONDS = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def stable_hash(value: Any) -> str:
    if isinstance(value, (dict, list)):
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _state_dir(config: Any) -> Path:
    users_path = str(getattr(config, "users_registry_path", "") or "").strip()
    if users_path:
        return Path(users_path).expanduser().parent
    config_file = str(getattr(config, "_config_file", "") or "").strip()
    if config_file:
        return Path(config_file).expanduser().parent
    return Path.home() / ".teamEvolver"


class ContextStateStore:
    """Persist opaque refs and session bindings without exposing private URIs."""

    def __init__(self, config: Any) -> None:
        root = _state_dir(config)
        self.path = root / "agent_context_state.json"
        self.audit_path = root / "agent_context_audit.jsonl"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "refs": {}, "sessions": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("refs"), dict):
            data["refs"] = {}
        if not isinstance(data.get("sessions"), dict):
            data["sessions"] = {}
        if not isinstance(data.get("snapshots"), dict):
            data["snapshots"] = {}
        data["schema_version"] = 1
        now = time.time()
        data["refs"] = {
            ref_id: record
            for ref_id, record in data["refs"].items()
            if isinstance(record, dict)
            and float(record.get("expires_at_epoch") or 0) > now
            and not bool(record.get("revoked"))
        }
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)

    def issue_ref(
        self,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        scope: str,
        uri: str,
        kind: str,
        version: str = "",
        ttl_seconds: int = _DEFAULT_REF_TTL_SECONDS,
    ) -> tuple[str, dict[str, Any]]:
        ref_id = "ctx_" + secrets.token_urlsafe(24)
        expires_at = time.time() + max(60, min(3600, int(ttl_seconds)))
        record = {
            "agent_id": str(agent_id),
            "user_id": str(user_id),
            "session_id": str(session_id),
            "scope": str(scope),
            "uri": str(uri),
            "uri_hash": stable_hash(uri),
            "kind": str(kind),
            "version": str(version),
            "created_at": _now(),
            "expires_at_epoch": expires_at,
        }
        with _STATE_LOCK:
            data = self._load()
            data["refs"][ref_id] = record
            self._save(data)
        public = {
            key: value
            for key, value in record.items()
            if key not in {"uri", "expires_at_epoch"}
        }
        public["expires_at"] = datetime.fromtimestamp(
            expires_at,
            timezone.utc,
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        return ref_id, public

    def resolve_ref(
        self,
        ref_id: str,
        *,
        agent_id: str,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        with _STATE_LOCK:
            data = self._load()
            record = data["refs"].get(str(ref_id or ""))
            if not isinstance(record, dict):
                return None
            if str(record.get("agent_id") or "") != str(agent_id or ""):
                return None
            if user_id and str(record.get("user_id") or "") != str(user_id):
                return None
            return dict(record)

    def revoke_ref(self, ref_id: str) -> None:
        with _STATE_LOCK:
            data = self._load()
            data["refs"].pop(str(ref_id or ""), None)
            for snapshot in data["snapshots"].values():
                if not isinstance(snapshot, dict):
                    continue
                snapshot["revoked"] = bool(
                    any(
                        isinstance(item, dict)
                        and item.get("context_ref") == ref_id
                        for item in snapshot.get("items") or []
                    )
                )
            self._save(data)

    def save_snapshot(
        self,
        *,
        snapshot_id: str,
        agent_id: str,
        user_id: str,
        session_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        private_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ref_id = str(item.get("context_ref") or "")
            record = self.resolve_ref(
                ref_id,
                agent_id=agent_id,
                user_id=user_id,
            )
            if record is None:
                continue
            private_items.append(
                {
                    "context_ref": ref_id,
                    "scope": str(record.get("scope") or ""),
                    "kind": str(record.get("kind") or ""),
                    "uri": str(record.get("uri") or ""),
                    "uri_hash": str(record.get("uri_hash") or ""),
                    "version": str(record.get("version") or ""),
                    "title": str(item.get("title") or ""),
                    "l0": str(item.get("l0") or ""),
                    "l1": str(item.get("l1") or ""),
                    "content_hash": str(item.get("content_hash") or ""),
                    "expanded": {
                        level: str(item.get(level) or "")
                        for level in ("l0", "l1")
                        if str(item.get(level) or "").strip()
                    },
                }
            )
        snapshot = {
            "schema_version": "teamevolver.context-snapshot.v1",
            "snapshot_id": snapshot_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": session_id,
            "items": private_items,
            "manifest_hash": stable_hash(
                [
                    {
                        key: item.get(key)
                        for key in (
                            "context_ref",
                            "scope",
                            "kind",
                            "uri_hash",
                            "version",
                            "content_hash",
                        )
                    }
                    for item in private_items
                ]
            ),
            "created_at": _now(),
            "revoked": False,
        }
        with _STATE_LOCK:
            data = self._load()
            data["snapshots"][snapshot_id] = snapshot
            self._save(data)
        return dict(snapshot)

    def record_snapshot_read(
        self,
        *,
        ref_id: str,
        agent_id: str,
        level: str,
        value: Any,
    ) -> None:
        with _STATE_LOCK:
            data = self._load()
            changed = False
            for snapshot in data["snapshots"].values():
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("agent_id") != agent_id
                ):
                    continue
                for item in snapshot.get("items") or []:
                    if (
                        isinstance(item, dict)
                        and item.get("context_ref") == ref_id
                    ):
                        expanded = item.setdefault("expanded", {})
                        expanded[level] = value
                        item["expanded_hash"] = stable_hash(expanded)
                        changed = True
                if changed:
                    snapshot["manifest_hash"] = stable_hash(
                        snapshot.get("items") or []
                    )
            if changed:
                self._save(data)

    def load_snapshot(
        self,
        snapshot_id: str,
        *,
        agent_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any] | None:
        with _STATE_LOCK:
            data = self._load()
            snapshot = data["snapshots"].get(str(snapshot_id or ""))
            if not isinstance(snapshot, dict) or snapshot.get("revoked"):
                return None
            if agent_id and snapshot.get("agent_id") != agent_id:
                return None
            if user_id and snapshot.get("user_id") != user_id:
                return None
            return dict(snapshot)

    def start_session(
        self,
        *,
        agent_id: str,
        user_id: str,
        external_session_id: str,
    ) -> tuple[dict[str, Any], bool]:
        digest = stable_hash(
            {
                "agent_id": agent_id,
                "external_session_id": external_session_id,
            }
        )[:32]
        context_session_id = f"ctxs_{digest}"
        with _STATE_LOCK:
            data = self._load()
            existing = data["sessions"].get(context_session_id)
            if isinstance(existing, dict):
                if (
                    str(existing.get("agent_id") or "") != agent_id
                    or str(existing.get("user_id") or "") != user_id
                ):
                    raise ValueError("context session subject binding conflict")
                return dict(existing), False
            record = {
                "context_session_id": context_session_id,
                "openviking_session_id": f"agent-{digest}",
                "agent_id": agent_id,
                "user_id": user_id,
                "external_session_id_hash": stable_hash(external_session_id),
                "last_sequence": 0,
                "events": {},
                "submitted_usage_keys": [],
                "openviking_created": False,
                "committed": False,
                "created_at": _now(),
                "updated_at": _now(),
            }
            data["sessions"][context_session_id] = record
            self._save(data)
            return dict(record), True

    def mark_openviking_created(
        self,
        context_session_id: str,
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        with _STATE_LOCK:
            data = self._load()
            session = data["sessions"].get(context_session_id)
            if not isinstance(session, dict) or session.get("agent_id") != agent_id:
                raise KeyError("context session not found")
            session["openviking_created"] = True
            session["updated_at"] = _now()
            self._save(data)
            return dict(session)

    def get_session(
        self,
        context_session_id: str,
        *,
        agent_id: str,
    ) -> dict[str, Any] | None:
        with _STATE_LOCK:
            data = self._load()
            record = data["sessions"].get(str(context_session_id or ""))
            if not isinstance(record, dict):
                return None
            if str(record.get("agent_id") or "") != str(agent_id or ""):
                return None
            return dict(record)

    def event_status(
        self,
        context_session_id: str,
        *,
        agent_id: str,
        event_id: str,
        event_hash: str,
        sequence: int,
    ) -> str:
        session = self.get_session(context_session_id, agent_id=agent_id)
        if session is None:
            raise KeyError("context session not found")
        if bool(session.get("committed")):
            raise ValueError("context session is already committed")
        existing = (
            session.get("events", {}).get(event_id)
            if isinstance(session.get("events"), dict)
            else None
        )
        if isinstance(existing, dict):
            if str(existing.get("hash") or "") != event_hash:
                raise ValueError("event id was reused with a different payload")
            return "duplicate"
        expected = int(session.get("last_sequence") or 0) + 1
        if int(sequence) != expected:
            raise ValueError(
                f"context event sequence must be {expected}, got {sequence}"
            )
        return "new"

    def record_event(
        self,
        context_session_id: str,
        *,
        agent_id: str,
        event_id: str,
        event_hash: str,
        sequence: int,
    ) -> dict[str, Any]:
        with _STATE_LOCK:
            data = self._load()
            session = data["sessions"].get(context_session_id)
            if not isinstance(session, dict) or session.get("agent_id") != agent_id:
                raise KeyError("context session not found")
            events = session.setdefault("events", {})
            events[event_id] = {
                "hash": event_hash,
                "sequence": int(sequence),
                "recorded_at": _now(),
            }
            session["last_sequence"] = int(sequence)
            session["updated_at"] = _now()
            self._save(data)
            return dict(session)

    def resolve_session_usage_refs(
        self,
        context_session_id: str,
        *,
        agent_id: str,
        ref_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Resolve explicit usage refs from snapshots bound to one Context Session."""
        requested = list(dict.fromkeys(str(item or "") for item in ref_ids if str(item or "")))
        if len(requested) > 200:
            raise ValueError("at most 200 used_context_refs are allowed")
        if not requested:
            return []
        with _STATE_LOCK:
            data = self._load()
            session = data["sessions"].get(context_session_id)
            if not isinstance(session, dict) or session.get("agent_id") != agent_id:
                raise KeyError("context session not found")
            found: dict[str, dict[str, Any]] = {}
            for snapshot in data["snapshots"].values():
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("agent_id") != agent_id
                    or snapshot.get("session_id") != context_session_id
                    or snapshot.get("user_id") != session.get("user_id")
                    or snapshot.get("revoked")
                ):
                    continue
                for item in snapshot.get("items") or []:
                    ref_id = str((item or {}).get("context_ref") or "")
                    if (
                        ref_id in requested
                        and isinstance(item, dict)
                        and item.get("uri")
                        and item.get("kind") in {"memory", "skill"}
                        and isinstance(item.get("expanded"), dict)
                        and bool(item.get("expanded"))
                    ):
                        found[ref_id] = dict(item)
            missing = [ref_id for ref_id in requested if ref_id not in found]
            if missing:
                raise ValueError(
                    f"used context reference is invalid for this session: {missing[0]}"
                )
            return [found[ref_id] for ref_id in requested]

    def mark_usage_submitted(
        self,
        context_session_id: str,
        *,
        agent_id: str,
        usage_key: str,
    ) -> dict[str, Any]:
        with _STATE_LOCK:
            data = self._load()
            session = data["sessions"].get(context_session_id)
            if not isinstance(session, dict) or session.get("agent_id") != agent_id:
                raise KeyError("context session not found")
            submitted = session.setdefault("submitted_usage_keys", [])
            if usage_key not in submitted:
                submitted.append(usage_key)
                session["updated_at"] = _now()
                self._save(data)
            return dict(session)

    def mark_committed(
        self,
        context_session_id: str,
        *,
        agent_id: str,
        result_hash: str,
    ) -> dict[str, Any]:
        with _STATE_LOCK:
            data = self._load()
            session = data["sessions"].get(context_session_id)
            if not isinstance(session, dict) or session.get("agent_id") != agent_id:
                raise KeyError("context session not found")
            session["committed"] = True
            session["commit_result_hash"] = result_hash
            session["committed_at"] = _now()
            session["updated_at"] = _now()
            self._save(data)
            return dict(session)

    def audit(
        self,
        *,
        action: str,
        agent_id: str,
        user_id: str,
        session_id: str = "",
        scope: str = "",
        uri_hash: str = "",
        result: str = "ok",
    ) -> None:
        entry = {
            "timestamp": _now(),
            "action": str(action),
            "agent_id": str(agent_id),
            "user_id": str(user_id),
            "session_id": str(session_id),
            "scope": str(scope),
            "uri_hash": str(uri_hash),
            "result": str(result),
        }
        with _STATE_LOCK:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.chmod(self.audit_path, 0o600)


def verify_context_usage(
    config: Any,
    *,
    agent_id: str,
    user_id: str,
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace Agent-supplied refs with server-verified, privacy-safe records."""
    store = ContextStateStore(config)
    verified_turns: list[dict[str, Any]] = []
    for turn in turns:
        normalized = dict(turn)
        usage = (
            turn.get("context_usage")
            if isinstance(turn.get("context_usage"), dict)
            else {}
        )
        verified: dict[str, Any] = {
            "context_snapshot_id": str(
                usage.get("context_snapshot_id") or ""
            ),
            "memory_refs": [],
            "skill_refs": [],
            "feedback": (
                dict(usage.get("feedback"))
                if isinstance(usage.get("feedback"), dict)
                else {}
            ),
            "verified": True,
        }
        for source_key, target_key, expected_kind in (
            ("memory_refs", "memory_refs", "memory"),
            ("skill_refs", "skill_refs", "skill"),
        ):
            for item in usage.get(source_key) or []:
                if not isinstance(item, dict):
                    raise ValueError(f"context_usage.{source_key} item must be an object")
                ref_id = str(
                    item.get("context_ref")
                    or item.get("receipt_id")
                    or ""
                )
                record = store.resolve_ref(
                    ref_id,
                    agent_id=agent_id,
                    user_id=user_id,
                )
                if record is None or record.get("kind") != expected_kind:
                    raise ValueError(
                        f"invalid or expired context reference: {ref_id or '<empty>'}"
                    )
                operation = str(item.get("operation") or "retrieved")
                if operation not in {
                    "retrieved",
                    "injected",
                    "read",
                    "selected",
                }:
                    raise ValueError(
                        f"unsupported context usage operation: {operation}"
                    )
                verified_item = {
                    "context_ref": ref_id,
                    "scope": str(record.get("scope") or ""),
                    "uri_hash": str(record.get("uri_hash") or ""),
                    "version": str(record.get("version") or ""),
                    "operation": operation,
                }
                if expected_kind == "skill":
                    uri = str(record.get("uri") or "")
                    marker = "/skills/"
                    skill_name = (
                        uri.split(marker, 1)[1].split("/", 1)[0]
                        if marker in uri
                        else uri.rstrip("/").rsplit("/", 1)[-1]
                    )
                    prefix = (
                        "personal"
                        if record.get("scope") == "personal_skills"
                        else "team"
                    )
                    verified_item["qualified_skill_id"] = (
                        f"{prefix}:{skill_name}"
                    )
                verified[target_key].append(verified_item)
        normalized["context_usage"] = verified
        verified_turns.append(normalized)
    return verified_turns
