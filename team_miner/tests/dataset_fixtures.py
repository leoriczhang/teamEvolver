"""Canonical Test Dataset fixtures; shorthand inputs here are never a runtime format."""

from __future__ import annotations

import copy
from typing import Any

from team_miner.benchmark_format import MINIMUM_REQUIREMENT_TARGET, PROGRESSIVE_DISCLOSURE
from team_replay.datasets.schema import make_document


def build_document(
    skill_name: str,
    scenarios: list[dict[str, Any]],
    *,
    candidate_revision: int = 1,
    created_at: str = "2026-10-10T00:00:00+00:00",
) -> dict[str, Any]:
    """Build explicit, complete fixture cases without invoking any model or miner.

    Tests may use concise scenario descriptions. Default conditions are test
    fixture content, not repairs or accepted input fields in production.
    """
    cases = []
    session_ids = []
    for index, scenario in enumerate(scenarios, start=1):
        gold = copy.deepcopy(scenario.get("gold") or {})
        output = list(scenario.get("requirements") or gold.get("must_hit") or [])
        output.extend(f"不得{item}" for item in gold.get("must_avoid") or [])
        output.extend(
            f"Fixture output condition {number}"
            for number in range(len(output) + 1, MINIMUM_REQUIREMENT_TARGET + 1)
        )
        trajectory = list(scenario.get("trajectory_requirements") or ["Read and verify the supplied Evidence."])
        sources = list(scenario.get("source_session_ids") or [])
        session_ids.extend(source for source in sources if source not in session_ids)
        query = str(scenario.get("query") or scenario.get("input") or "Fixture task")
        cases.append({
            "case_id": str(scenario.get("case_id") or scenario.get("id") or f"case-{index:03d}"),
            "query": query,
            "skill_ids": [skill_name],
            "checks": [
                {"id": f"R{number:02d}", "kind": "output", "text": text}
                for number, text in enumerate(output, start=1)
            ] + [
                {"id": f"T{number:02d}", "kind": "trajectory", "text": text}
                for number, text in enumerate(trajectory, start=1)
            ],
            "materials": copy.deepcopy(scenario.get("materials") or []),
            "provenance": {"session_ids": sources, "evidence_window": "historical", "synthesis_mode": "fixture"},
            "replay": {"progressive_disclosure": dict(PROGRESSIVE_DISCLOSURE)},
            "metadata": {
                "name": str(scenario.get("name") or query),
                "split": "test",
                "minimum_requirement_target": MINIMUM_REQUIREMENT_TARGET,
                "target_dimensions": list(scenario.get("target_dimensions") or []),
                "difficulty": str(scenario.get("difficulty") or "medium"),
                "gold": gold,
            },
        })
    return make_document(
        dataset_id=f"fixture-{skill_name}-{candidate_revision}",
        name=f"{skill_name} Test Dataset",
        skills=[skill_name],
        source={"kind": "skill_miner", "session_ids": session_ids},
        cases=cases,
        metadata={"candidate_revision": candidate_revision},
        created_at=created_at,
        updated_at=created_at,
    )
