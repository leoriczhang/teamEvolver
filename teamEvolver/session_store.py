"""Session queue, archive, and filter-audit storage helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .skills.hub import SkillHub
from .storage import is_not_found_error


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _load_json(bucket, key: str) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(bucket.get_object(key).read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        if is_not_found_error(exc):
            return None
        raise
    return data if isinstance(data, dict) else None


def _safe_list(bucket, prefix: str) -> list[str]:
    try:
        return sorted(obj.key for obj in bucket.iter_objects(prefix=prefix) if obj.key.endswith(".json"))
    except Exception:
        return []


def _first_user_text(session: dict[str, Any]) -> str:
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("prompt_text") or turn.get("instruction") or "").strip()
        if text:
            return text
    for message in session.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            text = str(message.get("content") or "").strip()
            if text:
                return text
    return ""


def _session_title(session: dict[str, Any]) -> str:
    title = str(session.get("title") or "").strip()
    if title:
        return title[:120]
    first = _first_user_text(session)
    return first[:80] if first else "(untitled session)"


def _num_turns(session: dict[str, Any]) -> int:
    turns = session.get("turns")
    if isinstance(turns, list):
        return len(turns)
    metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
    try:
        return int(metrics.get("interaction_turns") or 0)
    except (TypeError, ValueError):
        return 0


def _session_meta(session: dict[str, Any], *, status: str) -> dict[str, Any]:
    metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
    ingested_at = str(session.get("ingested_at") or "")
    timestamp = str(session.get("timestamp") or session.get("started_at") or ingested_at or "")
    return {
        "session_id": str(session.get("session_id") or ""),
        "title": _session_title(session),
        "user_alias": str(session.get("user_alias") or "anonymous"),
        "status": status,
        "num_turns": _num_turns(session),
        "timestamp": timestamp,
        "ingested_at": ingested_at,
        "tool_call_count": metrics.get("tool_call_count", 0),
        "total_tokens": metrics.get("total_tokens", 0),
        "value_judge": session.get("value_judge") if isinstance(session.get("value_judge"), dict) else {},
    }


class SessionStore:
    """Object-store backed session lifecycle manager."""

    def __init__(self, bucket, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")

    @classmethod
    def from_config(cls, config) -> "SessionStore":
        hub = SkillHub.object_storage_from_config(config)
        if hub is None:
            raise ValueError("session storage is not configured")
        return cls(hub._bucket, hub.session_prefix())

    def _key(self, rel: str) -> str:
        return f"{self._prefix}{rel}"

    def queue_key(self, session_id: str) -> str:
        return self._key(f"sessions/{session_id}.json")

    def archive_key(self, session_id: str) -> str:
        return self._key(f"session_archive/{session_id}.json")

    def filter_audit_key(self, session_id: str) -> str:
        return self._key(f"session_filter_audit/{session_id}.json")

    def save_queued(self, session: dict[str, Any]) -> str:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        queued = dict(session)
        queued["status"] = "queued"
        queued.setdefault("ingested_at", utc_now_iso())
        self._bucket.put_object(self.queue_key(session_id), _json_bytes(queued))
        self._bucket.put_object(self.archive_key(session_id), _json_bytes(queued))
        self.save_filter_audit(queued, status="queued")
        return self.queue_key(session_id)

    def save_skipped(self, session: dict[str, Any]) -> None:
        skipped = dict(session)
        skipped["status"] = "skipped"
        skipped.setdefault("ingested_at", utc_now_iso())
        self.save_filter_audit(skipped, status="skipped")

    def save_filter_audit(self, session: dict[str, Any], *, status: str) -> None:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        payload = {
            **_session_meta(session, status=status),
            "recorded_at": utc_now_iso(),
        }
        self._bucket.put_object(self.filter_audit_key(session_id), _json_bytes(payload))

    def _queue_keys(self) -> set[str]:
        return set(_safe_list(self._bucket, self._key("sessions/")))

    def list_queue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key in _safe_list(self._bucket, self._key("sessions/")):
            session = _load_json(self._bucket, key)
            if not session:
                continue
            rows.append({**_session_meta(session, status="queued"), "key": key})
        rows.sort(key=lambda item: str(item.get("ingested_at") or item.get("timestamp") or ""), reverse=True)
        return rows[: max(0, int(limit))]

    def list_conversations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        queue_keys = self._queue_keys()
        rows: list[dict[str, Any]] = []
        for key in _safe_list(self._bucket, self._key("session_archive/")):
            session = _load_json(self._bucket, key)
            if not session:
                continue
            session_id = str(session.get("session_id") or os.path.basename(key)[:-5])
            status = str(session.get("status") or "queued")
            if status == "queued" and self.queue_key(session_id) not in queue_keys:
                status = "consumed"
            rows.append({**_session_meta(session, status=status), "key": key})
        rows.sort(key=lambda item: str(item.get("ingested_at") or item.get("timestamp") or ""), reverse=True)
        return rows[: max(0, int(limit))]

    def load_session(self, session_id: str) -> Optional[dict[str, Any]]:
        session = _load_json(self._bucket, self.archive_key(session_id))
        if session is not None:
            if str(session.get("status") or "") == "queued":
                try:
                    self._bucket.get_object(self.queue_key(session_id))
                except Exception as exc:  # noqa: BLE001
                    if is_not_found_error(exc):
                        session = dict(session)
                        session["status"] = "consumed"
                    else:
                        raise
            return session
        return _load_json(self._bucket, self.queue_key(session_id))

    def list_filter_audit(self, *, limit: int = 100, decision: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        wanted = str(decision or "").strip().lower()
        for key in _safe_list(self._bucket, self._key("session_filter_audit/")):
            item = _load_json(self._bucket, key)
            if not item:
                continue
            value_judge = item.get("value_judge") if isinstance(item.get("value_judge"), dict) else {}
            if wanted and str(value_judge.get("decision") or "").lower() != wanted:
                continue
            rows.append({**item, "key": key})
        rows.sort(key=lambda item: str(item.get("recorded_at") or item.get("ingested_at") or ""), reverse=True)
        return rows[: max(0, int(limit))]

    def filter_stats(self) -> dict[str, Any]:
        rows = self.list_filter_audit(limit=100000)
        decisions: dict[str, int] = {}
        statuses: dict[str, int] = {}
        modes: dict[str, int] = {}
        for row in rows:
            value_judge = row.get("value_judge") if isinstance(row.get("value_judge"), dict) else {}
            decision = str(value_judge.get("decision") or "unknown")
            mode = str(value_judge.get("mode") or "unknown")
            status = str(row.get("status") or "unknown")
            decisions[decision] = decisions.get(decision, 0) + 1
            statuses[status] = statuses.get(status, 0) + 1
            modes[mode] = modes.get(mode, 0) + 1
        return {
            "total": len(rows),
            "decisions": decisions,
            "statuses": statuses,
            "modes": modes,
        }
