"""Shared progressive-disclosure and checklist decision protocol."""

from __future__ import annotations

from typing import Any, Mapping

from .metrics import objective_replay_decision


def normalize_case_checklist(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_checks = case.get("checks") or []
    return [
        {
            "id": str(item.get("id") or ""),
            "text": str(item.get("text") or "").strip(),
            "kind": str(item.get("kind") or ""),
            **({"polarity": "must_not"} if item.get("polarity") == "must_not" else {}),
        }
        for item in raw_checks
        if isinstance(item, Mapping)
        and str(item.get("id") or "").strip()
        and str(item.get("text") or "").strip()
        and item.get("kind") in {"output", "trajectory"}
    ]


def progressive_config(case: Mapping[str, Any]) -> dict[str, Any]:
    replay = case.get("replay") if isinstance(case.get("replay"), Mapping) else {}
    raw = replay.get("progressive_disclosure") if isinstance(replay.get("progressive_disclosure"), Mapping) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "initial_visibility": "query_only",
        "batch_size": max(1, int(raw.get("batch_size") or 4)),
        "stop_when": "all_checklist_items_satisfied",
    }


def initial_query(case: Mapping[str, Any]) -> str:
    return str(case.get("query") or "").strip()


def select_replay_cases(
    cases: list[Any],
) -> list[tuple[str, int]]:
    """Run every canonical Dataset case."""
    selected: list[tuple[str, int]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            continue
        provenance = case.get("provenance") if isinstance(case.get("provenance"), Mapping) else {}
        window = str(provenance.get("evidence_window") or "recent")
        case_id = str(case.get("case_id") or index)
        selected.append((f"{window}:{case_id}", index))
    return selected


def normalize_checklist_report(
    branch: Mapping[str, Any],
    *,
    expected_checklist: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = (
        branch.get("checklist_report")
        if isinstance(branch.get("checklist_report"), Mapping)
        else {}
    )
    expected = expected_checklist or []
    raw_items = raw.get("items") if isinstance(raw.get("items"), list) else []
    by_id = {
        str(item.get("id") or ""): item
        for item in raw_items
        if isinstance(item, Mapping) and item.get("id")
    }
    items: list[dict[str, Any]] = []
    if expected:
        for expected_item in expected:
            item_id = str(expected_item.get("id") or "")
            actual = by_id.get(item_id, {})
            items.append(
                {
                    **expected_item,
                    "satisfied": actual.get("satisfied") is True and bool(str(actual.get("evidence") or "").strip()),
                    "evidence": str(actual.get("evidence") or ""),
                }
            )
    else:
        items = [
            {
                "id": str(item.get("id") or ""),
                "text": str(item.get("text") or ""),
                "kind": str(item.get("kind") or "output"),
                "satisfied": item.get("satisfied") is True and bool(str(item.get("evidence") or "").strip()),
                "evidence": str(item.get("evidence") or ""),
            }
            for item in raw_items
            if isinstance(item, Mapping)
        ]
    satisfied = [item for item in items if item.get("satisfied")]
    unmet = [item for item in items if not item.get("satisfied")]
    total = len(items)
    all_satisfied = bool(raw.get("all_satisfied")) if not items else not unmet
    if total == 0:
        all_satisfied = True
    return {
        "all_satisfied": all_satisfied,
        "total": total,
        "satisfied_count": len(satisfied),
        "unmet_count": len(unmet),
        "items": items,
        "satisfied_ids": [str(item.get("id") or "") for item in satisfied],
        "unmet_ids": [str(item.get("id") or "") for item in unmet],
        "rounds": int(
            raw.get("rounds")
            or branch.get("interaction_turns")
            or len(branch.get("interactions") or [])
            or 0
        ),
        "judge": raw.get("judge", branch.get("judge", "")),
        "positive_observations": [
            value for value in raw.get("positive_observations") or []
            if isinstance(value, str) and value.strip()
        ],
    }


def aggregate_case_checklists(
    cases: list[dict[str, Any]],
    *,
    branch: str,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            continue
        side = case.get(branch) if isinstance(case.get(branch), Mapping) else {}
        report = normalize_checklist_report(side)
        if report["total"] > 0:
            reports.append(report)
    return {
        "all_satisfied": all(
            report.get("all_satisfied") for report in reports
        ) if reports else True,
        "case_count": len(reports),
        "total": sum(int(report.get("total") or 0) for report in reports),
        "satisfied_count": sum(
            int(report.get("satisfied_count") or 0) for report in reports
        ),
        "unmet_count": sum(
            int(report.get("unmet_count") or 0) for report in reports
        ),
        "reports": reports,
        "judge": "unavailable" if any(report.get("judge") == "unavailable" for report in reports) else "model",
    }


def progressive_replay_decision(
    *,
    efficiency: Mapping[str, Any],
    baseline_checklist: Mapping[str, Any] | None = None,
    candidate_checklist: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Checklist completion decides first; efficiency decides successful ties."""
    policy = objective_replay_decision(efficiency=dict(efficiency))
    baseline = dict(baseline_checklist or {})
    candidate = dict(candidate_checklist or {})
    candidate_has_checklist = int(candidate.get("total") or 0) > 0
    baseline_has_checklist = int(baseline.get("total") or 0) > 0
    candidate_passed = bool(candidate.get("all_satisfied", True))
    baseline_passed = bool(baseline.get("all_satisfied", True))

    if baseline.get("judge") == "unavailable" or candidate.get("judge") == "unavailable":
        policy.update(
            accepted=False, verdict="inconclusive", no_regression=False,
            decision_basis="checklist_judge_unavailable", decisive_metrics=[],
        )
    elif candidate_has_checklist and baseline_has_checklist and not candidate_passed and not baseline_passed:
        policy.update(
            accepted=False, verdict="inconclusive", no_regression=False,
            decision_basis="both_checklists_incomplete", decisive_metrics=[],
        )
    elif candidate_has_checklist and not candidate_passed:
        policy.update(
            {
                "accepted": False,
                "verdict": "reject",
                "no_regression": False,
                "decision_basis": "candidate_checklist_incomplete",
                "primary_metric": "checklist_completion",
                "decisive_metrics": ["checklist_completion"],
            }
        )
    elif candidate_has_checklist and baseline_has_checklist and not baseline_passed:
        policy.update(
            {
                "accepted": True,
                "verdict": "accept",
                "no_regression": True,
                "decision_basis": "candidate_only_completed_checklist",
                "primary_metric": "checklist_completion",
                "decisive_metrics": ["checklist_completion"],
            }
        )
    if not (baseline_passed and candidate_passed):
        policy["metric_changes"] = {}
        policy["improved_metrics"] = []
        policy["regressed_metrics"] = []
        policy["unchanged_metrics"] = []
    policy["checklist"] = {
        "baseline": baseline,
        "candidate": candidate,
    }
    policy["policy"] = "progressive-checklist-then-turn-priority"
    return policy


def select_disclosure_items(
    *,
    checklist: list[dict[str, Any]],
    report: Mapping[str, Any],
    disclosed_ids: set[str],
    batch_size: int,
) -> list[dict[str, Any]]:
    """Deterministic selection; the simulator sees only the returned batch."""
    status = {
        str(item.get("id") or ""): bool(item.get("satisfied"))
        for item in report.get("items") or []
        if isinstance(item, Mapping)
    }
    undisclosed_unmet = [
        item
        for item in checklist
        if not status.get(str(item.get("id") or ""), False)
        and str(item.get("id") or "") not in disclosed_ids
    ]
    batch = undisclosed_unmet[: max(1, int(batch_size or 1))]
    if not batch:
        batch = [
            item
            for item in checklist
            if not status.get(str(item.get("id") or ""), False)
        ][: max(1, int(batch_size or 1))]
    return [dict(item) for item in batch]
