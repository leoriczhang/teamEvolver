"""Persistent, human-readable lessons learned from Skill usage."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from teamEvolver.storage import is_not_found_error

_SCHEMA_VERSION = 1
_VALID_KINDS = {"defect", "exemplary"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    raw = str(value or "").strip()
    slug = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "-", raw).strip("-")
    if slug:
        return slug[:120]
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit]


def _used_skills(session: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for value in session.get("used_skills") or []:
        if value:
            names.append(str(value).strip())
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for value in turn.get("used_skills") or []:
            if value:
                names.append(str(value).strip())
    return list(dict.fromkeys(name for name in names if name))


def _experience_candidates(
    session: dict[str, Any],
    skill_name: str,
) -> list[dict[str, str]]:
    scores = (
        session.get("_judge_scores")
        if isinstance(session.get("_judge_scores"), dict)
        else {}
    )
    candidates: list[dict[str, str]] = []
    for raw in scores.get("skill_experiences") or []:
        if not isinstance(raw, dict):
            continue
        item_skill = str(raw.get("skill_name") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        key = str(raw.get("experience_key") or "").strip().lower()
        description = _clip(raw.get("description"), 1200)
        if item_skill != skill_name or kind not in _VALID_KINDS or not description:
            continue
        candidates.append(
            {
                "kind": kind,
                "experience_key": key or hashlib.sha256(
                    description.encode("utf-8")
                ).hexdigest()[:24],
                "description": description,
            }
        )

    # Compatibility for sessions analyzed before skill_experiences existed.
    # Only a single-Skill session is safe to attribute without guessing.
    if not candidates and _used_skills(session) == [skill_name]:
        kind = str(scores.get("evolution_evidence") or "").strip().lower()
        description = _clip(scores.get("evidence_reason"), 1200)
        if kind in _VALID_KINDS and description:
            candidates.append(
                {
                    "kind": kind,
                    "experience_key": hashlib.sha256(
                        description.encode("utf-8")
                    ).hexdigest()[:24],
                    "description": description,
                }
            )
    return candidates


class ExperienceLibraryStore:
    """Store one aggregated experience document per Skill."""

    def __init__(self, bucket: Any, *, prefix: str = "", max_records: int = 100) -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")
        self.max_records = max(1, int(max_records))

    def _key(self, skill_name: str) -> str:
        return f"{self._prefix}experience_library/{_safe_slug(skill_name)}.json"

    @staticmethod
    def _empty_state(skill_name: str) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "skill_name": str(skill_name or ""),
            "updated_at": "",
            "experiences": [],
        }

    def load(self, skill_name: str) -> dict[str, Any]:
        try:
            payload = json.loads(
                self._bucket.get_object(self._key(skill_name)).read().decode("utf-8")
            )
        except Exception as exc:
            if not is_not_found_error(exc):
                raise
            return self._empty_state(skill_name)
        if not isinstance(payload, dict):
            return self._empty_state(skill_name)
        state = self._empty_state(skill_name)
        state.update(payload)
        if not isinstance(state.get("experiences"), list):
            state["experiences"] = []
        return state

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        state["schema_version"] = _SCHEMA_VERSION
        state["updated_at"] = _utc_now_iso()
        self._bucket.put_object(
            self._key(str(state.get("skill_name") or "")),
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return state

    def record_sessions(
        self,
        skill_name: str,
        sessions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        state = self.load(skill_name)
        records = {
            str(item.get("id") or ""): dict(item)
            for item in state.get("experiences") or []
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        for session in sessions:
            if not isinstance(session, dict) or session.get("_evidence_window"):
                continue
            session_id = str(session.get("session_id") or "").strip()
            if not session_id:
                continue
            observed_at = str(
                session.get("ingested_at")
                or session.get("timestamp")
                or session.get("started_at")
                or _utc_now_iso()
            )
            user_alias = str(session.get("user_alias") or "").strip()
            scores = (
                session.get("_judge_scores")
                if isinstance(session.get("_judge_scores"), dict)
                else {}
            )
            score = scores.get("overall_score")
            score = (
                float(score)
                if isinstance(score, (int, float)) and not isinstance(score, bool)
                else None
            )
            for candidate in _experience_candidates(session, skill_name):
                identity = "|".join(
                    [skill_name, candidate["kind"], candidate["experience_key"]]
                )
                record_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                record = records.get(record_id) or {
                    "id": record_id,
                    "skill_name": skill_name,
                    "kind": candidate["kind"],
                    "experience_key": candidate["experience_key"],
                    "description": candidate["description"],
                    "occurrence_count": 0,
                    "session_ids": [],
                    "user_aliases": [],
                    "first_observed_at": observed_at,
                    "last_observed_at": observed_at,
                    "latest_score": score,
                }
                session_ids = [
                    str(item)
                    for item in record.get("session_ids") or []
                    if str(item)
                ]
                is_new_occurrence = session_id not in session_ids
                if is_new_occurrence:
                    session_ids.append(session_id)
                record["session_ids"] = session_ids[-200:]
                previous_count = max(
                    int(record.get("occurrence_count") or 0),
                    len(session_ids) - (1 if is_new_occurrence else 0),
                )
                record["occurrence_count"] = (
                    previous_count + 1 if is_new_occurrence else previous_count
                )
                users = [
                    str(item)
                    for item in record.get("user_aliases") or []
                    if str(item)
                ]
                if user_alias and user_alias not in users:
                    users.append(user_alias)
                record["user_aliases"] = users[-50:]
                if is_new_occurrence:
                    record["description"] = candidate["description"]
                    record["last_observed_at"] = observed_at
                    record["latest_score"] = score
                records[record_id] = record

        ordered = sorted(
            records.values(),
            key=lambda item: (
                str(item.get("last_observed_at") or ""),
                int(item.get("occurrence_count") or 0),
            ),
            reverse=True,
        )
        state["experiences"] = ordered[: self.max_records]
        return self._save(state)

    def backfill_legacy_evidence(self) -> int:
        """Import pre-library Skill evidence once, preserving Session identity."""
        marker_key = f"{self._prefix}experience_library/.evidence_backfill_v1.json"
        try:
            self._bucket.get_object(marker_key)
            return 0
        except Exception as exc:
            if not is_not_found_error(exc):
                raise

        evidence_prefix = f"{self._prefix}skill_evidence/"
        if hasattr(self._bucket, "iter_objects_bulk"):
            payloads = self._bucket.iter_objects_bulk(prefix=evidence_prefix)
            raw_values = payloads.values()
        else:
            raw_values = (
                self._bucket.get_object(item.key).read()
                for item in self._bucket.iter_objects(prefix=evidence_prefix)
            )
        imported = 0
        found_states = 0
        for raw in raw_values:
            try:
                state = json.loads(bytes(raw).decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            skill_name = str(state.get("skill_name") or "").strip()
            entries = state.get("evidence") if isinstance(state.get("evidence"), list) else []
            if not skill_name or not entries:
                continue
            found_states += 1
            sessions: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sessions.append(
                    {
                        "session_id": entry.get("session_id"),
                        "user_alias": entry.get("user_alias"),
                        "timestamp": entry.get("observed_at"),
                        "used_skills": [skill_name],
                        "_judge_scores": {
                            "overall_score": entry.get("judge_overall_score"),
                            "evolution_evidence": entry.get("evolution_evidence"),
                            "evidence_reason": entry.get("evidence_reason"),
                            "skill_experiences": entry.get("skill_experiences") or [],
                        },
                    }
                )
            before = len(self.load(skill_name).get("experiences") or [])
            after = len(self.record_sessions(skill_name, sessions).get("experiences") or [])
            imported += max(0, after - before)

        if found_states:
            self._bucket.put_object(
                marker_key,
                json.dumps(
                    {"completed_at": _utc_now_iso(), "imported": imported},
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
        return imported

    def list_experiences(
        self,
        *,
        kind: str = "",
        skill: str = "",
        search: str = "",
    ) -> dict[str, Any]:
        prefix = f"{self._prefix}experience_library/"
        rows: list[dict[str, Any]] = []
        if hasattr(self._bucket, "iter_objects_bulk"):
            payloads = self._bucket.iter_objects_bulk(prefix=prefix)
            raw_values = payloads.values()
        else:
            raw_values = (
                self._bucket.get_object(item.key).read()
                for item in self._bucket.iter_objects(prefix=prefix)
            )
        for raw in raw_values:
            try:
                state = json.loads(bytes(raw).decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            for item in state.get("experiences") or []:
                if isinstance(item, dict):
                    rows.append(dict(item))

        rows.sort(
            key=lambda item: (
                int(item.get("occurrence_count") or 0),
                str(item.get("last_observed_at") or ""),
            ),
            reverse=True,
        )
        skill_counts: dict[str, int] = {}
        for item in rows:
            name = str(item.get("skill_name") or "")
            if name:
                skill_counts[name] = skill_counts.get(name, 0) + 1
        stats = {
            "total_experiences": len(rows),
            "total_occurrences": sum(int(item.get("occurrence_count") or 0) for item in rows),
            "defect_experiences": sum(item.get("kind") == "defect" for item in rows),
            "defect_occurrences": sum(
                int(item.get("occurrence_count") or 0)
                for item in rows
                if item.get("kind") == "defect"
            ),
            "exemplary_experiences": sum(item.get("kind") == "exemplary" for item in rows),
            "exemplary_occurrences": sum(
                int(item.get("occurrence_count") or 0)
                for item in rows
                if item.get("kind") == "exemplary"
            ),
            "skills": len(skill_counts),
        }

        wanted_kind = str(kind or "").strip().lower()
        wanted_skill = str(skill or "").strip()
        needle = str(search or "").strip().lower()
        filtered = [
            item
            for item in rows
            if (not wanted_kind or item.get("kind") == wanted_kind)
            and (not wanted_skill or item.get("skill_name") == wanted_skill)
            and (
                not needle
                or needle in str(item.get("description") or "").lower()
                or needle in str(item.get("skill_name") or "").lower()
            )
        ]
        return {
            "items": filtered,
            "stats": stats,
            "skill_counts": dict(sorted(skill_counts.items())),
        }
