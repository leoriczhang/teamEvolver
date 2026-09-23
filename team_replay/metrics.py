"""Objective True Replay metric comparison and release policy."""

from __future__ import annotations

import math
from typing import Any

REPLAY_METRICS = (
    "interaction_turns",
    "tool_call_count",
    "total_tokens",
    "elapsed_seconds",
    "input_tokens",
    "output_tokens",
    "api_calls",
)

UNAVAILABLE = "unavailable"


def metric_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def count_tool_calls(messages: list[dict[str, Any]]) -> int:
    return sum(
        len(message.get("tool_calls") or [])
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )


def branch_efficiency(branch: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in (*REPLAY_METRICS, "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
        value = metric_number(branch.get(key))
        result[key] = value if value is not None else UNAVAILABLE
    messages = branch.get("messages") or []
    # A concrete tool trace proves a count. Absent trace does not prove zero.
    if result["tool_call_count"] == UNAVAILABLE and any("tool_calls" in message for message in messages):
        result["tool_call_count"] = count_tool_calls(messages)
    return result


def compare_efficiency(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    base = branch_efficiency(baseline)
    cand = branch_efficiency(candidate)
    dimensions: dict[str, dict[str, Any]] = {}
    for key in REPLAY_METRICS:
        baseline_value = metric_number(base[key])
        candidate_value = metric_number(cand[key])
        if baseline_value is None or candidate_value is None:
            dimensions[key] = {
                "baseline": base[key], "candidate": cand[key],
                "delta": None, "reduction_ratio": None, "winner": UNAVAILABLE,
            }
            continue
        delta = baseline_value - candidate_value
        gain = max(-1.0, min(1.0, delta / max(1, baseline_value)))
        dimensions[key] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "reduction_ratio": round(gain, 4),
            "winner": (
                "candidate"
                if delta > 0
                else "baseline"
                if delta < 0
                else "tie"
            ),
        }
    return {
        "baseline": base,
        "candidate": cand,
        "dimensions": dimensions,
        "improved_dimensions": [
            key for key, value in dimensions.items() if value["winner"] == "candidate"
        ],
        "regressed_dimensions": [
            key for key, value in dimensions.items() if value["winner"] == "baseline"
        ],
        "unchanged_dimensions": [
            key for key, value in dimensions.items() if value["winner"] == "tie"
        ],
    }


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
        baseline_value = metric_number(raw.get("baseline"))
        candidate_value = metric_number(raw.get("candidate"))
        if baseline_value is None or candidate_value is None:
            changes[name] = {
                "baseline": baseline_value if baseline_value is not None else UNAVAILABLE,
                "candidate": candidate_value if candidate_value is not None else UNAVAILABLE,
                "delta": None, "status": UNAVAILABLE,
            }
            continue
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
        decision_basis = "metrics_unavailable" if not (improved or regressed or unchanged) else "all_metrics_unchanged"
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
        "unavailable_metrics": [key for key, value in changes.items() if value["status"] == UNAVAILABLE],
    }
