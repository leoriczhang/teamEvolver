"""Session-backed experience library, independent of the evolution engine."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from team_skills.evolution.stages.judge import _parse_skill_experiences
from teamEvolver.storage import is_not_found_error


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def session_experiences(session: dict[str, Any]) -> list[dict[str, str]]:
    """Return model-authored, named Skill lessons from one Session."""
    scores = session.get("_judge_scores") or {}
    public = session.get("judge") or {}
    if "skill_experiences" in public:
        scores = public
    elif not scores:
        scores = public
    return _parse_skill_experiences(scores)


class ExperienceLibraryStore:
    """Aggregate indexed Session lessons and historical Skill Evidence.

    The Session index is updated atomically with ingestion on PostgreSQL and
    under its existing per-store lock on NAS. No Skill upload, engine startup,
    extra model call or successful evolution cycle is required to read it.
    """

    def __init__(self, bucket, prefix: str = "", *, session_store=None) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._sessions = session_store

    def _load(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._bucket.get_object(key).read())
        except Exception as exc:
            if is_not_found_error(exc):
                return None
            raise

    def record_sessions(self, skill_name: str, sessions: list[dict[str, Any]]) -> None:
        """Compatibility writer for per-Skill Evidence, deduped by Session."""
        for session in sessions:
            sid = str(session.get("session_id") or "").strip()
            if not sid:
                continue
            experiences = [
                item for item in session_experiences(session)
                if item["skill_name"] == skill_name
            ]
            scores = session.get("_judge_scores") or session.get("judge") or {}
            row = {
                "session_id": sid,
                "user_alias": session.get("user_alias") or "",
                "timestamp": session.get("timestamp") or session.get("observed_at") or "",
                "ingested_at": session.get("ingested_at") or "",
                "judge": {"overall_score": scores.get("overall_score")},
                "experiences": experiences,
            }
            key = f"{self._prefix}experience_library/sessions/{_digest(skill_name, sid)}.json"
            self._bucket.put_object(key, json.dumps(row, ensure_ascii=False).encode())

    def backfill_legacy_evidence(self) -> int:
        """Import legacy per-Skill model evidence without loading Skill files."""
        count = 0
        for obj in self._bucket.iter_objects(prefix=f"{self._prefix}skill_evidence/"):
            if not obj.key.endswith(".json"):
                continue
            payload = self._load(obj.key) or {}
            skill_name = str(payload.get("skill_name") or "").strip()
            if not skill_name:
                continue
            for evidence in payload.get("evidence") or []:
                sid = str(evidence.get("session_id") or "").strip()
                kind = str(evidence.get("evolution_evidence") or "").strip().lower()
                reason = str(evidence.get("evidence_reason") or "").strip()
                if not sid or kind not in {"defect", "exemplary"} or not reason:
                    continue
                key = f"{self._prefix}experience_library/sessions/{_digest(skill_name, sid)}.json"
                if self._load(key) is not None:
                    continue
                self.record_sessions(skill_name, [{
                    **evidence,
                    "_judge_scores": {
                        "overall_score": evidence.get("judge_overall_score"),
                        "skill_experiences": [{
                            "skill_name": skill_name,
                            "kind": kind,
                            "experience_key": f"legacy_{_digest(reason)}",
                            "description": reason,
                        }],
                    },
                }])
                count += 1
        return count

    def list_experiences(self, *, kind: str = "", skill: str = "", search: str = "") -> dict[str, Any]:
        rows = [
            self._load(obj.key) or {}
            for obj in self._bucket.iter_objects(prefix=f"{self._prefix}experience_library/sessions/")
            if obj.key.endswith(".json")
        ]
        if self._sessions is not None:
            # Existing archives are repaired into the index once; subsequent
            # requests read only lightweight metadata, never full trajectories.
            rows.extend(self._sessions.list_conversations(limit=10000))
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            sid = str(row.get("session_id") or "")
            if not sid:
                continue
            observed = str(row.get("timestamp") or row.get("ingested_at") or "")
            for lesson in row.get("experiences") or []:
                skill_name = str(lesson.get("skill_name") or "").strip()
                if not skill_name:
                    continue
                identity = _digest(skill_name, lesson["kind"], lesson["experience_key"])
                group = groups.setdefault(identity, {"lesson": lesson, "occurrences": {}})
                group["occurrences"][sid] = {
                    "session_id": sid,
                    "observed_at": observed,
                    "ingested_at": str(row.get("ingested_at") or ""),
                    "user_alias": str(row.get("user_alias") or ""),
                    "score": (row.get("judge") or {}).get("overall_score"),
                    "description": lesson["description"],
                }
        items = []
        for identity, group in groups.items():
            occurrences = sorted(
                group["occurrences"].values(),
                key=lambda item: (item["observed_at"], item["session_id"]),
            )
            latest = occurrences[-1]
            items.append({
                **group["lesson"],
                "id": identity,
                "description": latest["description"],
                "occurrence_count": len(occurrences),
                "session_ids": [item["session_id"] for item in occurrences],
                "user_aliases": sorted({item["user_alias"] for item in occurrences if item["user_alias"]}),
                "first_observed_at": occurrences[0]["observed_at"],
                "last_observed_at": latest["observed_at"],
                "last_ingested_at": max(item["ingested_at"] for item in occurrences),
                "latest_score": latest["score"],
            })
        skill_counts = Counter(item["skill_name"] for item in items if item["skill_name"])
        stats = {
            "total_experiences": len(items),
            "total_occurrences": sum(item["occurrence_count"] for item in items),
            "defect_experiences": sum(item["kind"] == "defect" for item in items),
            "defect_occurrences": sum(item["occurrence_count"] for item in items if item["kind"] == "defect"),
            "exemplary_experiences": sum(item["kind"] == "exemplary" for item in items),
            "exemplary_occurrences": sum(item["occurrence_count"] for item in items if item["kind"] == "exemplary"),
            "skills": len(skill_counts),
        }
        needle = search.strip().casefold()
        items = [
            item for item in items
            if (not kind or item["kind"] == kind)
            and (not skill or item["skill_name"] == skill)
            and (not needle or needle in f"{item['skill_name']} {item['description']}".casefold())
        ]
        items.sort(key=lambda item: (item["last_observed_at"], item["id"]), reverse=True)
        return {"items": items, "stats": stats, "skill_counts": dict(sorted(skill_counts.items()))}
