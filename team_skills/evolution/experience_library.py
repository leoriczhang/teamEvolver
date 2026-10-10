"""Session-backed experience library, independent of the evolution engine."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from team_skills.evolution.stages.judge import _parse_skill_experiences


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
    """Aggregate lessons from the current Session index.

    The Session index is updated atomically with ingestion on PostgreSQL and
    under its existing per-store lock on NAS. No Skill upload, engine startup,
    extra model call or successful evolution cycle is required to read it.
    """

    def __init__(self, session_store) -> None:
        self._sessions = session_store

    def list_experiences(self, *, kind: str = "", skill: str = "", search: str = "") -> dict[str, Any]:
        rows = self._sessions.list_conversations(limit=10000)
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
