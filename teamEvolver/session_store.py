"""Session queue, archive, and filter-audit storage helpers."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from team_skills.evolution.experience_library import session_experiences
from team_skills.library.hub import SkillHub

from .storage import is_not_found_error


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Cross-instance locks for the session-index read-modify-write. Ingest is
# concurrent (HTTP requests, bounded Langfuse pull fan-out), and every queued
# ingest merges into ``session_index.json`` (load → merge → overwrite). The
# object store's per-key write lock only serializes the final PUT; without a
# process-wide RMW lock two concurrent ingests read the same base snapshot and
# the loser's rows are silently dropped. Keyed by store identity so distinct
# buckets never contend.
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _index_lock_for(bucket) -> threading.Lock:
    root = str(getattr(bucket, "root", "") or "")
    if root:
        identity = f"local:{os.path.abspath(root)}"
    else:
        endpoint = str(getattr(bucket, "_endpoint", "") or "")
        if endpoint:
            identity = f"remote:{endpoint}:{getattr(bucket, '_root_prefix', '')}"
        else:
            # In-process stores (tests): the shared bucket registry returns a
            # singleton per memory:// endpoint, so object identity is stable.
            identity = f"obj:{id(bucket)}"
    with _INDEX_LOCKS_GUARD:
        lock = _INDEX_LOCKS.get(identity)
        if lock is None:
            lock = threading.Lock()
            _INDEX_LOCKS[identity] = lock
        return lock


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


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


def _session_fingerprint(session: dict[str, Any]) -> str:
    """Stable content fingerprint used to detect whether a re-ingested
    session actually changed.

    Combines the turn count with a hash of the transcript text so that a
    session which is merely re-submitted (same conversation, later time)
    produces the same fingerprint, while genuinely continued conversations
    (new turns / new text) produce a different one.
    """
    parts: list[str] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        parts.append(str(turn.get("prompt_text") or turn.get("instruction") or ""))
        parts.append(str(turn.get("response_text") or turn.get("response") or ""))
    if not parts:
        for message in session.get("messages") or []:
            if isinstance(message, dict):
                parts.append(str(message.get("role") or ""))
                parts.append(str(message.get("content") or ""))
    runtime = (
        session.get("runtime")
        if isinstance(session.get("runtime"), dict)
        else {}
    )
    parts.extend(
        [
            str(runtime.get("type") or session.get("source") or ""),
            str(runtime.get("integration_id") or ""),
            json.dumps(session.get("legacy_converter") or {}, sort_keys=True),
        ]
    )
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        usage = (
            turn.get("context_usage")
            if isinstance(turn.get("context_usage"), dict)
            else {}
        )
        parts.append(
            json.dumps(
                {
                    "snapshot": usage.get("context_snapshot_id") or "",
                    "memory_refs": usage.get("memory_refs") or [],
                    "skill_refs": usage.get("skill_refs") or [],
                    "feedback": usage.get("feedback") or {},
                    "tool_calls": turn.get("tool_calls") or [],
                    "tool_results": turn.get("tool_results") or [],
                    "tool_errors": turn.get("tool_errors") or [],
                    "used_skills": turn.get("used_skills") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{_num_turns(session)}:{digest}"


def _session_used_skills(session: dict[str, Any]) -> list[str]:
    """Union of skills actually used across the session (top level + turns).

    Used both for list filtering (group sessions by skill) and exports; keeps
    first-seen order and de-duplicates.
    """
    skills: list[str] = []
    top = session.get("used_skills")
    if isinstance(top, list):
        skills.extend(str(s) for s in top if s)
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        used = turn.get("used_skills")
        if isinstance(used, list):
            skills.extend(str(s) for s in used if s)
    seen: set[str] = set()
    return [s for s in skills if not (s in seen or seen.add(s))]


def session_display_meta(session: dict[str, Any]) -> dict[str, str]:
    """Adapter-provided display meta: {user_id, session_id, trace_id}.

    Populated by the tenant adapter's ``extract_meta`` hook (pull path) or, for
    the push path, derived from the Agent envelope. Only these three scalar keys
    are carried into the lightweight session index for the console “运行总览”
    list and the detail payload; anything else is dropped.
    """
    raw = session.get("meta")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("user_id", "session_id", "trace_id"):
        value = raw.get(key)
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            out[key] = text[:200]
    return out


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
        "content_fingerprint": _session_fingerprint(session),
        "used_skills": _session_used_skills(session),
        "value_judge": session.get("value_judge") if isinstance(session.get("value_judge"), dict) else {},
        # Adapter-extracted display meta (user_id / session_id / trace_id).
        "meta": session_display_meta(session),
        # Session-level judge scores (dimensions + per-dimension reasons) are
        # persisted alongside the classifier verdict so every judged session —
        # including ones skipped by the value filter and never consumed by an
        # evolution cycle — still surfaces its review conclusion in the console.
        "judge": session.get("judge") if isinstance(session.get("judge"), dict) else {},
        "experiences": session_experiences(session),
    }


class SessionStore:
    """Object-store backed session lifecycle manager."""

    def __init__(self, bucket, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")

    @classmethod
    def from_config(cls, config, tenant_id: str = "default") -> "SessionStore":
        hub = SkillHub.object_storage_from_config(config, tenant_id=tenant_id)
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

    def session_index_key(self) -> str:
        return self._key("session_index.json")

    def _load_session_index(self) -> list[dict[str, Any]]:
        if hasattr(self._bucket, "read_session_index"):
            return self._bucket.read_session_index(self.session_index_key())
        try:
            raw = json.loads(
                self._bucket.get_object(self.session_index_key())
                .read()
                .decode("utf-8")
            )
        except Exception:
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _upsert_session_index(self, session: dict[str, Any], *, status: str) -> None:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return
        self._merge_session_index([(session_id, session)], status=status)

    def _merge_session_index(
        self, sessions: list[tuple[str, dict[str, Any]]], *, status: str
    ) -> None:
        """Merge freshly loaded sessions into the persisted index in one write.

        Serialized per store identity: concurrent ingests (HTTP + Langfuse
        pull fan-out) otherwise race the load→merge→overwrite cycle and lose
        each other's rows.
        """
        if hasattr(self._bucket, "write_session_records"):
            self._bucket.write_session_records(
                {}, self.session_index_key(), [_session_meta(session, status=status) for _, session in sessions]
            )
            return
        with _index_lock_for(self._bucket):
            fresh_ids = {session_id for session_id, _ in sessions}
            rows = [
                item
                for item in self._load_session_index()
                if str(item.get("session_id") or "") not in fresh_ids
            ]
            for _session_id, session in sessions:
                rows.append(_session_meta(session, status=status))
            rows.sort(
                key=lambda item: str(item.get("timestamp") or item.get("ingested_at") or ""),
                reverse=True,
            )
            self._bucket.put_object(
                self.session_index_key(),
                json.dumps(rows[:10000], ensure_ascii=False, indent=2).encode("utf-8"),
            )

    def save_queued(self, session: dict[str, Any]) -> str:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        queued = dict(session)
        queued["status"] = "queued"
        queued.setdefault("ingested_at", utc_now_iso())
        if hasattr(self._bucket, "write_session_records"):
            self._save_pg_session(queued, queued=True)
            return self.queue_key(session_id)
        self._bucket.put_object(self.queue_key(session_id), _json_bytes(queued))
        self._bucket.put_object(self.archive_key(session_id), _json_bytes(queued))
        self.save_filter_audit(queued, status="queued")
        self._upsert_session_index(queued, status="queued")
        return self.queue_key(session_id)

    def save_skipped(self, session: dict[str, Any]) -> None:
        skipped = dict(session)
        skipped["status"] = "skipped"
        skipped.setdefault("ingested_at", utc_now_iso())
        session_id = str(skipped.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        if hasattr(self._bucket, "write_session_records"):
            self._save_pg_session(skipped, queued=False)
            return
        self._bucket.put_object(self.archive_key(session_id), _json_bytes(skipped))
        self.save_filter_audit(skipped, status="skipped")
        self._upsert_session_index(skipped, status="skipped")

    def _save_pg_session(self, session: dict, *, queued: bool) -> None:
        sid = session["session_id"]
        meta = _session_meta(session, status=session["status"])
        objects = {
            self.archive_key(sid): _json_bytes(session),
            self.filter_audit_key(sid): _json_bytes({**meta, "recorded_at": utc_now_iso()}),
        }
        if queued:
            objects[self.queue_key(sid)] = _json_bytes(session)
        self._bucket.write_session_records(objects, self.session_index_key(), [meta])

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

    def conversation_statuses(self, session_ids: list[str]) -> dict[str, str]:
        """Resolve queued/consumed state without reading every archived payload."""
        wanted = {str(value or "").strip() for value in session_ids}
        wanted.discard("")
        if not wanted:
            return {}
        queue_keys = self._queue_keys()
        archive_keys = set(_safe_list(self._bucket, self._key("session_archive/")))
        statuses: dict[str, str] = {}
        for session_id in wanted:
            if self.queue_key(session_id) in queue_keys:
                statuses[session_id] = "queued"
            elif self.archive_key(session_id) in archive_keys:
                statuses[session_id] = "consumed"
            else:
                statuses[session_id] = "unknown"
        return statuses

    def duplicate_of_processed(self, session: dict[str, Any]) -> bool:
        """True when identical content already has complete model analysis.

        Guards against the same conversation being re-submitted at a later
        time (no new turns), which would otherwise re-queue it, regenerate the
        same coalesced candidate, and re-run a redundant evolution cycle. A
        genuinely continued conversation has a different fingerprint and is not
        treated as a duplicate.
        """
        from team_skills.evolution.stages.analyze import session_has_merged_outputs

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return False
        prior = _load_json(self._bucket, self.archive_key(session_id))
        if not prior or not session_has_merged_outputs(prior):
            return False
        return _session_fingerprint(prior) == _session_fingerprint(session)

    def load_index_rows(self) -> list[dict[str, Any]]:
        """Public read of the persisted session index (newest first)."""
        return self._load_session_index()

    def load_archived(self, session_id: str) -> Optional[dict[str, Any]]:
        """Load one archived session payload (queued or skipped)."""
        return _load_json(self._bucket, self.archive_key(session_id))

    def has_judge_score(self, session_id: str) -> bool:
        """True when the archived session already carries a numeric judge score."""
        session = self.load_archived(session_id)
        if not isinstance(session, dict):
            return False
        judge = session.get("judge")
        return isinstance(judge, dict) and isinstance(
            judge.get("overall_score"), (int, float)
        ) and not isinstance(judge.get("overall_score"), bool)

    def save_session_judge(self, session_id: str, judge: dict[str, Any]) -> bool:
        """Persist a session-level quality review onto archive + index.

        Used by the post-ingest async judge so sessions the value filter
        marks ``skipped`` (task_only / chitchat) — which never enter an
        evolution cycle — still surface their Good/Bad review in the console.
        Writes the archive payload and merges the row into the session index
        under the SAME index RMW lock as ingest; the queue object (if still
        present) is updated too so a later drain keeps the score.

        Returns False when the session no longer exists or carries no turns
        (nothing judgeable).
        """
        session_id = str(session_id or "").strip()
        if not session_id or not isinstance(judge, dict):
            return False
        session = _load_json(self._bucket, self.archive_key(session_id))
        if session is None:
            return False
        if not isinstance(session.get("turns"), list) or not session.get("turns"):
            return False
        status = str(session.get("status") or "consumed")
        updated = dict(session)
        updated["judge"] = judge
        updated.setdefault("judged_at", judge.get("judged_at") or utc_now_iso())
        self._bucket.put_object(self.archive_key(session_id), _json_bytes(updated))
        try:
            queue = _load_json(self._bucket, self.queue_key(session_id))
            if queue is not None:
                queue = dict(queue)
                queue["judge"] = judge
                queue.setdefault("judged_at", updated["judged_at"])
                self._bucket.put_object(self.queue_key(session_id), _json_bytes(queue))
        except Exception:  # noqa: BLE001 - queue sync is best-effort
            pass
        self._merge_session_index([(session_id, updated)], status=status)
        return True

    def list_queue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        queue_keys = self._queue_keys()
        # Fast path: derive rows from the session index instead of
        # downloading every queued session object (hundreds of sequential
        # viking reads that stall the dashboard). Download only the sessions
        # missing from the index, then merge them back in a single write so
        # one missing key never degrades into a full scan.
        rows: list[dict[str, Any]] = []
        fresh: list[tuple[str, dict[str, Any]]] = []
        index_rows = self._load_session_index()
        if index_rows:
            by_id = {
                str(item.get("session_id") or ""): item for item in index_rows
            }
            for key in queue_keys:
                session_id = os.path.basename(key)[:-5]
                row = by_id.get(session_id)
                if row is not None:
                    rows.append({**row, "status": "queued", "key": key})
                    continue
                session = _load_json(self._bucket, key)
                if not session:
                    continue
                fresh.append(
                    (
                        str(session.get("session_id") or session_id),
                        session,
                    )
                )
                rows.append({**_session_meta(session, status="queued"), "key": key})
        else:
            # Cold start: one full scan, then persist the index so subsequent
            # calls take the fast path.
            for key in _safe_list(self._bucket, self._key("sessions/")):
                session = _load_json(self._bucket, key)
                if not session:
                    continue
                fresh.append(
                    (
                        str(session.get("session_id") or os.path.basename(key)[:-5]),
                        session,
                    )
                )
                rows.append({**_session_meta(session, status="queued"), "key": key})
        if fresh:
            self._merge_session_index(fresh, status="queued")
        rows.sort(key=lambda item: str(item.get("timestamp") or item.get("ingested_at") or ""), reverse=True)
        return rows[: max(0, int(limit))]

    def list_conversations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        queue_keys = self._queue_keys()
        # The cold-start rebuild and the backfill persist a full index
        # overwrite; hold the same RMW lock as ingest merges so a concurrent
        # save_queued is not clobbered by a stale rebuild snapshot.
        with _index_lock_for(self._bucket):
            rows = self._load_session_index()
            if not rows:
                for key in _safe_list(self._bucket, self._key("session_archive/")):
                    session = _load_json(self._bucket, key)
                    if not session:
                        continue
                    session_id = str(session.get("session_id") or os.path.basename(key)[:-5])
                    rows.append(
                        {
                            **_session_meta(
                                session,
                                status=str(session.get("status") or "queued"),
                            ),
                            "key": key,
                        }
                    )
                if rows:
                    rows.sort(
                        key=lambda item: str(
                            item.get("timestamp") or item.get("ingested_at") or ""
                        ),
                        reverse=True,
                    )
                    self._bucket.put_object(
                        self.session_index_key(),
                        json.dumps(rows[:10000], ensure_ascii=False, indent=2).encode("utf-8"),
                    )
            for row in rows:
                session_id = str(row.get("session_id") or "")
                if (
                    str(row.get("status") or "") == "queued"
                    and self.queue_key(session_id) not in queue_keys
                ):
                    row["status"] = "consumed"
            # Repair fields introduced after the original Session index. Each
            # archive is read at most once, then the enriched row is persisted.
            stale_rows = [
                row for row in rows
                if "used_skills" not in row or "meta" not in row or "experiences" not in row
            ]
            if stale_rows:
                for row in stale_rows:
                    needs_skills = "used_skills" not in row
                    needs_meta = "meta" not in row
                    needs_experiences = "experiences" not in row
                    session = (
                        _load_json(self._bucket, self.archive_key(str(row.get("session_id") or "")))
                        if needs_skills or needs_meta or needs_experiences
                        else None
                    )
                    if needs_skills:
                        row["used_skills"] = _session_used_skills(session) if session else []
                    if needs_meta:
                        row["meta"] = session_display_meta(session) if session else {}
                    if needs_experiences:
                        row["experiences"] = session_experiences(session) if session else []
                rows.sort(
                    key=lambda item: str(item.get("timestamp") or item.get("ingested_at") or ""),
                    reverse=True,
                )
                if hasattr(self._bucket, "backfill_session_index_fields"):
                    self._bucket.backfill_session_index_fields(self.session_index_key(), stale_rows)
                else:
                    self._bucket.put_object(
                        self.session_index_key(),
                        json.dumps(rows[:10000], ensure_ascii=False, indent=2).encode("utf-8"),
                    )
        rows.sort(key=lambda item: str(item.get("timestamp") or item.get("ingested_at") or ""), reverse=True)
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
