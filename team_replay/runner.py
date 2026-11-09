"""Pure Replay engine entry: immutable input, injected host, no HTTP or DB."""
from __future__ import annotations

import copy

from ._util import stable_hash
from .aggregation import aggregate_true_replay_windows
from .artifacts import validate_skill_treatment
from .contracts import REPLAY_RUN_RESULT_SCHEMA, ReplaySpec
from .engine import execute_pair, make_context, render_trajectory, source_runtime_type, treatment_for_case
from .evaluation import (
    evaluation_policy_hash,
    external_case_request,
    external_evaluation_enabled,
    external_score_decision,
    normalize_evaluation,
    normalize_external_report,
)
from .execution_inputs import case_execution_plan_pair
from .execution_plan import treatment_execution_plan
from .hooks import ReplayUnsupported
from .metrics import compare_efficiency
from .policy import normalize_checklist_report, progressive_replay_decision


def evaluate_case(spec: ReplaySpec, case: dict, *, host, factory, judge=None, feedback=None):
    """Execute exactly the saved Case and treatments; never read current assets."""
    external = external_evaluation_enabled(spec.evaluation)
    provenance = case.get("provenance") or {}
    source = copy.deepcopy(provenance.get("session_snapshot") or {})
    source["runtime"] = copy.deepcopy(provenance.get("source_runtime") or spec.runtime)
    if source.get("source") == "managed_agent_candidate_audit" or (
        source.get("runtime_context") or {}
    ).get("candidate_job_id"):
        raise ReplayUnsupported("candidate audit Session cannot be a Replay source")
    snapshot = copy.deepcopy(provenance.get("context_snapshot") or {})
    if provenance.get("context_snapshot_id") and "context_snapshot" not in provenance:
        raise ReplayUnsupported("recorded Context snapshot must be fixed before Replay")
    contexts = {}
    plans = case_execution_plan_pair(
        treatment_execution_plan(spec.baseline), treatment_execution_plan(spec.candidate), case,
    )
    for branch, treatment in (("baseline", spec.baseline), ("candidate", spec.candidate)):
        payload = copy.deepcopy(dict(treatment.payload))
        branch_snapshot = copy.deepcopy(snapshot)
        skill = None
        if spec.subject_kind == "memory":
            branch_snapshot["items"] = [
                item for item in branch_snapshot.get("items", [])
                if item.get("uri") != payload.get("uri")
            ]
            if payload.get("content"):
                branch_snapshot["items"].append({
                    "kind": "memory", "uri": payload.get("uri", ""), "scope": payload.get("scope", "team_memory"),
                    "content_hash": treatment.content_hash, "expanded": {"full": payload["content"]},
                })
        elif payload:
            validation = validate_skill_treatment(payload)
            if not validation["passed"]:
                raise ValueError("invalid fixed Skill treatment")
            skill = treatment_for_case(payload, case)
        contexts[branch] = make_context(
            branch, skill, case, source, int(spec.limits.get("timeout_seconds", 600)), snapshot=branch_snapshot,
            execution_plan=plans[0 if branch == "baseline" else 1],
        )
    if not source_runtime_type(source):
        raise ReplayUnsupported("Replay runtime.type is required")
    max_interactions = int(spec.limits.get("max_interactions", 4))
    harness, harness_error = {}, None
    if not external or max_interactions > 1:
        try:
            harness = host.judge_harness()
        except Exception as exc:
            if not external:
                raise
            harness_error = {"stage": "harness", "type": type(exc).__name__,
                             "error_code": "REPLAY_AUXILIARY_HARNESS_UNAVAILABLE"}
    results = execute_pair(
        factory, contexts, case, harness=harness,
        max_interactions=max_interactions, judge=judge, feedback=feedback, checklist_required=not external,
    )
    if harness_error is not None:
        for result in results.values():
            result.setdefault("auxiliary_errors", []).insert(0, copy.deepcopy(harness_error))
    checklists = {b: normalize_checklist_report(r, expected_checklist=case["checks"]) for b, r in results.items()}
    efficiency = compare_efficiency(results["baseline"], results["candidate"])
    extra = {}
    if external:
        report = {"status": "unavailable", "error_code": "external_evaluation_branch_failed"}
        if all(r.get("ok") for r in results.values()) and all(
            context.execution_plan is None or results[branch].get("execution_verification") == "verified"
            for branch, context in contexts.items()
        ):
            try:
                request = external_case_request(spec, case, results)
                evaluate = getattr(host, "evaluate_external", None)
                if not callable(evaluate):
                    raise ValueError("external evaluator is unavailable")
                report = normalize_external_report(evaluate(copy.deepcopy(request)), request)
            except Exception:
                report = {"status": "unavailable", "error_code": "external_evaluation_unavailable"}
        policy = external_score_decision([report], spec.evaluation)
        extra = {"evaluation": normalize_evaluation(spec.evaluation),
                 "evaluation_policy_hash": evaluation_policy_hash(spec.evaluation), "external_evaluation": report}
    else:
        policy = progressive_replay_decision(
            efficiency=efficiency, baseline_checklist=checklists["baseline"],
            candidate_checklist=checklists["candidate"],
        )
    if not external and any(
        efficiency[branch][metric] == "unavailable"
        for branch in ("baseline", "candidate") for metric in ("tool_call_count", "total_tokens")
    ):
        policy.update(accepted=False, verdict="inconclusive", no_regression=False, decision_basis="metrics_unavailable")
    if any(not r.get("ok") for r in results.values()):
        policy.update(accepted=False, verdict="inconclusive", no_regression=False, decision_basis="branch_failure")
    return {
        "schema_version": REPLAY_RUN_RESULT_SCHEMA, "run_id": spec.run_id, "status": "evaluated",
        "accepted": policy["accepted"], "verdict": policy["verdict"], "no_regression": policy["no_regression"],
        "decision_policy": policy, "efficiency": efficiency, "checklist": checklists,
        "runtime_type": source_runtime_type(source), "case_count": 1, "case_id": case["case_id"],
        "case_hash": stable_hash(case), "dataset_hash": spec.metadata.get("dataset_hash", ""),
        "adapter_revision": getattr(factory, "revision", ""), "reason": policy["decision_basis"],
        **extra,
        "cases": [{
            **{b: {**r, "checklist_report": checklists[b], "trajectory": render_trajectory(r.get("messages", []))}
               for b, r in results.items()},
            **({"external_evaluation": copy.deepcopy(extra["external_evaluation"])} if external else {}),
        }],
    }


def evaluate_spec(spec, *, host, case_index=None, judge=None, feedback=None):
    """A fake ReplayHost and deterministic judge suffice for a pure unit test."""
    factory = host.resolve_replay_factory(str(spec.runtime.get("type") or ""), {})
    if factory is None:
        raise ReplayUnsupported("Replay adapter is unavailable")
    outputs = [
        (case["case_id"], evaluate_case(spec, copy.deepcopy(dict(case)), host=host, factory=factory,
                                       judge=judge, feedback=feedback))
        for index, case in enumerate(spec.cases) if case_index is None or index == case_index
    ]
    if not outputs:
        raise ValueError("no selected Replay Case")
    if len(outputs) == 1:
        return outputs[0][1]
    return {**aggregate_results(outputs), "run_id": spec.run_id,
            "schema_version": REPLAY_RUN_RESULT_SCHEMA, "dataset_hash": spec.metadata.get("dataset_hash", "")}


def aggregate_results(outputs):
    """A missing or failed Case cannot be hidden by the aggregate efficiency."""
    result = aggregate_true_replay_windows(outputs)
    if external_evaluation_enabled(result.get("evaluation")):
        return result
    verdicts = {value.get("verdict") for _, value in outputs}
    if "reject" in verdicts:
        result.update(accepted=False, verdict="reject", no_regression=False, reason="one or more Cases rejected")
        result["decision_policy"] = {**result.get("decision_policy", {}), "accepted": False, "verdict": "reject"}
    elif "inconclusive" in verdicts or any(value.get("status") != "evaluated" for _, value in outputs):
        result.update(accepted=False, verdict="inconclusive", no_regression=False,
                      reason="one or more Cases are inconclusive")
        result["decision_policy"] = {**result.get("decision_policy", {}), "accepted": False, "verdict": "inconclusive"}
    return result
