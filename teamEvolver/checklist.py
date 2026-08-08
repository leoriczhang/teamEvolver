"""Replay checklist helpers shared by validation and the dashboard."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

CHECKLIST_FORMAT = "common_checklist_v2"
MIN_COMMON_SUPPORT = 2


def _claim(value: Any) -> str:
    return (
        str(value.get("claim") or "").strip()
        if isinstance(value, dict)
        else str(value or "").strip()
    )


def _sources(value: Any) -> list[str]:
    raw = value.get("supporting_session_ids") if isinstance(value, dict) else []
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (raw if isinstance(raw, list) else [])
            if str(item or "").strip()
        )
    )


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
    )


def compile_common_checklist(job: dict[str, Any]) -> dict[str, Any]:
    """Compile old jobs on read; new jobs persist the same structure at queue time."""
    if isinstance(job.get("checklist"), dict):
        return job["checklist"]
    evidence = job.get("evidence_classification")
    evidence = evidence if isinstance(evidence, dict) else {}
    session_evidence = [
        item for item in job.get("session_evidence") or [] if isinstance(item, dict)
    ]
    cases = [item for item in job.get("replay_cases") or [] if isinstance(item, dict)]
    session_ids = list(
        dict.fromkeys(
            str(item.get("session_id") or "").strip()
            for item in [*session_evidence, *cases]
            if str(item.get("session_id") or "").strip()
        )
    )
    profiles: dict[str, list[str]] = {}
    for item in [*session_evidence, *cases]:
        profile = str(item.get("evaluation_profile") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        if profile and session_id:
            profiles.setdefault(profile, [])
            if session_id not in profiles[profile]:
                profiles[profile].append(session_id)
    controlled_profiles = {
        name: ids for name, ids in profiles.items() if len(ids) >= MIN_COMMON_SUPPORT
    }
    items = [
        {
            "id": "execution_complete",
            "claim": "The branch must complete through real tool execution.",
            "kind": "hard",
            "evaluator": "branch_ok",
            "required": True,
            "scope": "all_cases",
            "source_session_ids": session_ids,
        },
        {
            "id": "clean_final_response",
            "claim": "The final response must not report a tool or model failure.",
            "kind": "hard",
            "evaluator": "clean_final_response",
            "required": True,
            "scope": "all_cases",
            "source_session_ids": session_ids,
        },
    ]
    if controlled_profiles:
        items.extend(
            [
                {
                    "id": "artifact_contract",
                    "claim": "The real artifact must pass deterministic profile checks.",
                    "kind": "hard",
                    "evaluator": "artifact_contract",
                    "required": True,
                    "scope": "all_cases",
                    "source_session_ids": session_ids,
                },
                {
                    "id": "post_write_validation",
                    "claim": "A validation ToolResult must follow the last artifact mutation.",
                    "kind": "hard",
                    "evaluator": "post_write_validation",
                    "required": True,
                    "scope": "all_cases",
                    "source_session_ids": session_ids,
                },
            ]
        )
    eligible = []
    provisional = []
    for raw in evidence.get("team_skill") or []:
        claim = _claim(raw)
        if not claim:
            continue
        sources = _sources(raw)
        row = {
            "claim": claim,
            "source_session_ids": sources,
            "support_count": len(sources),
            "causal_link": (
                str(raw.get("causal_link") or "")
                if isinstance(raw, dict)
                else ""
            ),
        }
        (eligible if len(sources) >= MIN_COMMON_SUPPORT else provisional).append(row)
    if not eligible and controlled_profiles:
        profile_sources = list(
            dict.fromkeys(
                session_id
                for ids in controlled_profiles.values()
                for session_id in ids
            )
        )
        candidate = job.get("candidate_skill")
        candidate = candidate if isinstance(candidate, dict) else {}
        edit_summary = (
            candidate.get("edit_summary")
            if isinstance(candidate.get("edit_summary"), dict)
            else {}
        )
        for value in edit_summary.get("changed_sections") or []:
            claim = str(value or "").strip()
            if claim:
                eligible.append(
                    {
                        "claim": claim,
                        "source_session_ids": profile_sources,
                        "support_count": len(profile_sources),
                        "causal_link": str(edit_summary.get("notes") or ""),
                    }
                )
    for row in eligible:
        claim = row["claim"]
        items.append(
            {
                "id": "common_" + hashlib.sha1(claim.encode()).hexdigest()[:10],
                "claim": claim,
                "kind": "soft",
                "evaluator": "llm_checklist",
                "required": True,
                "scope": "source_sessions",
                **row,
            }
        )
    action = str(job.get("proposed_action") or "")
    return {
        "format": CHECKLIST_FORMAT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "minimum_common_support": MIN_COMMON_SUPPORT,
        "source_session_ids": session_ids,
        "controlled_profiles": controlled_profiles,
        "commonality": {
            "passed": bool(eligible or controlled_profiles),
            "eligible_claim_count": len(eligible),
            "provisional_claim_count": len(provisional),
            "distinct_session_count": len(session_ids),
        },
        "items": items,
        "provisional_claims": provisional,
        "excluded_personal_evidence": evidence.get("user_memory") or [],
        "merge_context": job.get("merge_context") or {},
    }


def scope_checklist_for_case(
    checklist: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    """Return only requirements supported by the replay case's evidence."""
    session_id = str(case.get("session_id") or "").strip()
    scoped = dict(checklist)
    items = []
    for raw_item in checklist.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        sources = _unique_strings(list(raw_item.get("source_session_ids") or []))
        if (
            raw_item.get("scope") == "source_sessions"
            and session_id
            and sources
            and session_id not in sources
        ):
            continue
        items.append(dict(raw_item))
    scoped["items"] = items
    present = {
        str(item.get("id") or "")
        for item in items
        if str(item.get("id") or "")
    }
    merge_context = (
        dict(checklist.get("merge_context"))
        if isinstance(checklist.get("merge_context"), dict)
        else {}
    )
    merge_context["checklist_sources"] = [
        {
            **source,
            "required_item_ids": [
                item_id
                for item_id in _unique_strings(source.get("required_item_ids"))
                if item_id in present
            ],
        }
        for source in merge_context.get("checklist_sources") or []
        if isinstance(source, dict)
    ]
    scoped["merge_context"] = merge_context
    scoped["scoped_for"] = {
        "session_id": session_id,
        "evidence_window": str(case.get("evidence_window") or "recent"),
    }
    return scoped


def reply_reports_failure(reply: str) -> bool:
    text = str(reply or "").strip()
    lower = text.lower()
    return (
        text.startswith(("工具调用失败", "模型调用失败"))
        or "bash_exit_nonzero" in lower
        or "llm_error" in lower
        or lower.startswith("tool call failed")
    )


def evaluate_branch_checklist(
    checklist: dict[str, Any],
    branch: dict[str, Any],
    soft_results: dict[str, bool] | None = None,
) -> dict[str, Any]:
    soft_results = soft_results or {}
    gap = branch.get("artifact_gap_report")
    gap = gap if isinstance(gap, dict) else {}
    post_write = bool(branch.get("post_write_validation_passed"))
    if not post_write:
        interactions = [
            item for item in branch.get("interactions") or [] if isinstance(item, dict)
        ]
        post_write = bool(
            interactions and interactions[-1].get("post_write_validation_passed")
        )
    rows = []
    for item in checklist.get("items") or []:
        if not isinstance(item, dict):
            continue
        evaluator = str(item.get("evaluator") or "")
        if evaluator == "branch_ok":
            passed = bool(branch.get("ok"))
        elif evaluator == "clean_final_response":
            passed = not reply_reports_failure(str(branch.get("final_response") or ""))
        elif evaluator == "artifact_contract":
            passed = bool(gap.get("passed"))
        elif evaluator == "post_write_validation":
            passed = post_write
        else:
            passed = bool(soft_results.get(str(item.get("id") or "")))
        rows.append({**item, "passed": passed})
    hard = [item for item in rows if item.get("kind") == "hard"]
    soft = [item for item in rows if item.get("kind") == "soft"]
    passed_count = sum(1 for item in rows if item["passed"])
    return {
        "passed": bool(rows) and all(item["passed"] for item in rows),
        "hard_pass": all(item["passed"] for item in hard),
        "pass_rate": round(passed_count / max(1, len(rows)), 4),
        "hard_pass_rate": round(
            sum(1 for item in hard if item["passed"]) / max(1, len(hard)), 4
        ),
        "soft_pass_rate": round(
            sum(1 for item in soft if item["passed"]) / max(1, len(soft)), 4
        ),
        "items": rows,
    }


def aggregate_branch_checklist_results(
    checklist: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold case-scoped checklist results back into the complete union."""
    observed: dict[str, list[bool]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id:
                observed.setdefault(item_id, []).append(bool(item.get("passed")))
    rows = []
    for raw_item in checklist.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        item_id = str(raw_item.get("id") or "")
        checks = observed.get(item_id, [])
        rows.append(
            {
                **raw_item,
                "passed": bool(checks) and all(checks),
                "observed_case_count": len(checks),
            }
        )
    hard = [item for item in rows if item.get("kind") == "hard"]
    soft = [item for item in rows if item.get("kind") == "soft"]
    return {
        "passed": bool(rows)
        and all(
            item["passed"]
            for item in rows
            if item.get("required", True)
        ),
        "hard_pass": all(item["passed"] for item in hard),
        "pass_rate": round(
            sum(1 for item in rows if item["passed"]) / max(1, len(rows)),
            4,
        ),
        "hard_pass_rate": round(
            sum(1 for item in hard if item["passed"]) / max(1, len(hard)),
            4,
        ),
        "soft_pass_rate": round(
            sum(1 for item in soft if item["passed"]) / max(1, len(soft)),
            4,
        ),
        "items": rows,
    }


def objective_replay_decision(
    *,
    checklist: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    efficiency: dict[str, Any],
) -> dict[str, Any]:
    """Decide publication from checklist quality and objective execution cost."""
    dimensions = (
        efficiency.get("dimensions")
        if isinstance(efficiency.get("dimensions"), dict)
        else {}
    )
    turn_gain = float(
        (dimensions.get("interaction_turns") or {}).get("reduction_ratio") or 0.0
    )
    efficiency_score = float(efficiency.get("score") or 0.0)
    coverage_gain = round(
        float(candidate.get("pass_rate") or 0.0)
        - float(baseline.get("pass_rate") or 0.0),
        4,
    )
    commonality_pass = bool((checklist.get("commonality") or {}).get("passed"))
    hard_gate = bool(candidate.get("hard_pass"))
    checklist_gate = bool(candidate.get("passed"))
    baseline_items = {
        str(item.get("id") or ""): bool(item.get("passed"))
        for item in baseline.get("items") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    candidate_items = {
        str(item.get("id") or ""): bool(item.get("passed"))
        for item in candidate.get("items") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    regressed_item_ids = [
        item_id
        for item_id, passed in baseline_items.items()
        if passed and not candidate_items.get(item_id, False)
    ]
    no_regression = not regressed_item_ids
    merge_sources = [
        source
        for source in (checklist.get("merge_context") or {}).get(
            "checklist_sources"
        )
        or []
        if isinstance(source, dict)
    ]
    merge_source_results = []
    for source in merge_sources:
        required_ids = _unique_strings(source.get("required_item_ids"))
        failed_ids = [
            item_id
            for item_id in required_ids
            if not candidate_items.get(item_id, False)
        ]
        merge_source_results.append(
            {
                "skill_name": source.get("skill_name"),
                "version": source.get("version"),
                "inherited": bool(source.get("inherited")),
                "required_item_ids": required_ids,
                "failed_item_ids": failed_ids,
                "passed": not failed_ids,
            }
        )
    merge_union_pass = all(
        result["passed"] for result in merge_source_results
    )
    tool_gain = float(
        (dimensions.get("tool_call_count") or {}).get("reduction_ratio") or 0.0
    )
    token_gain = float(
        (dimensions.get("total_tokens") or {}).get("reduction_ratio") or 0.0
    )
    if turn_gain > 0:
        efficiency_gain = True
    elif turn_gain == 0 and (tool_gain > 0 or token_gain > 0):
        efficiency_gain = True
    else:
        efficiency_gain = False
    quality_gate = hard_gate and checklist_gate
    accepted = (
        commonality_pass
        and quality_gate
        and no_regression
        and merge_union_pass
        and efficiency_gain
    )
    reasons = []
    if not commonality_pass:
        reasons.append("commonality_gate_failed")
    if not quality_gate:
        reasons.append("checklist_gate_failed")
    if not no_regression:
        reasons.append("checklist_items_regressed")
    if not merge_union_pass:
        reasons.append("merge_union_failed")
    if not efficiency_gain:
        if turn_gain < 0:
            reasons.append("interaction_turns_regressed")
        elif turn_gain == 0 and tool_gain <= 0 and token_gain <= 0:
            reasons.append("no_efficiency_gain_on_any_dimension")
        else:
            reasons.append("no_efficiency_gain")
    return {
        "accepted": accepted,
        "policy": "checklist_efficiency_v1",
        "quality_gate": quality_gate,
        "commonality_pass": commonality_pass,
        "no_regression": no_regression,
        "regressed_item_ids": regressed_item_ids,
        "merge_union_pass": merge_union_pass,
        "merge_source_results": merge_source_results,
        "coverage_gain": coverage_gain,
        "turn_gain": round(turn_gain, 4),
        "efficiency_score": round(efficiency_score, 4),
        "reason_codes": reasons,
    }
