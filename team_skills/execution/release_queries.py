"""Read immutable release content and its frozen validation evidence, never current Candidate state."""
from __future__ import annotations

import copy

from team_replay.execution_plan import ExecutionPlanError, normalize_execution_plan
from team_skills.evolution.repository import EvolutionRepository, digest
from team_skills.library.bundle import bundle_tree_sha256, decode_bundle_payload
from team_skills.library.repository import SkillLibraryError, SkillNotFound

from .repository import ExecutionRepository


def _pick(value, fields):
    return {key: copy.deepcopy(value[key]) for key in fields if key in value}


def _cost(value):
    result = _pick(value, (
        "scope", "status", "reasons", "warnings", "total_tokens", "known_total_tokens", "budget_tokens",
        "accounting_status", "agent_cost_complete", "agent_replay_tokens", "prior_validation_runs",
        "replay_te_llm_accounting_complete",
    ))
    usage = value.get("te_llm_usage")
    if isinstance(usage, dict):
        result["te_llm_usage"] = _pick(usage, (
            "complete", "total_tokens", "known_total_tokens", "unknown_attempts", "model_attempts",
            "input_tokens", "output_tokens",
        ))
    return result


def _checklist(value):
    result = _pick(value, ("judge", "total", "case_count", "unmet_count", "all_satisfied", "satisfied_count"))
    reports = value.get("reports")
    if "case_count" not in result and isinstance(reports, list):
        result["case_count"] = len(reports)
    complete = (isinstance(reports, list) and len(reports) == result.get("case_count")
                and all(isinstance(report, dict) and type(report.get("all_satisfied")) is bool for report in reports))
    result["completed_count"] = sum(report["all_satisfied"] for report in reports) if complete else None
    return result


def evaluation_summary(evaluation):
    """Copy recorded aggregates; omit conversations, per-Case reports and probe result bodies."""
    result = _pick(evaluation, (
        "status", "candidate_revision", "validation_run_id", "validation_verdict", "recommended_publish",
        "static_validation", "execution_gate", "publication_gate",
    ))
    if isinstance(evaluation.get("cost_gate"), dict):
        result["cost_gate"] = _cost(evaluation["cost_gate"])
    replay = evaluation.get("replay")
    if not isinstance(replay, dict):
        return result
    summary = _pick(replay, (
        "status", "verdict", "accepted", "case_count", "total", "completed", "succeeded", "failed",
        "efficiency", "execution_gate", "evaluation_policy_hash",
    ))
    if isinstance(replay.get("checklist"), dict):
        summary["checklist"] = {
            side: _checklist(value)
            for side, value in replay["checklist"].items() if isinstance(value, dict)
        }
    if isinstance(replay.get("decision_policy"), dict):
        summary["decision_policy"] = _pick(replay["decision_policy"], (
            "policy", "verdict", "accepted", "decision_basis", "metric_changes", "primary_metric",
            "decisive_metrics", "improved_metrics", "regressed_metrics", "secondary_metrics",
            "unchanged_metrics", "unavailable_metrics", "all_windows_evaluated",
            "baseline_score", "candidate_score", "score_delta", "min_score", "min_improvement",
            "case_count", "evaluated_case_count",
        ))
    if isinstance(replay.get("external_evaluation"), dict):
        summary["external_evaluation"] = _pick(replay["external_evaluation"], (
            "status", "policy", "verdict", "accepted", "decision_basis", "error_code",
            "baseline_score", "candidate_score", "score_delta", "min_score", "min_improvement",
            "case_count", "evaluated_case_count",
        ))
    if isinstance(replay.get("runtime_gate"), dict):
        summary["runtime_gate"] = _pick(replay["runtime_gate"], ("status", "reason", "required_runtimes"))
    if isinstance(replay.get("probe_gate"), dict):
        probe = replay["probe_gate"]
        summary["probe_gate"] = _pick(probe, (
            "status", "reasons", "attribution", "token_budget", "total_replay_tokens",
        ))
        summary["probe_gate"]["runs"] = [
            _pick(run, ("kind", "job_id", "verdict", "execution_gate"))
            for run in probe.get("runs", []) if isinstance(run, dict)
        ]
    if isinstance(replay.get("cost_gate"), dict):
        summary["cost_gate"] = _cost(replay["cost_gate"])
    result["replay"] = summary
    return result


def _empty(release, status, reason):
    return {
        "status": status, "reason": reason, "source": "frozen_validation_command_result",
        "source_release_id": release.get("restored_from") or None,
        "run_id": None, "payload_hash": None, "evaluation_hash": None,
        "candidate_id": release.get("candidate_id") or None,
        "candidate_revision": release.get("candidate_revision"),
        "baseline_execution_plan": None, "evaluation": None,
    }


def _composition(plan):
    value = normalize_execution_plan(plan)
    if value is None:
        raise ExecutionPlanError("missing execution plan")
    # These transport authorization fields do not change released component content.
    value["target_component_ids"] = []
    value["binding"].pop("allow_skill_change", None)
    return value


def _identity(payload, release):
    return (payload.get("candidate_id") == release.get("candidate_id")
            and type(payload.get("revision")) is int
            and payload["revision"] == release.get("candidate_revision"))


def _dependencies_match(payload, release):
    detail = payload.get("detail") or {}
    dependencies = detail.get("skill_dependencies")
    if not isinstance(dependencies, list) or any(not isinstance(d, dict) or not d.get("name") for d in dependencies):
        return False
    expected = copy.deepcopy(dependencies)
    if len({d["name"] for d in expected}) != len(expected):
        return False
    name = payload.get("skill_name")
    if name:
        try:
            if "bundle" not in payload or (payload.get("base_version", 0) and payload.get("baseline") is None):
                return False
            bundle = decode_bundle_payload(payload["bundle"])
            baseline = decode_bundle_payload(payload["baseline"]) if payload.get("baseline") else {}
            after = bundle_tree_sha256(bundle)
            if payload.get("tree_sha256") != after:
                return False
            if after != bundle_tree_sha256(baseline):
                dependency = next((d for d in expected if d["name"] == name), None)
                if dependency is None or type(payload.get("base_version")) is not int:
                    return False
                dependency.update(version=payload["base_version"] + 1, tree_sha256=after)
        except (SkillLibraryError, KeyError, TypeError, ValueError):
            return False
    actual = release["document"].get("skill_dependencies")
    return isinstance(actual, list) and sorted(expected, key=lambda d: d["name"]) == sorted(
        actual, key=lambda d: d.get("name", ""),
    )


async def _validation(conn, tx, release):
    result = _empty(release, "unavailable", "validation_reference_missing")
    ref = release.get("validation_ref")
    if release.get("restored_from") or (ref or "").startswith("rollback:"):
        return _empty(release, "not_applicable", "rollback")
    if not ref:
        if release["document"].get("origin") == "baseline-registration":
            return _empty(release, "not_applicable", "baseline_registration")
        return result
    history = EvolutionRepository(tx)
    command = await history.command(conn, ref)
    publication = None
    if command and command["kind"] == "skill.evolve":
        publication = command
        payload = command["payload"]
        outcome = command.get("result") or {}
        if (not isinstance(payload, dict) or command["payload_hash"] != digest(payload)
                or payload.get("operation") != "execution_publish_reviewed" or not _identity(payload, release)
                or not command.get("finished_at") or outcome.get("status") != "published"
                or outcome.get("release_id") != release["release_id"] or not payload.get("validation_run_id")):
            return {**result, "reason": "publication_command_mismatch"}
        command = await history.command(conn, payload["validation_run_id"])
    if command is None:
        return {**result, "reason": "validation_command_missing"}
    payload, outcome = command["payload"], command.get("result") or {}
    if (command["kind"] != "skill.validate" or not isinstance(payload, dict)
            or command["payload_hash"] != digest(payload) or not _identity(payload, release)):
        return {**result, "reason": "validation_command_mismatch"}
    if not command.get("finished_at") or outcome.get("status") not in {"evaluated", "published"}:
        return {**result, "reason": "validation_not_completed"}
    evaluation = outcome.get("evaluation")
    if not isinstance(evaluation, dict) or not evaluation:
        return {**result, "reason": "evaluation_missing"}
    if (evaluation.get("candidate_revision") != release["candidate_revision"]
            or evaluation.get("validation_run_id", command["command_id"]) != command["command_id"]):
        return {**result, "reason": "evaluation_identity_mismatch"}
    if publication:
        expected = publication["payload"]
        if (expected.get("validation_payload_hash") != command["payload_hash"]
                or expected.get("validation_result_hash") != digest(evaluation)):
            return {**result, "reason": "publication_validation_hash_mismatch"}
    detail = payload.get("detail") or {}
    if detail.get("execution_profile_id") != release["profile_id"]:
        return {**result, "reason": "validation_profile_mismatch"}
    if not _dependencies_match(payload, release):
        return {**result, "reason": "validated_dependencies_mismatch"}
    try:
        published = _composition(release["document"]["execution_plan"])
        frozen = _composition(detail.get("candidate_execution_plan"))
        if published != frozen:
            return {**result, "reason": "validated_content_mismatch"}
        baseline = detail.get("baseline_execution_plan")
        if baseline and _composition(baseline)["binding"].get("profile_id") != release["profile_id"]:
            return {**result, "reason": "baseline_profile_mismatch"}
    except (ExecutionPlanError, KeyError, TypeError, ValueError):
        return {**result, "reason": "validation_plan_invalid"}
    return {
        **result, "status": "available", "reason": "", "run_id": command["command_id"],
        "payload_hash": command["payload_hash"], "evaluation_hash": digest(evaluation),
        "evaluation_hash_scope": "full_frozen_evaluation_before_summary_projection",
        "publication_command_id": publication["command_id"] if publication else None,
        "baseline_execution_plan": copy.deepcopy(baseline), "base_release_id": detail.get("base_release_id"),
        "evaluation": evaluation_summary(evaluation),
    }


async def release_details(conn, tx, *, profile_id, release_id):
    """Tenant-admin API callers own the read transaction; no locks, writes or model calls."""
    repository = ExecutionRepository(tx)
    profile = await repository.profile(conn, profile_id)
    release = await repository.release(conn, release_id)
    if release["profile_id"] != profile_id:
        raise SkillNotFound("execution release not found")
    return {"profile_id": profile_id, "current_release_id": profile["current_release_id"],
            "release": release, "validation": await _validation(conn, tx, release)}
