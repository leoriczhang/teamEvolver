"""Shared progressive-disclosure and checklist decision protocol."""

from __future__ import annotations

from typing import Any, Mapping

from .dataset_synthesizer import checklist_items, flatten_requirements
from .replay_metrics import objective_replay_decision


def normalize_case_checklist(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = [
        {
            "id": str(item.get("id") or f"C{index:02d}"),
            "text": str(item.get("text") or item.get("requirement") or "").strip(),
            "kind": str(item.get("kind") or "output"),
        }
        for index, item in enumerate(case.get("checklist") or [], start=1)
        if isinstance(item, Mapping)
        and str(item.get("text") or item.get("requirement") or "").strip()
    ]
    if explicit:
        return explicit
    return checklist_items(
        flatten_requirements(case.get("requirements")),
        flatten_requirements(case.get("trajectory_requirements")),
    )


def progressive_config(case: Mapping[str, Any]) -> dict[str, Any]:
    raw = (
        case.get("progressive_disclosure")
        if isinstance(case.get("progressive_disclosure"), Mapping)
        else {}
    )
    return {
        "enabled": bool(raw.get("enabled", True)),
        "initial_visibility": "query_only",
        "batch_size": max(1, int(raw.get("batch_size") or 4)),
        "stop_when": "all_checklist_items_satisfied",
    }


def initial_query(case: Mapping[str, Any]) -> str:
    return str(case.get("query") or case.get("instruction") or "").strip()


def select_replay_cases(
    cases: list[Any],
) -> list[tuple[str, int]]:
    """Run every progressive dataset case; retain one-per-window for legacy jobs."""
    progressive = any(
        isinstance(case, Mapping)
        and (
            bool(case.get("checklist"))
            or str(case.get("dataset_format") or "").startswith(
                "teamEvolver-progressive-test"
            )
        )
        for case in cases
    )
    selected: list[tuple[str, int]] = []
    seen_windows: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            continue
        window = str(case.get("evidence_window") or "recent")
        if window not in {"recent", "historical"}:
            window = "recent"
        if progressive:
            dataset_id = str(
                case.get("dataset_id") or case.get("case_id") or index
            )
            selected.append((f"{window}:{dataset_id}", index))
            continue
        if window in seen_windows:
            continue
        selected.append((window, index))
        seen_windows.add(window)
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
                    "satisfied": bool(actual.get("satisfied")),
                    "evidence": str(actual.get("evidence") or ""),
                }
            )
    else:
        items = [
            {
                "id": str(item.get("id") or ""),
                "text": str(item.get("text") or ""),
                "kind": str(item.get("kind") or "output"),
                "satisfied": bool(item.get("satisfied")),
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

    if candidate_has_checklist and not candidate_passed:
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
    policy["checklist"] = {
        "baseline": baseline,
        "candidate": candidate,
    }
    policy["policy"] = "progressive_checklist_then_turn_priority_v1"
    return policy


def next_disclosure_prompt(
    *,
    checklist: list[dict[str, Any]],
    report: Mapping[str, Any],
    disclosed_ids: set[str],
    round_number: int,
    batch_size: int,
) -> tuple[str, list[str]]:
    """Return the next hidden requirement batch and its ids."""
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
    ids = [str(item.get("id") or "") for item in batch]
    if not batch:
        return "", []
    lines = [
        f"第 {round_number} 轮 Checklist 检查仍有未满足项。",
        "保留已完成内容，只补齐以下要求：",
        *(
            f"{index}. [{item.get('id')}] {item.get('text')}"
            for index, item in enumerate(batch, start=1)
        ),
        "完成后重新检查现有产物并给出最新结果。",
    ]
    return "\n".join(lines), ids
