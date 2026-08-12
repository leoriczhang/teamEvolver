"""Objective True Replay metric comparison and release policy."""

from __future__ import annotations

from typing import Any


REPLAY_METRICS = (
    "interaction_turns",
    "tool_call_count",
    "total_tokens",
)


def objective_replay_decision(
    *,
    efficiency: dict[str, Any],
) -> dict[str, Any]:
    dimensions = (
        efficiency.get("dimensions")
        if isinstance(efficiency.get("dimensions"), dict)
        else {}
    )
    changes: dict[str, dict[str, Any]] = {}
    improved: list[str] = []
    regressed: list[str] = []
    unchanged: list[str] = []

    for name in REPLAY_METRICS:
        raw = (
            dimensions.get(name)
            if isinstance(dimensions.get(name), dict)
            else {}
        )
        baseline_value = int(raw.get("baseline") or 0)
        candidate_value = int(raw.get("candidate") or 0)
        delta = baseline_value - candidate_value
        status = (
            "improved"
            if delta > 0
            else "regressed"
            if delta < 0
            else "unchanged"
        )
        changes[name] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "status": status,
        }
        if status == "improved":
            improved.append(name)
        elif status == "regressed":
            regressed.append(name)
        else:
            unchanged.append(name)

    turn_status = changes["interaction_turns"]["status"]
    secondary_metrics = ("tool_call_count", "total_tokens")
    secondary_improved = [
        name for name in secondary_metrics if changes[name]["status"] == "improved"
    ]
    secondary_regressed = [
        name for name in secondary_metrics if changes[name]["status"] == "regressed"
    ]

    if turn_status == "improved":
        accepted = True
        verdict = "accept"
        decision_basis = "interaction_turns_decreased"
        decisive_metrics = ["interaction_turns"]
    elif turn_status == "regressed":
        accepted = False
        verdict = "reject"
        decision_basis = "interaction_turns_increased"
        decisive_metrics = ["interaction_turns"]
    elif secondary_improved and not secondary_regressed:
        accepted = True
        verdict = "accept"
        decision_basis = "secondary_metrics_decreased"
        decisive_metrics = secondary_improved
    elif secondary_regressed:
        accepted = False
        verdict = "reject"
        decision_basis = "secondary_metrics_increased"
        decisive_metrics = secondary_regressed
    else:
        accepted = False
        verdict = "inconclusive"
        decision_basis = "all_metrics_unchanged"
        decisive_metrics = []

    return {
        "accepted": accepted,
        "verdict": verdict,
        "policy": "true_replay_turn_priority_v2",
        "decision_basis": decision_basis,
        "primary_metric": "interaction_turns",
        "secondary_metrics": list(secondary_metrics),
        "decisive_metrics": decisive_metrics,
        "no_regression": verdict != "reject",
        "metric_changes": changes,
        "improved_metrics": improved,
        "regressed_metrics": regressed,
        "unchanged_metrics": unchanged,
    }
