"""Reuse mandatory ingest analysis; never run a second Session Judge."""
from __future__ import annotations

import asyncio
import copy

from session_ingestion.schemas import session_payload_hash
from team_skills.evolution.stages.aggregate import aggregate_sessions_by_skill
from team_skills.evolution.stages.analyze import session_has_merged_outputs
from team_skills.evolution.stages.summarize import _extract_session_metadata, build_session_trajectory


async def load_sessions(descriptors: list[dict], payloads) -> list[dict]:
    sessions = []
    for descriptor in descriptors:
        raw = await asyncio.to_thread(payloads.get_session, descriptor["content_ref"])
        if session_payload_hash(raw) != descriptor["content_hash"]:
            raise ValueError("Session content hash changed after acceptance")
        session = copy.deepcopy(raw)
        if not session_has_merged_outputs(session):
            raise ValueError("Session requires mandatory ingest analysis before Skill Evolution")
        session.update({
            "session_id": descriptor["session_id"], "content_hash": descriptor["content_hash"],
            "user_alias": descriptor["user_id"],
        })
        _extract_session_metadata(session)
        session["_trajectory"] = build_session_trajectory(session)
        session["_avg_prm"] = session["_judge_scores"]["overall_score"]
        sessions.append(session)
    return sessions


def groups(sessions: list[dict]) -> dict[str, list[dict]]:
    return aggregate_sessions_by_skill(sessions)


def evidence_gap(sessions: list[dict], config) -> str:
    if any(
        (s.get("runtime_context") or {}).get("candidate_job_id")
        and (s.get("runtime_context") or {}).get("candidate_sha256") for s in sessions
    ):
        return ""
    users = {s.get("user_alias") for s in sessions if s.get("user_alias")}
    ids = {s["session_id"] for s in sessions}
    if len(ids) < int(config.get("min_group_sessions")):
        return "insufficient distinct Sessions for team evidence"
    if len(users) < int(config.get("min_group_users")):
        return "insufficient distinct users for team evidence"
    return ""


def replay_cases(sessions: list[dict]) -> list[dict]:
    cases = []
    for session in sessions:
        context = session.get("runtime_context") or {}
        if context.get("candidate_job_id"):
            continue
        for turn in session.get("turns") or []:
            if not turn.get("prompt_text") or not turn.get("response_text"):
                continue
            cases.append({
                "case_id": f"{session['session_id']}:{int(turn.get('turn_num') or 0)}",
                "query": str(turn["prompt_text"])[:3000], "skill_ids": [],
                "checks": [{"id": "R01", "kind": "output", "text": str(turn["prompt_text"])[:3000]}],
                "materials": [], "provenance": {
                    "session_id": session["session_id"], "turn_num": int(turn.get("turn_num") or 0),
                    "session_snapshot": {
                        key: copy.deepcopy(session[key])
                        for key in ("session_id", "turns", "runtime", "runtime_context", "source")
                        if key in session
                    },
                    "source_runtime": copy.deepcopy(session.get("runtime") or {"type": session.get("source", "")}),
                    "source_runtime_context": copy.deepcopy(context),
                    "evidence_window": session.get("_evidence_window") or "recent",
                },
                "replay": {"progressive_disclosure": {"batch_size": 4}},
                "metadata": {"evaluation_profile": str(context.get("evaluation_profile") or "")},
            })
            if len(cases) == 6:
                return cases
    return cases
