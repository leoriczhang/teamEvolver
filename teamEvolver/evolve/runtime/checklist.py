"""Compile reusable team evidence into an executable replay checklist."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

CHECKLIST_FORMAT = "common_checklist_v2"
MIN_COMMON_SUPPORT = 2

_MACHINE_MARKERS = (
    "assert",
    "parser",
    "machine",
    "data-",
    "token",
    "exact",
    "精确",
    "必须",
    "校验",
    "解析",
    "固定",
)
_ARTIFACT_MARKERS = (
    "artifact",
    "file",
    "html",
    "report",
    "deck",
    "产物",
    "文件",
    "报告",
    "演示",
)


def _item_id(prefix: str, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _source_ids(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    raw = value.get("supporting_session_ids")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item or "").strip() for item in raw if str(item or "").strip()))


def _claim(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("claim") or "").strip()
    return str(value or "").strip()


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


def _merge_checklist_union(
    checklist: dict[str, Any],
    inherited_checklists: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    inherited = [
        value for value in (inherited_checklists or []) if isinstance(value, dict)
    ]
    if not inherited:
        return checklist

    items = [
        dict(item)
        for item in checklist.get("items") or []
        if isinstance(item, dict)
    ]
    positions = {
        str(item.get("id") or ""): index
        for index, item in enumerate(items)
        if str(item.get("id") or "")
    }
    sources = [
        {
            "skill_name": "candidate_evidence",
            "version": None,
            "inherited": False,
            "required_item_ids": [
                str(item.get("id") or "")
                for item in items
                if item.get("required", True) and str(item.get("id") or "")
            ],
        }
    ]
    for context in inherited:
        inherited_checklist = (
            context.get("checklist")
            if isinstance(context.get("checklist"), dict)
            else {}
        )
        skill_name = str(context.get("skill_name") or "").strip()
        try:
            version = int(context.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        required_ids: list[str] = []
        for raw_item in inherited_checklist.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            if item.get("required", True):
                required_ids.append(item_id)
            item["inherited_from"] = {
                "skill_name": skill_name,
                "version": version or None,
            }
            existing_index = positions.get(item_id)
            if existing_index is None:
                positions[item_id] = len(items)
                items.append(item)
                continue
            existing = items[existing_index]
            existing["source_session_ids"] = _unique_strings(
                [
                    *list(existing.get("source_session_ids") or []),
                    *list(item.get("source_session_ids") or []),
                ]
            )
            if item.get("kind") == "hard":
                existing["kind"] = "hard"
                existing["evaluator"] = item.get("evaluator") or existing.get(
                    "evaluator"
                )
        sources.append(
            {
                "skill_name": skill_name,
                "version": version or None,
                "inherited": True,
                "required_item_ids": _unique_strings(required_ids),
                "source_job_id": str(context.get("job_id") or ""),
            }
        )

    checklist["items"] = items
    merge_context = (
        dict(checklist.get("merge_context"))
        if isinstance(checklist.get("merge_context"), dict)
        else {}
    )
    merge_context.update(
        {
            "required_union_regression_guard": True,
            "checklist_sources": sources,
            "union_item_count": len(items),
        }
    )
    checklist["merge_context"] = merge_context
    checklist["source_session_ids"] = _unique_strings(
        [
            *list(checklist.get("source_session_ids") or []),
            *[
                session_id
                for context in inherited
                for session_id in list(
                    (
                        context.get("checklist")
                        if isinstance(context.get("checklist"), dict)
                        else {}
                    ).get("source_session_ids")
                    or []
                )
            ],
        ]
    )
    return checklist


def compile_common_checklist(
    job: dict[str, Any] | None = None,
    *,
    action: str = "",
    evidence_classification: dict[str, Any] | None = None,
    session_evidence: list[dict[str, Any]] | None = None,
    replay_cases: list[dict[str, Any]] | None = None,
    dedup: dict[str, Any] | None = None,
    inherited_checklists: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a versioned checklist from cross-session team evidence.

    Claims require support from at least two independent sessions. Unsupported
    claims remain visible as provisional evidence but cannot justify publishing
    a shared skill. ``job`` is accepted for validation of legacy jobs; newly
    queued jobs use the explicit keyword arguments and persist the result.
    """
    if isinstance(job, dict):
        if isinstance(job.get("checklist"), dict):
            return job["checklist"]
        action = str(job.get("proposed_action") or job.get("action") or action)
        evidence_classification = (
            job.get("evidence_classification")
            if isinstance(job.get("evidence_classification"), dict)
            else evidence_classification
        )
        session_evidence = (
            job.get("session_evidence")
            if isinstance(job.get("session_evidence"), list)
            else session_evidence
        )
        replay_cases = (
            job.get("replay_cases")
            if isinstance(job.get("replay_cases"), list)
            else replay_cases
        )
        dedup = (
            job.get("dedup")
            if isinstance(job.get("dedup"), dict)
            else dedup
        )
        inherited_checklists = (
            job.get("inherited_checklists")
            if isinstance(job.get("inherited_checklists"), list)
            else inherited_checklists
        )
    classification = evidence_classification or {}
    evidence = [item for item in (session_evidence or []) if isinstance(item, dict)]
    cases = [item for item in (replay_cases or []) if isinstance(item, dict)]
    session_ids = list(
        dict.fromkeys(
            str(item.get("session_id") or "").strip()
            for item in [*evidence, *cases]
            if str(item.get("session_id") or "").strip()
        )
    )
    user_aliases = list(
        dict.fromkeys(
            str(item.get("user_alias") or "").strip()
            for item in evidence
            if str(item.get("user_alias") or "").strip()
        )
    )
    profiles: dict[str, list[str]] = {}
    for item in [*evidence, *cases]:
        profile = str(item.get("evaluation_profile") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        if profile and session_id:
            profiles.setdefault(profile, [])
            if session_id not in profiles[profile]:
                profiles[profile].append(session_id)
    controlled_profiles = {
        name: ids for name, ids in profiles.items() if len(ids) >= MIN_COMMON_SUPPORT
    }

    items: list[dict[str, Any]] = [
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

    team_claims = classification.get("team_skill")
    team_claims = team_claims if isinstance(team_claims, list) else []
    eligible_claims: list[dict[str, Any]] = []
    provisional_claims: list[dict[str, Any]] = []
    artifact_expected = bool(controlled_profiles)
    for value in team_claims:
        claim = _claim(value)
        if not claim:
            continue
        sources = _source_ids(value)
        row = {
            "claim": claim,
            "source_session_ids": sources,
            "causal_link": (
                str(value.get("causal_link") or "").strip()
                if isinstance(value, dict)
                else ""
            ),
            "support_count": len(sources),
        }
        if any(marker in claim.lower() for marker in _ARTIFACT_MARKERS):
            artifact_expected = True
        if len(sources) >= MIN_COMMON_SUPPORT:
            eligible_claims.append(row)
        else:
            provisional_claims.append(row)

    # Older controlled-evaluation jobs may not contain structured team claims.
    # Their changed sections are still cross-session evidence, not a reason to
    # fail commonality before the candidate can be replayed.
    if not eligible_claims and controlled_profiles and isinstance(job, dict):
        candidate = (
            job.get("candidate_skill")
            if isinstance(job.get("candidate_skill"), dict)
            else {}
        )
        edit_summary = (
            candidate.get("edit_summary")
            if isinstance(candidate.get("edit_summary"), dict)
            else {}
        )
        profile_sources = list(
            dict.fromkeys(
                session_id
                for session_ids in controlled_profiles.values()
                for session_id in session_ids
            )
        )
        for value in edit_summary.get("changed_sections") or []:
            claim = str(value or "").strip()
            if claim:
                eligible_claims.append(
                    {
                        "claim": claim,
                        "source_session_ids": profile_sources,
                        "causal_link": str(edit_summary.get("notes") or ""),
                        "support_count": len(profile_sources),
                    }
                )

    if artifact_expected:
        items.extend(
            [
                {
                    "id": "artifact_contract",
                    "claim": "The real artifact must pass its deterministic profile checks.",
                    "kind": "hard",
                    "evaluator": "artifact_contract",
                    "required": True,
                    "scope": "all_cases",
                    "source_session_ids": session_ids,
                },
                {
                    "id": "post_write_validation",
                    "claim": "A successful validation ToolResult must follow the final artifact mutation.",
                    "kind": "hard",
                    "evaluator": "post_write_validation",
                    "required": True,
                    "scope": "all_cases",
                    "source_session_ids": session_ids,
                },
            ]
        )

    for row in eligible_claims:
        claim = row["claim"]
        lower = claim.lower()
        machine_checkable = bool(controlled_profiles) and any(
            marker in lower for marker in _MACHINE_MARKERS
        )
        items.append(
            {
                "id": _item_id("common", claim),
                "claim": claim,
                "kind": "hard" if machine_checkable else "soft",
                "evaluator": "artifact_contract" if machine_checkable else "llm_checklist",
                "required": True,
                "scope": "source_sessions",
                "source_session_ids": row["source_session_ids"],
                "causal_link": row["causal_link"],
                "support_count": row["support_count"],
            }
        )

    merge_context = {}
    if action == "merge_skill":
        merge_context = {
            "source_skill": str((dedup or {}).get("most_similar_skill") or ""),
            "similarity": (dedup or {}).get("similarity"),
            "required_union_regression_guard": True,
        }
    commonality_pass = bool(eligible_claims or controlled_profiles)
    checklist = {
        "format": CHECKLIST_FORMAT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "minimum_common_support": MIN_COMMON_SUPPORT,
        "source_session_ids": session_ids,
        "source_user_aliases": user_aliases,
        "controlled_profiles": controlled_profiles,
        "commonality": {
            "passed": commonality_pass,
            "eligible_claim_count": len(eligible_claims),
            "provisional_claim_count": len(provisional_claims),
            "distinct_session_count": len(session_ids),
            "distinct_user_count": len(user_aliases),
        },
        "items": items,
        "provisional_claims": provisional_claims,
        "excluded_personal_evidence": classification.get("user_memory") or [],
        "merge_context": merge_context,
    }
    return _merge_checklist_union(checklist, inherited_checklists)


def scope_checklist_for_case(
    checklist: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    """Limit source-bound requirements to the replay case that proves them."""
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
    merge_context = (
        dict(checklist.get("merge_context"))
        if isinstance(checklist.get("merge_context"), dict)
        else {}
    )
    present = {
        str(item.get("id") or "")
        for item in items
        if str(item.get("id") or "")
    }
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
    gap = (
        branch.get("artifact_gap_report")
        if isinstance(branch.get("artifact_gap_report"), dict)
        else {}
    )
    post_write = bool(branch.get("post_write_validation_passed"))
    if not post_write:
        interactions = [
            item
            for item in branch.get("interactions") or []
            if isinstance(item, dict)
        ]
        post_write = bool(
            interactions
            and interactions[-1].get("post_write_validation_passed")
        )
    rows = []
    for item in checklist.get("items") or []:
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
    return {
        "passed": bool(rows) and all(item["passed"] for item in rows),
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


def aggregate_branch_checklist_results(
    checklist: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold case-scoped results back into the complete checklist union."""
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
    dimensions = efficiency.get("dimensions") or {}
    turn_gain = float(
        (dimensions.get("interaction_turns") or {}).get("reduction_ratio") or 0.0
    )
    efficiency_score = float(efficiency.get("score") or 0.0)
    coverage_gain = round(
        float(candidate.get("pass_rate") or 0.0)
        - float(baseline.get("pass_rate") or 0.0),
        4,
    )
    commonality = bool((checklist.get("commonality") or {}).get("passed"))
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
    objective_gain = coverage_gain > 0 or (
        turn_gain > 0 and efficiency_score >= 0.05
    )
    accepted = (
        commonality
        and hard_gate
        and checklist_gate
        and no_regression
        and merge_union_pass
        and objective_gain
    )
    baseline_hard_gate = bool(baseline.get("hard_pass"))
    definitive_regression = (
        bool(regressed_item_ids)
        or not merge_union_pass
        or (baseline_hard_gate and not hard_gate)
    )
    if accepted:
        verdict = "accept"
    elif not commonality:
        verdict = "reject"
    elif definitive_regression:
        verdict = "reject"
    else:
        verdict = "inconclusive"
    reason_codes: list[str] = []
    if not commonality:
        reason_codes.append("commonality_gate_failed")
    if not hard_gate or not checklist_gate:
        reason_codes.append("checklist_gate_failed")
    if regressed_item_ids:
        reason_codes.append("checklist_items_regressed")
    if not merge_union_pass:
        reason_codes.append("merge_union_failed")
    if not objective_gain:
        reason_codes.append("no_objective_gain")
    if turn_gain < 0:
        reason_codes.append("interaction_turns_regressed")
    return {
        "accepted": bool(accepted),
        "verdict": verdict,
        "policy": "checklist_efficiency_v1",
        "quality_gate": hard_gate and checklist_gate,
        "commonality_pass": commonality,
        "no_regression": no_regression,
        "regressed_item_ids": regressed_item_ids,
        "merge_union_pass": merge_union_pass,
        "merge_source_results": merge_source_results,
        "coverage_gain": coverage_gain,
        "turn_gain": round(turn_gain, 4),
        "efficiency_score": round(efficiency_score, 4),
        "reason_codes": reason_codes,
    }
