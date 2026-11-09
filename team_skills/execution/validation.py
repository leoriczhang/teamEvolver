"""Freeze cross-Skill regression and verify every observable execution branch."""
from __future__ import annotations

import copy

from team_replay.datasets.candidate_selection import attach_dataset
from team_replay.datasets.repository import DatasetRepository
from team_replay.execution_inputs import case_execution_plan_pair, normalize_execution_inputs
from team_replay.execution_plan import execution_plan_hash, validate_execution_plan_pair
from team_skills.evolution.history import skill_payload
from team_skills.evolution.repository import digest
from team_skills.library.repository import SkillLibraryError, SkillLibraryRepository

from .holdout import ENHANCED_VALIDATION, candidate_validation_policy, freeze_holdout
from .repository import ExecutionRepository, ExecutionVersionConflict


async def freeze_execution_validation(conn, tx, detail, *, tenant_id="", candidate_id="", candidate_revision=0):
    if not detail.get("candidate_execution_plan"):
        return detail
    result = copy.deepcopy(detail)
    left, right = validate_execution_plan_pair(result["baseline_execution_plan"], result["candidate_execution_plan"])
    if right["target_component_ids"] and "prompt" not in result.get("allowed_modules", ["skill"]):
        raise SkillLibraryError("scope_violation: Prompt was not selected")
    profile, _ = await ExecutionRepository(tx).current(conn, result["execution_profile_id"])
    if result.get("candidate_kind") == "execution" and (
        profile["affected_skill_ids"] or result.get("affected_skill_ids") or result.get("skill_dependencies")
        or result.get("skill_changed")
    ):
        raise SkillLibraryError("Prompt-only Candidate cannot acquire Skill dependencies")
    if profile["current_release_id"] != result["base_release_id"]:
        raise ExecutionVersionConflict("execution baseline changed before validation")
    result.update(baseline_execution_plan=left, candidate_execution_plan=right)
    dataset_id = (result.get("replay_sampling") or {}).get("current_dataset_id")
    if dataset_id:
        datasets = DatasetRepository(tx)
        covered = {s for case in result["replay_cases"] for s in case.get("skill_ids", [])}
        for name in sorted(set(result["affected_skill_ids"]) - covered):
            rows = await datasets.historical_cases(conn, skill_id=name, before_dataset_id=dataset_id)
            # Explicit bounded coverage, not an unbounded model or file-processing loop.
            for row in rows[:3]:
                case = copy.deepcopy(row["case"])
                case["materials"] = [await datasets.material(conn, item) for item in case.get("materials", [])]
                result["replay_cases"].append(attach_dataset(case, row, selection="historical"))
    dependencies = []
    library = SkillLibraryRepository(tx)
    for dependency in result["skill_dependencies"]:
        saved = await library.get_version(conn, dependency["name"], dependency["version"])
        if not saved or saved["tree_sha256"] != dependency["tree_sha256"]:
            raise ExecutionVersionConflict("frozen Skill dependency is unavailable")
        bundle = await library.read_version_bundle(conn, dependency["name"], dependency["version"])
        dependencies.append(skill_payload(dependency["name"], bundle))
    result["dependency_treatments"] = dependencies
    policy = candidate_validation_policy(result)
    enhanced = policy["strategy"] == ENHANCED_VALIDATION
    if (not enhanced and result.get("validation_budget") is None
            and "max_total_tokens" not in (right["environment"].get("validation") or {})):
        # The old implicit ceiling is not a new standard publication requirement.
        # Keep the Agent environment and generation-authorization baseline unchanged.
        policy = {**policy, "max_total_tokens": None}
    if enhanced:
        holdout, cases = await freeze_holdout(
            conn, tx, tenant_id=tenant_id, detail=result,
            candidate_id=candidate_id, candidate_revision=candidate_revision,
        )
    else:
        holdout, cases = {"status": "not_required", "policy": policy}, []
    result["replay_cases"].extend(cases)
    result["validation_plan"] = {
        "schema_version": "teamevolver.execution-validation.v1",
        "validation_strategy": policy["strategy"],
        "baseline_plan_hash": execution_plan_hash(left), "candidate_plan_hash": execution_plan_hash(right),
        "required_skill_ids": result["affected_skill_ids"],
        "case_ids": [case["case_id"] for case in result["replay_cases"]],
        "attribution": "combination_only" if len(result.get("actual_modules", [])) > 1 else "single_module",
        "independent_verification_required": enhanced,
        "holdout": holdout,
        "probe_policy": policy,
    }
    if left["environment"].get("template_input_contract"):
        case_hashes = {}
        for case in result["replay_cases"]:
            try:
                before, after = case_execution_plan_pair(left, right, case)
            except (TypeError, ValueError) as exc:
                raise SkillLibraryError("invalid Case execution inputs: " + str(exc)) from exc
            identity = case["case_id"]
            if identity in case_hashes:
                raise SkillLibraryError("duplicate Case identity in execution input bindings")
            case_hashes[identity] = {
                "baseline": execution_plan_hash(before), "candidate": execution_plan_hash(after),
                "execution_inputs_hash": digest(normalize_execution_inputs(
                    case["provenance"]["execution_inputs"],
                )),
            }
        result["validation_plan"]["case_plan_hashes"] = case_hashes
    return result


def execution_gate(evaluation, detail):
    if not detail.get("candidate_execution_plan"):
        return {"status": "not_required"}
    pairs = evaluation.get("cases") or []
    required = {case["case_id"] for case in detail.get("replay_cases", [])}
    single = evaluation.get("case_id") if len(pairs) == 1 else None
    observed_ids = {pair.get("case_id") or single for pair in pairs}
    errors = []
    if not required or not required <= observed_ids:
        errors.append("missing_execution_cases")
    bound_inputs = bool(detail["baseline_execution_plan"]["environment"].get("template_input_contract"))
    frozen_hashes = (detail.get("validation_plan") or {}).get("case_plan_hashes") or {}
    cases = {case["case_id"]: case for case in detail.get("replay_cases", [])}
    if bound_inputs and (set(frozen_hashes) != required or len(pairs) != len(required) or observed_ids != required):
        errors.append("execution_input_case_bindings_missing")
    for pair in pairs:
        case_id = pair.get("case_id") or single
        expected = {}
        if bound_inputs:
            try:
                before, after = case_execution_plan_pair(
                    detail["baseline_execution_plan"], detail["candidate_execution_plan"], cases[case_id],
                )
                expected = {"baseline": execution_plan_hash(before), "candidate": execution_plan_hash(after),
                            "execution_inputs_hash": digest(normalize_execution_inputs(
                                cases[case_id]["provenance"]["execution_inputs"],
                            ))}
                if expected != frozen_hashes.get(case_id):
                    errors.append(f"execution_input_binding_changed:{case_id}")
            except (KeyError, TypeError, ValueError):
                errors.append(f"invalid_execution_input_binding:{case_id}")
        for branch, key in (("baseline", "baseline_execution_plan"), ("candidate", "candidate_execution_plan")):
            observed = pair.get(branch) or {}
            if (observed.get("execution_verification") != "verified" or not observed.get("observed_requests")
                    or observed.get("execution_plan_hash") != (
                        expected.get(branch) if bound_inputs else execution_plan_hash(detail[key]))):
                errors.append(f"unverified_{branch}:{pair.get('case_id', '')}")
    return {"status": "passed" if not errors else "inconclusive", "reasons": errors}


def apply_peer_treatments(job, detail, skill_name):
    peers = detail.get("dependency_treatments")
    if not peers:
        return job
    before, after = [], []
    for peer in peers:
        if peer["name"] == skill_name:
            before.append(job["current_skill"])
            after.append(job["candidate_skill"])
        else:
            before.append(copy.deepcopy(peer))
            after.append(copy.deepcopy(peer))
    job.update(current_skill={"name": skill_name, "skills": before},
               candidate_skill={"name": skill_name, "skills": after},
               skill_ids=detail["affected_skill_ids"])
    return job
