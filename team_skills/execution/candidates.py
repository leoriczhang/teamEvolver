"""Candidate-scoped frozen component changes and conservative publication gates."""
from __future__ import annotations

import copy

from team_skills.library.repository import SkillLibraryError

from .contracts import apply_changes
from .holdout import enhanced_validation


def reconcile_scope(detail, *, skill_changed):
    """Derive treatment permissions from actual bytes after every edit and at acceptance."""
    result = copy.deepcopy(detail)
    if not result.get("candidate_execution_plan"):
        return result
    allowed = result.get("allowed_modules", ["skill"])
    if skill_changed and "skill" not in allowed:
        raise SkillLibraryError("scope_violation: Skill was not selected")
    before = {p["component_id"]: p for p in result["baseline_execution_plan"]["components"]}
    targets = sorted(p["component_id"] for p in result["candidate_execution_plan"]["components"]
                     if p["content_hash"] != before[p["component_id"]]["content_hash"])
    if targets and "prompt" not in allowed:
        raise SkillLibraryError("scope_violation: Prompt was not selected")
    for key in ("baseline_execution_plan", "candidate_execution_plan"):
        result[key]["binding"]["allow_skill_change"] = bool(skill_changed)
        result[key]["target_component_ids"] = targets
    result["skill_changed"] = bool(skill_changed)
    result["actual_modules"] = (["skill"] if skill_changed else []) + (["prompt"] if targets else [])
    return result


def revise_execution_detail(detail, changes):
    result = copy.deepcopy(detail)
    baseline = result.get("baseline_execution_plan")
    if not baseline:
        raise SkillLibraryError("candidate has no frozen business execution baseline")
    known = set(result.get("session_ids") or [])
    for session in result.get("session_evidence") or []:
        if session.get("session_id"):
            known.add(session["session_id"])
        known.update(ref["trace_id"] for ref in (session.get("provenance") or {}).get("target_trace_refs", []))
    for change in result.get("component_changes") or []:
        known.update(change.get("evidence_refs") or [])
    # At initial proposal the writer already validates raw refs; later human edits
    # may retain those refs or cite other frozen targets, never invent evidence.
    if known and isinstance(changes, list) and any(
        isinstance(change, dict) and isinstance(change.get("evidence_refs"), list)
        and any(not isinstance(ref, str) or ref not in known for ref in change["evidence_refs"])
        for change in changes
    ):
        raise SkillLibraryError("component edit references evidence outside the frozen candidate")
    candidate = apply_changes(baseline, changes, allowed_modules=result.get("allowed_modules", ["skill"]))
    baseline["target_component_ids"] = candidate["target_component_ids"]
    result.update(baseline_execution_plan=baseline, candidate_execution_plan=candidate, component_changes=changes)
    actual = ["prompt"]
    if result.get("skill_changed"):
        actual.insert(0, "skill")
    result["actual_modules"] = actual
    result.pop("independent_validation", None)
    return result


def publication_gate(detail, evaluation, *, candidate_revision=None, validation_run_id=None,
                     validation_payload_hash=None, validation_result_hash=None):
    """Additional evidence never weakens the existing Skill Replay policy."""
    if not detail.get("candidate_execution_plan"):
        return {"passed": True, "reasons": []}
    reasons = []
    if not detail.get("actual_modules"):
        reasons.append("no_component_changes")
    receipt_gate = evaluation.get("execution_gate") or {}
    if receipt_gate.get("status") != "passed":
        reasons.append("execution_not_observed")
    required = set(detail.get("affected_skill_ids") or [])
    covered = {name for case in detail.get("replay_cases", []) for name in case.get("skill_ids", [])}
    if not required <= covered:
        reasons.append("regression_coverage_missing")
    plan = detail.get("validation_plan") or {}
    if not enhanced_validation(detail):
        if evaluation.get("verdict") != "accept":
            reasons.append("main_replay_not_accepted")
        if not plan.get("input_hash"):
            reasons.append("validation_inputs_missing")
        if ((plan.get("probe_policy") or {}).get("max_total_tokens") is not None
                and (evaluation.get("cost_gate") or {}).get("status") != "passed"):
            reasons.append("validation_budget_unverified")
        return {"passed": not reasons, "reasons": reasons}
    if plan and (plan.get("holdout") or {}).get("status") != "ready":
        reasons.extend((plan.get("holdout") or {}).get("reasons") or ["locked_acceptance_missing"])
    if plan and (evaluation.get("probe_gate") or {}).get("status") != "passed":
        reasons.append("execution_probes_incomplete")
    if plan and (evaluation.get("cost_gate") or {}).get("status") != "passed":
        reasons.append("total_model_cost_unverified")
    independent = detail.get("independent_validation") or {}
    if (independent.get("schema_version") != "teamevolver.independent-validation.v2"
            or independent.get("status") != "passed" or not independent.get("evidence_ref")
            or independent.get("candidate_revision") != candidate_revision):
        reasons.append("independent_validation_missing")
    trusted = {"validation_run_id": validation_run_id, "validation_payload_hash": validation_payload_hash,
               "validation_result_hash": validation_result_hash}
    if any(not value or independent.get(key) != value for key, value in trusted.items()):
        reasons.append("independent_validation_result_changed")
    if (not plan.get("input_hash") or independent.get("validation_input_hash") != plan.get("input_hash")
            or not detail.get("replay_cases") or not {c["case_id"] for c in detail.get("replay_cases", [])}
            <= set(independent.get("case_ids") or [])):
        reasons.append("independent_validation_inputs_changed")
    return {"passed": not reasons, "reasons": reasons}
