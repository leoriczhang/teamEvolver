"""Pending Prompt input snapshots inside the shared evolution branch checkpoint."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from team_replay._util import stable_hash
from team_replay.evaluation import (
    evaluation_policy_hash,
    external_case_request,
    external_evaluation_enabled,
    normalize_evaluation,
    validate_external_report,
)
from team_replay.execution_inputs import case_execution_plan_pair, normalize_execution_inputs
from team_replay.execution_plan import execution_plan_hash, normalize_execution_plan, validate_execution_plan_pair
from team_replay.metrics import metric_number
from team_skills.candidates.repository import CandidateStateError
from team_skills.evolution.repository import canonical, digest

from .contracts import apply_changes, text_hash

_RETIRED_FEEDBACK_FIELDS = (
    "active_candidate_completion", "active_candidate_token_feedback", "active_candidate_diagnostic_context",
    "active_candidate_behavior_feedback", "replay_diagnostic_audit", "behavior_review_protocol",
)


async def restore_prompt_history(services, task, historical, payloads):
    """Recover old summary-only rows only from their exact archived source hash."""
    from session_ingestion.queries import SessionQueries
    from team_skills.evolution.session_processing import load_sessions

    result, unavailable = [], []
    for session in historical:
        if session.get("turns"):
            result.append(session)
            continue
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            row = await SessionQueries(services.tx).get_session(conn, session_id=session["session_id"])
        if row and row.get("content_hash") == session.get("content_hash"):
            try:
                restored = await load_sessions([row], payloads)
            except (FileNotFoundError, ValueError):
                restored = []
            if restored:
                restored[0]["_evidence_window"] = "historical"
                result.extend(restored)
                continue
        unavailable.append(session["session_id"])
    return result, unavailable


def efficiency_feedback(evaluation):
    """Only the three existing efficiency dimensions, never Replay text or IDs."""
    replay = evaluation.get("replay") if isinstance(evaluation, dict) else None
    if isinstance(replay, dict) and external_evaluation_enabled(replay.get("evaluation")):
        return {}
    efficiency = replay.get("efficiency") if isinstance(replay, dict) else None
    if not isinstance(efficiency, dict):
        return {}
    metrics = ("interaction_turns", "tool_call_count", "total_tokens")
    result = {}
    for branch in ("baseline", "candidate"):
        values = efficiency.get(branch)
        result[branch] = {
            metric: number if (number := metric_number((values or {}).get(metric))) is not None else "unavailable"
            for metric in metrics
        } if isinstance(values, dict) else {
            metric: "unavailable" for metric in metrics
        }
    return result


def _external_feedback(replay, pair, case, evaluation):
    """Bind existing scores to the frozen policy and this exact executed pair."""
    if (normalize_evaluation(replay.get("evaluation")) != evaluation
            or replay.get("evaluation_policy_hash") != evaluation_policy_hash(evaluation)):
        return None
    report = pair.get("external_evaluation")
    if report is None:
        matches = [value.get("external_evaluation") for value in (replay.get("window_results") or {}).values()
                   if value.get("case_id") == case["case_id"]
                   and value.get("evaluation_policy_hash") == evaluation_policy_hash(evaluation)
                   and normalize_evaluation(value.get("evaluation")) == evaluation]
        if len(matches) != 1:
            return None
        report = matches[0]
    try:
        report = validate_external_report(report)
        request = external_case_request(SimpleNamespace(evaluation=evaluation), case, pair)
        if report["request_hash"] != stable_hash(request):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {branch: copy.deepcopy(report[branch]) for branch in ("baseline", "candidate")}


def _basic_context(context, *, include_replay_feedback=False):
    """Read old checkpoints without reactivating their retired derived feedback."""
    result = copy.deepcopy(context)
    for key in _RETIRED_FEEDBACK_FIELDS:
        result.pop(key, None)
    if not include_replay_feedback:
        result.pop("active_candidate_replay_feedback", None)
    if (context.get("active_candidate_replay_feedback") or {}).get("evaluation_mode") == "external_agent":
        result["active_candidate_feedback"] = {}
    feedback = result.get("active_candidate_feedback")
    result["active_candidate_feedback"] = efficiency_feedback({
        "replay": {"efficiency": feedback},
    }) if feedback else {}
    return result


def validation_feedback(context, candidate, command):
    """Project an existing completed development Replay, never run another Judge.

    Frozen Case ownership excludes locked acceptance and unknown/diagnostic
    results. The original machine judgment remains a hypothesis, not new
    production Evidence or permission to change business requirements.
    """
    evaluation = candidate.get("evaluation") or {}
    payload = (command or {}).get("payload") or {}
    detail = payload.get("detail") or {}
    try:
        # Native validation canonicalizes revision scalars (1 -> "1") and
        # component ordering. Compare the full canonical plans used by Replay,
        # preserving every target, binding, environment and content field.
        same_plans = all(normalize_execution_plan(detail.get(key)) == normalize_execution_plan(
            candidate["detail"].get(key),
        ) for key in ("baseline_execution_plan", "candidate_execution_plan"))
    except ValueError:
        return {}
    if (not command or command.get("kind") != "skill.validate" or not command.get("finished_at")
            or command.get("payload_hash") != digest(payload)
            or command.get("command_id") != evaluation.get("validation_run_id")
            or payload.get("candidate_id") != candidate["candidate_id"]
            or payload.get("revision") != candidate["revision"]
            or evaluation.get("candidate_revision") != candidate["revision"]
            or (command.get("result") or {}).get("evaluation") != evaluation
            or detail.get("execution_profile_id") != context["execution_profile_id"]
            or detail.get("base_release_id") != context["base_release_id"]
            or not same_plans):
        return {}
    cases = {case["case_id"]: case for case in detail.get("replay_cases") or []
             if isinstance(case, dict) and isinstance(case.get("case_id"), str)}
    replay = evaluation.get("replay") or {}
    try:
        selected_evaluation = normalize_evaluation((payload.get("replay_config") or {}).get("config", {}).get(
            "evaluation",
        ))
        external = external_evaluation_enabled(selected_evaluation)
        if external != external_evaluation_enabled(replay.get("evaluation")):
            return {}
    except (TypeError, ValueError):
        return {}
    results = []
    for pair in replay.get("cases") or []:
        if not isinstance(pair, dict):
            continue
        original = cases.get(pair.get("case_id"))
        provenance = (original or {}).get("provenance") or {}
        selection = provenance.get("replay_selection")
        if (not original or selection not in {"current", "historical"}
                or pair.get("selection") != selection or pair.get("query") != original.get("query")
                or pair.get("diagnostic_kind") or original.get("diagnostic_kind")
                or (original.get("metadata") or {}).get("validation_role") == "locked_acceptance"
                or (pair.get("provenance") or {}).get("replay_selection") != selection):
            continue
        item = {"query": original["query"], "checks": [{key: copy.deepcopy(check[key]) for key in
                ("id", "kind", "text", "polarity") if key in check} for check in original.get("checks") or []]}
        try:
            plans = case_execution_plan_pair(
                detail["baseline_execution_plan"], detail["candidate_execution_plan"], original,
            )
            inputs = (original.get("provenance") or {}).get("execution_inputs")
            if inputs is not None:
                inputs = normalize_execution_inputs(inputs)
                expected = {"baseline": execution_plan_hash(plans[0]), "candidate": execution_plan_hash(plans[1]),
                            "execution_inputs_hash": digest(inputs)}
                if ((detail.get("validation_plan") or {}).get("case_plan_hashes") or {}).get(
                    original["case_id"],
                ) != expected:
                    continue
                item["execution_inputs"] = {key: copy.deepcopy(inputs[key]) for key in (
                    "template_variables", "input_messages",
                )}
        except (KeyError, TypeError, ValueError):
            continue
        for branch in ("baseline", "candidate"):
            observed = pair.get(branch) or {}
            if not isinstance(observed, dict):
                break
            interactions = observed.get("interactions") or []
            if (observed.get("ok") is not True or type(observed.get("completed")) is not bool
                    or observed.get("execution_verification") != "verified"
                    or observed.get("execution_plan_hash") != execution_plan_hash(
                        plans[0 if branch == "baseline" else 1])
                    or not isinstance(interactions, list) or not interactions
                    or not all(isinstance(turn, dict) for turn in interactions)
                    or interactions[0].get("prompt") != original["query"]
                    or any(not isinstance(turn.get("prompt"), str) or not isinstance(turn.get("response"), str)
                           for turn in interactions)):
                break
            report = observed.get("checklist_report") or {}
            reports = report.get("items") or []
            expected = item["checks"]
            if (report.get("judge") == "unavailable" or not isinstance(reports, list)
                    or not all(isinstance(actual, dict) for actual in reports) or len(reports) != len(expected)
                    or any(any(actual.get(key) != check.get(key) for key in ("id", "kind", "text", "polarity"))
                           or type(actual.get("satisfied")) is not bool or not isinstance(actual.get("evidence"), str)
                           for check, actual in zip(expected, reports))):
                if not external:
                    break
                reports = []
            item[branch] = {
                "completed": observed.get("completed") is True,
                "interactions": [{key: turn[key] for key in ("prompt", "response")} for turn in interactions],
                "checks": [{key: actual[key] for key in ("id", "satisfied", "evidence")} for actual in reports],
            }
        else:
            if external:
                report = _external_feedback(replay, pair, original, selected_evaluation)
                if report is None:
                    continue
                item["external_evaluation"] = report
            results.append(item)
    return {"candidate_revision": candidate["revision"], "usage": "development_diagnostic_only",
            **({"evaluation_mode": selected_evaluation["mode"]} if external else {}),
            "machine_judgment_is_not_business_authority": True, "cases": results} if results else {}


def source_plan(plan):
    """Treatment targets and the derived Skill flag do not change the published asset."""
    result = normalize_execution_plan(plan)
    result["target_component_ids"] = []
    result["binding"].pop("allow_skill_change", None)
    return result


def candidate_identity(candidate):
    if candidate is None:
        return None
    detail = candidate["detail"]
    return {
        "candidate_id": candidate["candidate_id"], "revision": candidate["revision"],
        "base_release_id": detail.get("base_release_id"),
        "execution_profile_id": detail.get("execution_profile_id"),
        "working_plan_hash": execution_plan_hash(detail["candidate_execution_plan"])
        if detail.get("candidate_execution_plan") else None,
    }


def assert_planning_current(snapshot, candidate, release_id):
    if (candidate_identity(candidate) != snapshot["active_candidate"]
            or candidate is not None and candidate["status"] != "open"
            or release_id != snapshot["execution_context"]["base_release_id"]):
        raise CandidateStateError("candidate or execution baseline changed while the planner was running")


def pending_context(context, candidate):
    result = _basic_context(context)
    result.update(working_execution_plan=copy.deepcopy(context["baseline_execution_plan"]),
                  inherited_component_changes=[], active_candidate=candidate_identity(candidate),
                  active_candidate_feedback={})
    if candidate is None:
        return result
    if candidate["status"] != "open":
        raise CandidateStateError("pending Candidate must finish validation before Prompt evolution")
    detail = candidate["detail"]
    if detail.get("candidate_execution_plan"):
        left, right = validate_execution_plan_pair(
            detail["baseline_execution_plan"], detail["candidate_execution_plan"],
        )
        if (detail.get("base_release_id") != context["base_release_id"]
                or detail.get("execution_profile_id") != context["execution_profile_id"]
                or source_plan(left) != source_plan(context["baseline_execution_plan"])):
            raise CandidateStateError("pending Prompt does not match the frozen execution baseline")
        inherited = copy.deepcopy(detail.get("component_changes") or [])
        rebuilt = apply_changes(left, inherited, allowed_modules=["prompt"]) if inherited else left
        if normalize_execution_plan(rebuilt) != right:
            raise CandidateStateError("pending Prompt changes or evidence references are incomplete")
        result.update(working_execution_plan=copy.deepcopy(detail["candidate_execution_plan"]),
                      inherited_component_changes=inherited)
    evaluation = candidate.get("evaluation") or {}
    if evaluation.get("candidate_revision") == candidate["revision"]:
        result["active_candidate_feedback"] = efficiency_feedback(evaluation)
    return result


async def freeze_prompt_planning(
    conn, repo, *, task, branch_key, context, candidate, sessions, current_session_ids=None,
):
    """Persist before either model branch; a Worker retry reuses the same input bytes."""
    key = "__prompt_planning__:" + branch_key
    saved = await repo.branch(conn, task.task_id, key)
    if saved is not None:
        assert_planning_current(saved, candidate, context["base_release_id"])
        if source_plan(saved["execution_context"]["baseline_execution_plan"]) != source_plan(
            context["baseline_execution_plan"],
        ):
            raise CandidateStateError("frozen Prompt planning baseline changed")
        return restore_sessions(saved)
    frozen = pending_context(context, candidate)
    evaluation = (candidate or {}).get("evaluation") or {}
    if (candidate and evaluation.get("candidate_revision") == candidate["revision"]
            and evaluation.get("validation_run_id")):
        command = await repo.command(conn, candidate["evaluation"]["validation_run_id"])
        feedback = validation_feedback(context, candidate, command)
        if feedback:
            frozen["active_candidate_replay_feedback"] = feedback
    if current_session_ids is not None:
        from .batching import select_prompt_materials

        sessions, window = select_prompt_materials(sessions, frozen, current_session_ids)
        frozen["material_window"] = window
    snapshot = {
        "execution_context": frozen, "active_candidate": candidate_identity(candidate),
        "feedback_result_hash": digest(candidate.get("evaluation") or {}) if candidate else None,
        "sessions": copy.deepcopy(sessions), "session_sets": [], "replay_feedback_protocol": 1,
    }
    for session in snapshot["sessions"]:
        fields = {}
        for field in ("_skills_referenced", "_skills_injected"):
            if isinstance(session.get(field), set):
                fields[field] = sorted(session.pop(field))
        snapshot["session_sets"].append(fields)
    await repo.save_branch(conn, tenant_id=task.tenant_id, command_id=task.task_id, key=key,
                           session_ids=[s["session_id"] for s in sessions], result=snapshot)
    return restore_sessions(snapshot)


def restore_sessions(snapshot):
    # Canonicalize the consumed projection for stable first/retry writer bytes.
    # The persisted snapshot and its historical hashes remain untouched.
    result = json.loads(canonical(snapshot))
    for key in _RETIRED_FEEDBACK_FIELDS:
        result.pop(key, None)
    result["execution_context"] = _basic_context(
        result["execution_context"], include_replay_feedback=result.get("replay_feedback_protocol") == 1,
    )
    for session, fields in zip(result["sessions"], result.pop("session_sets"), strict=True):
        for key, values in fields.items():
            session[key] = set(values)
    return result


def synthesis_context(context):
    result = _basic_context(context)
    for key in ("working_execution_plan", "inherited_component_changes", "active_candidate",
                "active_candidate_job_id", "excluded_source_keys"):
        result.pop(key, None)
    result["active_candidate_feedback"] = {} if (
        context.get("active_candidate_replay_feedback") or {}
    ).get("evaluation_mode") == "external_agent" else efficiency_feedback({
        "replay": {"efficiency": result.get("active_candidate_feedback")},
    })
    return result


def cumulative_changes(context, changes):
    """Retain untouched pending components and refs; only fresh refs went through the writer."""
    inherited = copy.deepcopy(context.get("inherited_component_changes") or [])
    by_id = {change["component_id"]: change for change in inherited}
    for change in changes:
        value = copy.deepcopy(change)
        previous = by_id.get(value["component_id"], {})
        value["evidence_refs"] = list(dict.fromkeys([
            *previous.get("evidence_refs", []), *value.get("evidence_refs", []),
        ]))
        by_id[value["component_id"]] = value
    baseline = {part["component_id"]: part for part in context["baseline_execution_plan"]["components"]}
    return [change for cid, change in by_id.items() if text_hash(change["content"]) != baseline[cid]["content_hash"]]
