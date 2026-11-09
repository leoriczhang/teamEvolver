"""Bounded A/A, repeat and single-change comparisons through the existing Replay engine."""
from __future__ import annotations

import copy

from team_replay._util import stable_hash
from team_replay.execution_inputs import case_execution_plan_pair, normalize_execution_inputs, template_input_contract
from team_replay.execution_plan import execution_plan_hash
from team_replay.metrics import metric_number
from team_replay.runtime_matrix import runtime_type_for_case

from .holdout import enhanced_validation
from .validation import execution_gate


def probe_jobs(job):
    if not enhanced_validation(job):
        return [], []
    detail = job
    plan = detail.get("validation_plan") or {}
    policy = plan.get("probe_policy") or {}
    maximum = policy.get("max_probe_cases", 4)
    # Cover each affected Skill with at most one Case, then one current Case.
    cases, covered = [], set()
    for selection in ("locked_acceptance", "current"):
        chosen = next((c for c in job.get("replay_cases", [])
                       if c.get("provenance", {}).get("replay_selection") == selection), None)
        if chosen and chosen not in cases:
            cases.append(copy.deepcopy(chosen))
            covered.update(chosen.get("skill_ids") or [])
    for case in job.get("replay_cases", []):
        skills = set(case.get("skill_ids") or [])
        if skills - covered or not cases:
            cases.append(copy.deepcopy(case))
            covered.update(skills)
    if len(cases) > maximum or not set(detail.get("affected_skill_ids") or []) <= covered:
        return [], ["probe_coverage_exceeds_budget"]
    variants = []

    def add(name, left, right, skill_change):
        value = copy.deepcopy(job)
        value.update(job_id=job["job_id"] + ":" + name, replay_cases=copy.deepcopy(cases))
        for key, body in (("baseline_execution_plan", left), ("candidate_execution_plan", right)):
            value[key] = copy.deepcopy(body)
            value[key]["binding"]["allow_skill_change"] = skill_change
        if not skill_change:
            value["candidate_skill"] = copy.deepcopy(value["current_skill"])
        if template_input_contract(left) is not None:
            hashes = {}
            for case in value["replay_cases"]:
                before, after = case_execution_plan_pair(
                    value["baseline_execution_plan"], value["candidate_execution_plan"], case,
                )
                hashes[case["case_id"]] = {
                    "baseline": execution_plan_hash(before), "candidate": execution_plan_hash(after),
                    "execution_inputs_hash": stable_hash(normalize_execution_inputs(
                        case["provenance"]["execution_inputs"],
                    )),
                }
            value["validation_plan"]["case_plan_hashes"] = hashes
        value["diagnostic_kind"] = name
        variants.append(value)

    left, right = job["baseline_execution_plan"], job["candidate_execution_plan"]
    add("aa", left, left, False)
    joint = "skill" in job.get("actual_modules", [])
    for index in range(1, policy.get("repetitions", 2)):
        add(f"repeat-{index}", left, right, joint)
    targets = set(right["target_component_ids"])
    dimensions = {p["kind"] for p in right["components"] if p["component_id"] in targets}
    if joint:
        dimensions.add("skill")
    if len(dimensions) > 1:
        for dimension in sorted(dimensions):
            partial = copy.deepcopy(left)
            replacement = {p["component_id"]: p for p in right["components"]
                           if p["component_id"] in targets and p["kind"] == dimension}
            partial["components"] = [copy.deepcopy(replacement.get(p["component_id"], p))
                                     for p in partial["components"]]
            add("only-" + dimension, left, partial, dimension == "skill")
    if len(variants) > policy.get("max_extra_runs", 5):
        return [], ["probe_runs_exceed_budget"]
    return variants, []


def _tokens(evaluation):
    efficiency = evaluation.get("efficiency") or {}
    values = [metric_number((efficiency.get(branch) or {}).get("total_tokens"))
              for branch in ("baseline", "candidate")]
    return sum(values) if all(v is not None for v in values) else None


def _case_evaluations(evaluation):
    windows = evaluation.get("window_results")
    if isinstance(windows, dict) and windows:
        for window in windows.values():
            if isinstance(window, dict):
                yield from _case_evaluations(window)
    else:
        yield evaluation


def _repeat_results(evaluation, expected, detail, *, primary=False):
    """Index the original per-Case verdict; a subset's aggregate is not comparable."""
    values, invalid = {}, False
    shared = template_input_contract(detail["baseline_execution_plan"]) is not None
    hashes = (detail.get("validation_plan") or {}).get("case_plan_hashes") or {}
    for result in _case_evaluations(evaluation):
        declared = (result.get("case_id"), str(result.get("runtime_type") or "").strip().lower().replace("_", "-"))
        if primary and all(declared) and declared not in expected:
            continue  # A known unselected failure may legitimately have no pair.
        pairs = result.get("cases") or []
        # An aggregate with several Cases has no usable per-Case verdict.
        if len(pairs) != 1:
            invalid = True
            continue
        pair = pairs[0]
        identity = (pair.get("case_id") or result.get("case_id"),
                    str(result.get("runtime_type") or "").strip().lower().replace("_", "-"))
        if primary and identity not in expected:
            continue  # Unselected primary Cases/runtimes do not enter this comparison.
        if identity not in expected:
            invalid = True
            values[identity] = (None, [])
            continue
        if identity in values:
            invalid = True
            continue
        if result.get("case_id") and pair.get("case_id") and result["case_id"] != pair["case_id"]:
            invalid = True
        completed = []
        for branch in ("baseline", "candidate"):
            observed = pair.get(branch) or {}
            expected_hash = ((hashes.get(identity[0]) or {}).get(branch) if shared else
                             execution_plan_hash(detail[branch + "_execution_plan"]))
            if (type(observed.get("completed")) is not bool
                    or observed.get("execution_verification") != "verified"
                    or not observed.get("observed_requests")
                    or observed.get("execution_plan_hash") != expected_hash):
                invalid = True
            completed.append(observed.get("completed"))
        verdict = result.get("verdict")
        if verdict not in {"accept", "reject", "inconclusive"}:
            invalid = True
        values[identity] = (verdict, completed)
    return values, invalid


def repeat_stability(primary, repeated, variant):
    """Compare exactly the frozen repeat Case/runtime keys, retaining all original decisions."""
    cases = variant.get("replay_cases") or []
    expected = {(case.get("case_id"), runtime_type_for_case(case)) for case in cases}
    if not expected or len(expected) != len(cases) or any(not all(identity) for identity in expected):
        return ["repeat_case_coverage_mismatch"]
    before, bad_before = _repeat_results(primary, expected, variant, primary=True)
    after, bad_after = _repeat_results(repeated, expected, variant)
    reasons = []
    if set(before) != expected or set(after) != expected:
        reasons.append("repeat_case_coverage_mismatch")
    if bad_before or bad_after:
        reasons.append("probe_unverified:" + variant["diagnostic_kind"])
    for identity in expected & before.keys() & after.keys():
        if before[identity][0] != after[identity][0]:
            reasons.append("repeat_verdict_unstable")
        if before[identity][1] != after[identity][1]:
            reasons.append("repeat_completion_unstable")
    return reasons


async def run_probes(job, primary, evaluate):
    if not job.get("candidate_execution_plan"):
        return {"status": "not_required"}
    if not enhanced_validation(job):
        return {"status": "not_required"}
    variants, reasons = probe_jobs(job)
    observations, total = [], _tokens(primary)
    budget = job.get("validation_plan", {}).get("probe_policy", {}).get("max_total_tokens", 1_000_000)
    if total is None:
        reasons.append("total_cost_unknown")
    for variant in variants:
        if total is None:
            break
        if budget is not None and total >= budget:
            reasons.append("probe_cost_budget_exhausted")
            break
        outcome = await evaluate(variant)
        gate = execution_gate(outcome, variant)
        runtime = (outcome.get("runtime_gate") or {}).get("status")
        kind = variant["diagnostic_kind"]
        # A/A and ablation measure execution, not a release recommendation. A
        # correct A/A normally has no efficiency gain and an inconclusive verdict.
        if gate["status"] != "passed" or runtime not in {"passed", "inconclusive", "rejected"}:
            reasons.append("probe_unverified:" + variant["diagnostic_kind"])
        tokens = _tokens(outcome)
        total = total + tokens if tokens is not None else None
        if tokens is None:
            reasons.append("probe_cost_unknown")
        if kind.startswith("repeat-"):
            reasons.extend(repeat_stability(primary, outcome, variant))
        if kind == "aa":
            # Asset equality is mandatory; output equality is not expected from stochastic models.
            if execution_plan_hash(variant["baseline_execution_plan"]) != execution_plan_hash(
                variant["candidate_execution_plan"]
            ):
                reasons.append("aa_assets_differ")
            if any((pair.get("baseline") or {}).get("completed") != (pair.get("candidate") or {}).get("completed")
                   for pair in outcome.get("cases") or []):
                reasons.append("aa_completion_unstable")
        observations.append({"kind": kind, "job_id": variant["job_id"], "execution_gate": gate,
                             "verdict": outcome.get("verdict"), "result": outcome})
    if total is not None and budget is not None and total > budget:
        reasons.append("total_cost_exceeded_budget")
    if len(observations) != len(variants):
        reasons.append("probe_plan_incomplete")
    return {"status": "passed" if not reasons else "inconclusive", "reasons": sorted(set(reasons)),
            "total_replay_tokens": total, "token_budget": budget, "runs": observations,
            "attribution": "single_change_comparisons" if any(v["kind"].startswith("only-") for v in observations)
            else "single_module_or_combination_only"}
