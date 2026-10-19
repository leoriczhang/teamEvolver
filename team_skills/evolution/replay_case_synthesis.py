"""Cross-Session synthesis of the two current Candidate Replay Cases."""
from __future__ import annotations

import copy
from collections.abc import Mapping

from team_replay.datasets.synthesis import synthesize_evolution_datasets

CURRENT_CASE_COUNT = 2
MIN_CHECKS = 6
MAX_CHECKS = 8
MIN_SOURCE_SESSIONS = 2


class CrossSessionCaseSynthesisError(ValueError):
    pass


class LLMCrossSessionCaseSynthesizer:
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, *, skill_name, sessions, candidate_skill, evidence_context):
        generated = await synthesize_evolution_datasets(
            self.llm,
            skill_name=skill_name,
            sessions=sessions,
            candidate_skill=candidate_skill,
            evidence_context=evidence_context,
            case_count=CURRENT_CASE_COUNT,
            min_requirements=MIN_CHECKS,
            max_requirements=MAX_CHECKS,
            min_source_sessions=MIN_SOURCE_SESSIONS,
            batch_size=2,
        )
        return hydrate_cross_session_cases(generated, sessions, skill_name=skill_name)


def hydrate_cross_session_cases(cases, sessions, *, skill_name):
    if len(cases) != CURRENT_CASE_COUNT:
        raise CrossSessionCaseSynthesisError("Skill Candidate requires exactly two cross-Session Replay Cases")
    sources = {
        str(session.get("session_id") or ""): session
        for session in sessions if str(session.get("session_id") or "").strip()
    }
    hydrated = []
    for raw in cases:
        case = copy.deepcopy(dict(raw))
        provenance = case.setdefault("provenance", {})
        source_ids = list(dict.fromkeys(
            str(value or "").strip() for value in provenance.get("session_ids") or []
            if str(value or "").strip() in sources
        ))
        if len(source_ids) < MIN_SOURCE_SESSIONS:
            raise CrossSessionCaseSynthesisError("each current Replay Case requires at least two source Sessions")
        representative = next((
            sources[source_id] for source_id in source_ids
            if isinstance(sources[source_id].get("turns"), list) and sources[source_id].get("turns")
        ), None)
        if representative is None:
            raise CrossSessionCaseSynthesisError("Replay Case has no executable source Session")
        checks = case.get("checks") if isinstance(case.get("checks"), list) else []
        if not MIN_CHECKS <= len(checks) <= MAX_CHECKS:
            raise CrossSessionCaseSynthesisError("current Replay Case Checklist must contain 6..8 items")
        runtime_context = (
            representative.get("runtime_context")
            if isinstance(representative.get("runtime_context"), Mapping)
            else {}
        )
        provenance.update({
            "session_ids": source_ids,
            "session_id": str(representative.get("session_id") or ""),
            "turn_num": 1,
            "session_snapshot": {
                key: copy.deepcopy(representative[key])
                for key in ("session_id", "turns", "runtime", "runtime_context", "source")
                if key in representative
            },
            "source_runtime": copy.deepcopy(
                representative.get("runtime") or {"type": representative.get("source", "")}
            ),
            "source_runtime_context": copy.deepcopy(runtime_context),
            "evidence_window": "recent",
            "synthesis_mode": "cross_session",
        })
        case["skill_ids"] = [skill_name]
        hydrated.append(case)
    return hydrated
