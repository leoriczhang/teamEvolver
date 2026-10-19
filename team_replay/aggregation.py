"""Aggregate multi-window True Replay results into one release decision."""

from __future__ import annotations

from typing import Any

from .metrics import REPLAY_METRICS, UNAVAILABLE, compare_efficiency, metric_number
from .policy import aggregate_case_checklists, progressive_replay_decision


def aggregate_true_replay_windows(
    results: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    evaluated = [
        (window, result)
        for window, result in results
        if str(result.get("status") or "") in {"", "evaluated"}
    ]
    if not evaluated:
        return {
            "status": "skipped",
            "accepted": False,
            "no_regression": False,
            "reason": "no replay window produced an evaluation",
            "window_results": {window: result for window, result in results},
        }
    all_windows_evaluated = len(evaluated) == len(results)

    cases: list[dict[str, Any]] = []
    for window, result in evaluated:
        for raw_case in result.get("cases") or []:
            if isinstance(raw_case, dict):
                case = dict(raw_case)
                case["evidence_window"] = window
                cases.append(case)
    efficiency_inputs = {"baseline": {}, "candidate": {}}
    unavailable = {"baseline": set(), "candidate": set()}
    for _, result in evaluated:
        report = (
            result.get("efficiency")
            if isinstance(result.get("efficiency"), dict)
            else {}
        )
        for branch in ("baseline", "candidate"):
            values = (
                report.get(branch)
                if isinstance(report.get(branch), dict)
                else {}
            )
            for key in REPLAY_METRICS:
                value = metric_number(values.get(key))
                if value is None:
                    unavailable[branch].add(key)
                else:
                    efficiency_inputs[branch][key] = efficiency_inputs[branch].get(key, 0) + value
    for branch in ("baseline", "candidate"):
        for key in unavailable[branch]:
            efficiency_inputs[branch][key] = UNAVAILABLE
    efficiency = compare_efficiency(
        efficiency_inputs["baseline"],
        efficiency_inputs["candidate"],
    )
    branch_checklists = {
        branch: aggregate_case_checklists(cases, branch=branch)
        for branch in ("baseline", "candidate")
    }
    policy = progressive_replay_decision(
        efficiency=efficiency,
        baseline_checklist=branch_checklists["baseline"],
        candidate_checklist=branch_checklists["candidate"],
    )
    accepted = bool(policy.get("accepted")) and all_windows_evaluated
    verdict = str(policy.get("verdict") or "inconclusive")
    if not all_windows_evaluated and verdict == "accept":
        verdict = "inconclusive"
    no_regression = bool(policy.get("no_regression")) and all_windows_evaluated
    return {
        "status": "evaluated",
        "mode": "true_replay",
        "accepted": accepted,
        "verdict": verdict,
        "no_regression": no_regression,
        "case_count": sum(
            int(result.get("case_count") or 0)
            for _, result in evaluated
        ),
        "cases": cases,
        "efficiency": efficiency,
        "checklist": branch_checklists,
        "decision_policy": {
            **policy,
            "accepted": accepted,
            "all_windows_evaluated": all_windows_evaluated,
        },
        "window_results": {window: result for window, result in results},
    }
