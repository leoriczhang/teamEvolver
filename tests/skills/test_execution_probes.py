"""Bounded diagnostic experiments, preserving frozen non-target execution inputs."""
from __future__ import annotations

import copy
import hashlib

import httpx
import pytest

from team_replay._util import stable_hash
from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.contracts import skill_job_from_spec, skill_replay_spec
from team_replay.execution_inputs import (
    EXECUTION_INPUTS_SCHEMA,
    TEMPLATE_INPUT_CONTRACT_SCHEMA,
    case_execution_plan_pair,
)
from team_replay.execution_plan import execution_plan_hash, validate_execution_plan_pair
from team_replay.integration import FixedHost
from team_replay.runner import evaluate_spec
from team_replay.runtime_matrix import evaluate_runtime_compatibility
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.probes import probe_jobs, repeat_stability, run_probes
from team_skills.execution.validation import execution_gate
from tests.support.execution import profile_raw
from tests.support.replay import judge, spec


def job(*, joint=True, context=True, policy=None):
    value = skill_job_from_spec(spec())
    before = plan_for(validate_profile(profile_raw()), validate_profile(profile_raw())["components"])
    after = copy.deepcopy(before)
    targets = ["main-prompt", "shared-context"] if context else ["main-prompt"]
    for part in after["components"]:
        if part["component_id"] in targets:
            part.update(content="Improved " + part["content"], revision=2)
            part["content_hash"] = hashlib.sha256(part["content"].encode()).hexdigest()
    for plan in (before, after):
        plan["target_component_ids"] = targets
        plan["binding"]["allow_skill_change"] = joint
    for case in value["replay_cases"]:
        case["provenance"]["source_runtime"] = {"type": "observed_http"}
        case["provenance"]["replay_selection"] = "locked_acceptance"
    value.update(baseline_execution_plan=before, candidate_execution_plan=after,
                 affected_skill_ids=["demo", "helper"], actual_modules=["skill", "prompt"] if joint else ["prompt"],
                 validation_plan={"probe_policy": {"max_probe_cases": 4, "max_extra_runs": 5,
                                                    "repetitions": 2, "max_total_tokens": 1000, **(policy or {})}})
    if not joint:
        value["candidate_skill"] = copy.deepcopy(value["current_skill"])
    return value


def outcome(value, *, verdict="accept", tokens=10):
    pairs = [{"case_id": case["case_id"], **{
        branch: {"completed": True, "execution_verification": "verified", "observed_requests": [{"test": True}],
                 "execution_plan_hash": execution_plan_hash(plan)}
        for branch, plan in zip(("baseline", "candidate"), case_execution_plan_pair(
            value["baseline_execution_plan"], value["candidate_execution_plan"], case,
        ), strict=True)}} for case in value["replay_cases"]]
    return {"verdict": verdict, "runtime_gate": {"status": "passed"},
            "efficiency": {branch: {"total_tokens": tokens} for branch in ("baseline", "candidate")},
            "cases": pairs, "window_results": {
                str(index): {"case_id": pair["case_id"], "verdict": verdict,
                             "runtime_type": case["provenance"]["source_runtime"]["type"], "cases": [pair]}
                for index, (case, pair) in enumerate(zip(value["replay_cases"], pairs, strict=True))}}


def test_aa_repeat_and_each_single_change_keep_environment_placement_and_other_assets_fixed():
    source = job()
    frozen = copy.deepcopy(source)
    variants, reasons = probe_jobs(source)
    assert reasons == [] and source == frozen
    by_kind = {item["diagnostic_kind"]: item for item in variants}
    assert set(by_kind) == {"aa", "repeat-1", "only-agent_prompt", "only-context_fragment", "only-skill"}
    for kind, variant in by_kind.items():
        left, right = validate_execution_plan_pair(
            variant["baseline_execution_plan"], variant["candidate_execution_plan"],
        )
        assert left["environment"] == right["environment"] == source["baseline_execution_plan"]["environment"]
        changed = {p["kind"] for p, q in zip(left["components"], right["components"], strict=True) if p != q}
        if kind == "aa":
            assert left == right and variant["current_skill"] == variant["candidate_skill"]
        elif kind.startswith("only-"):
            dimension = kind.removeprefix("only-")
            assert changed == (set() if dimension == "skill" else {dimension})
            assert (variant["current_skill"] != variant["candidate_skill"]) is (dimension == "skill")
        else:
            assert changed == {"agent_prompt", "context_fragment"}
            assert variant["candidate_skill"] == source["candidate_skill"]


def test_probe_cases_include_locked_acceptance_even_when_development_case_already_covers_all_skills():
    source = job()
    development = copy.deepcopy(source["replay_cases"][0])
    development["case_id"] = "development"
    development["provenance"]["replay_selection"] = "current"
    source["replay_cases"].insert(0, development)
    variants, reasons = probe_jobs(source)
    assert reasons == []
    assert all(any(case["provenance"]["replay_selection"] == "locked_acceptance"
                   for case in variant["replay_cases"]) for variant in variants)


@pytest.mark.parametrize("policy,reason", [({"max_probe_cases": 1}, "probe_coverage_exceeds_budget"),
                                          ({"max_extra_runs": 2}, "probe_runs_exceed_budget")])
def test_case_coverage_and_extra_run_limits_are_hard_bounds(policy, reason):
    source = job(policy=policy)
    if "max_probe_cases" in policy:
        first = copy.deepcopy(source["replay_cases"][0])
        source["replay_cases"][0]["skill_ids"] = ["demo"]
        first.update(case_id="second", skill_ids=["helper"])
        source["replay_cases"].append(first)
    variants, reasons = probe_jobs(source)
    assert variants == [] and reasons == [reason]


@pytest.mark.asyncio
async def test_aa_no_business_gain_is_not_a_rejection_and_repeat_matches_primary():
    source = job()
    primary, seen = outcome(source), []

    async def evaluate(variant):
        seen.append(variant["diagnostic_kind"])
        result = outcome(variant, verdict="inconclusive" if variant["diagnostic_kind"] == "aa" else "accept")
        if variant["diagnostic_kind"] == "aa":
            result["runtime_gate"]["status"] = "inconclusive"
        elif variant["diagnostic_kind"].startswith("only-"):
            result["runtime_gate"]["status"] = "rejected"
            result["verdict"] = "reject"
        return result

    result = await run_probes(source, primary, evaluate)
    assert result["status"] == "passed" and result["reasons"] == []
    assert result["attribution"] == "single_change_comparisons"
    assert len(seen) == 5 and result["total_replay_tokens"] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason", [("verdict", "repeat_verdict_unstable"),
                                         ("aa_completion", "aa_completion_unstable"),
                                         ("unverified", "probe_unverified:"),
                                         ("missing_requests", "probe_unverified:"),
                                         ("wrong_plan", "probe_unverified:"),
                                         ("runtime_unsupported", "probe_unverified:"),
                                         ("runtime_absent", "probe_unverified:"),
                                         ("runtime_blocked", "probe_unverified:"),
                                         ("runtime_pending", "probe_unverified:"),
                                         ("runtime_missing", "probe_unverified:"),
                                         ("missing_case", "probe_unverified:")])
async def test_unstable_or_unobserved_probe_results_are_inconclusive(fault, reason):
    source = job(joint=False, context=False)

    async def evaluate(variant):
        result = outcome(variant)
        if fault == "verdict" and variant["diagnostic_kind"].startswith("repeat-"):
            result["verdict"] = "reject"
            for leaf in result["window_results"].values():
                leaf["verdict"] = "reject"
        elif fault == "aa_completion" and variant["diagnostic_kind"] == "aa":
            result["cases"][0]["candidate"]["completed"] = False
        elif fault == "unverified":
            result["cases"][0]["candidate"]["execution_verification"] = "self_reported"
        elif fault == "missing_requests":
            result["cases"][0]["baseline"]["observed_requests"] = []
        elif fault == "wrong_plan":
            result["cases"][0]["baseline"]["execution_plan_hash"] = "0" * 64
        elif fault.startswith("runtime_"):
            result["runtime_gate"] = {} if fault == "runtime_absent" else {"status": fault.removeprefix("runtime_")}
        elif fault == "missing_case":
            result["cases"] = []
        return result

    result = await run_probes(source, outcome(source), evaluate)
    assert result["status"] == "inconclusive"
    assert any(item.startswith(reason) for item in result["reasons"])


@pytest.mark.asyncio
async def test_same_inconclusive_verdict_does_not_hide_repeat_completion_instability():
    source = job(joint=False, context=False)

    async def evaluate(variant):
        result = outcome(variant, verdict="inconclusive")
        if variant["diagnostic_kind"].startswith("repeat-"):
            for pair in result["cases"]:
                pair["baseline"]["completed"] = pair["candidate"]["completed"] = False
        return result

    result = await run_probes(source, outcome(source, verdict="inconclusive"), evaluate)
    assert result["status"] == "inconclusive"
    assert "repeat_completion_unstable" in result["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown_primary", "unknown_probe", "exhausted", "overshoot"])
async def test_unknown_or_exhausted_cost_cannot_pass_or_continue_unbounded_runs(fault):
    source = job(policy={"max_total_tokens": 25})
    primary, seen = outcome(source), []
    if fault == "unknown_primary":
        primary["efficiency"]["baseline"]["total_tokens"] = "unavailable"
    elif fault == "exhausted":
        primary["efficiency"]["baseline"]["total_tokens"] = 25

    async def evaluate(variant):
        seen.append(variant["diagnostic_kind"])
        result = outcome(variant)
        if fault == "unknown_probe":
            result["efficiency"]["candidate"]["total_tokens"] = None
        return result

    result = await run_probes(source, primary, evaluate)
    assert result["status"] == "inconclusive"
    assert len(seen) == (0 if fault in {"unknown_primary", "exhausted"} else 1)
    assert "probe_plan_incomplete" in result["reasons"]
    if fault == "unknown_primary":
        assert "total_cost_unknown" in result["reasons"]
    elif fault == "unknown_probe":
        assert "probe_cost_unknown" in result["reasons"]
    elif fault == "overshoot":
        assert "total_cost_exceeded_budget" in result["reasons"]


@pytest.mark.asyncio
async def test_unlimited_ceiling_runs_all_original_probes_without_changing_execution_environment():
    source, seen = job(policy={"max_total_tokens": None}), []
    frozen = copy.deepcopy(source)

    async def evaluate(variant):
        seen.append(variant["diagnostic_kind"])
        assert variant["validation_plan"]["probe_policy"]["max_total_tokens"] is None
        assert variant["baseline_execution_plan"]["environment"] == frozen["baseline_execution_plan"]["environment"]
        return outcome(variant, tokens=20_000_000)

    result = await run_probes(source, outcome(source, tokens=20_000_000), evaluate)
    assert result["status"] == "passed" and result["token_budget"] is None
    assert len(seen) == 5 and result["total_replay_tokens"] == 240_000_000
    assert source == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["primary", "probe"])
async def test_unlimited_ceiling_still_stops_when_observed_usage_is_unknown(unknown):
    source, seen = job(policy={"max_total_tokens": None}), []
    primary = outcome(source)
    if unknown == "primary":
        primary["efficiency"]["baseline"]["total_tokens"] = None

    async def evaluate(variant):
        seen.append(variant["diagnostic_kind"])
        result = outcome(variant)
        result["efficiency"]["candidate"]["total_tokens"] = None
        return result

    result = await run_probes(source, primary, evaluate)
    assert result["status"] == "inconclusive" and result["token_budget"] is None
    assert len(seen) == (0 if unknown == "primary" else 1)
    assert "probe_plan_incomplete" in result["reasons"]
    assert "probe_cost_budget_exhausted" not in result["reasons"]


@pytest.mark.asyncio
async def test_every_probe_variant_runs_through_real_observed_transport_and_all_branches_are_verified():
    source, sent = job(), []

    def post(endpoint, **kwargs):
        sent.append(kwargs["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}],
                                        "usage": {"total_tokens": 10, "prompt_tokens": 6, "completion_tokens": 4}},
                              request=httpx.Request("POST", endpoint))

    factory = ObservedHttpFactory({"endpoint": "https://model.test/chat"}, post=post)

    async def evaluate(variant):
        result = evaluate_spec(skill_replay_spec(variant), host=FixedHost(factory, {}), judge=judge)
        # Apply the production validator's runtime matrix to the real execution,
        # rather than inventing a passed runtime gate for an A/A with no gain.
        result["runtime_gate"] = evaluate_runtime_compatibility(
            {"required_runtimes": ["observed_http"]}, [result],
        )
        for pair in result["cases"]:
            for branch in ("baseline", "candidate"):
                assert pair[branch]["execution_verification"] == "verified"
                assert pair[branch]["observed_requests"]
        return result

    primary = await evaluate(source)
    result = await run_probes(source, primary, evaluate)
    assert result["status"] == "passed", result["reasons"]
    assert len(result["runs"]) == 5
    assert len(sent) == 12  # Primary A/B plus five real A/B transport experiments.


@pytest.mark.asyncio
async def test_legacy_skill_candidate_does_not_invoke_multi_asset_probe_executor():
    async def forbidden(_):
        pytest.fail("legacy Skill-only validation must not acquire Prompt probe work")

    assert await run_probes({}, {}, forbidden) == {"status": "not_required"}


def subset_job():
    value = job(joint=False, context=False)
    template = value["replay_cases"][0]
    cases = []
    for index in range(6):
        case = copy.deepcopy(template)
        case["case_id"] = f"case-{index}"
        case["provenance"]["replay_selection"] = (
            "locked_acceptance" if index == 0 else "current" if index == 1 else "historical"
        )
        cases.append(case)
    value["replay_cases"] = cases
    return value


@pytest.mark.asyncio
async def test_unselected_primary_failures_do_not_make_two_stable_repeated_cases_unstable():
    source = subset_job()
    primary = outcome(source, verdict="reject")
    for leaf in list(primary["window_results"].values())[:2]:
        leaf["verdict"] = "accept"
    for leaf in list(primary["window_results"].values())[2:]:
        leaf["cases"][0]["candidate"]["completed"] = False
    frozen, seen = copy.deepcopy(primary), []

    async def evaluate(variant):
        seen.append([case["case_id"] for case in variant["replay_cases"]])
        return outcome(variant, verdict="inconclusive" if variant["diagnostic_kind"] == "aa" else "accept")

    result = await run_probes(source, primary, evaluate)
    assert result["status"] == "passed" and result["reasons"] == []
    assert seen == [["case-0", "case-1"], ["case-0", "case-1"]]
    assert primary == frozen and primary["verdict"] == "reject"


@pytest.mark.parametrize("fault,reason", [
    ("verdict", "repeat_verdict_unstable"), ("completion", "repeat_completion_unstable"),
    ("missing_case", "repeat_case_coverage_mismatch"), ("extra_case", "repeat_case_coverage_mismatch"),
    ("runtime_changed", "repeat_case_coverage_mismatch"), ("runtime_missing", "repeat_case_coverage_mismatch"),
    ("duplicate_key", "probe_unverified:repeat-1"), ("unverified_primary", "probe_unverified:repeat-1"),
    ("unverified_repeat", "probe_unverified:repeat-1"), ("verdict_missing", "probe_unverified:repeat-1"),
])
def test_repeat_compares_original_case_outcomes_and_requires_exact_verified_coverage(fault, reason):
    source = subset_job()
    variant = next(value for value in probe_jobs(source)[0] if value["diagnostic_kind"] == "repeat-1")
    primary, repeated = outcome(source), outcome(variant)
    leaf = repeated["window_results"]["0"]
    if fault == "verdict":
        leaf["verdict"] = "reject"
    elif fault == "completion":
        leaf["cases"][0]["candidate"]["completed"] = False
    elif fault == "missing_case":
        repeated["window_results"].pop("0")
    elif fault == "extra_case":
        extra = copy.deepcopy(leaf)
        extra["case_id"] = extra["cases"][0]["case_id"] = "unselected-extra"
        repeated["window_results"]["extra"] = extra
    elif fault == "runtime_changed":
        leaf["runtime_type"] = "other-runtime"
    elif fault == "runtime_missing":
        leaf.pop("runtime_type")
    elif fault == "duplicate_key":
        repeated["window_results"]["duplicate"] = copy.deepcopy(leaf)
    elif fault == "unverified_primary":
        primary["window_results"]["0"]["cases"][0]["baseline"]["execution_verification"] = "pending"
    elif fault == "unverified_repeat":
        leaf["cases"][0]["candidate"]["execution_verification"] = "pending"
    else:
        leaf.pop("verdict")
    assert reason in repeat_stability(primary, repeated, variant)


@pytest.mark.parametrize("field,reason", [("verdict", "repeat_verdict_unstable"),
                                          ("completion", "repeat_completion_unstable")])
def test_same_case_multiple_runtimes_are_compared_separately_regardless_of_order(field, reason):
    source = job(joint=False, context=False)
    other = copy.deepcopy(source["replay_cases"][0])
    other["provenance"]["source_runtime"]["type"] = "other-runtime"
    source["replay_cases"].append(other)
    variant = {**source, "diagnostic_kind": "repeat-1"}
    primary, repeated = outcome(source), outcome(source)
    primary["window_results"]["0"]["verdict"] = repeated["window_results"]["0"]["verdict"] = "reject"
    for result in (primary, repeated):
        result["window_results"]["0"]["cases"][0]["candidate"]["completed"] = False
    repeated["window_results"] = dict(reversed(list(repeated["window_results"].items())))
    assert repeat_stability(primary, repeated, variant) == []
    if field == "verdict":
        repeated["window_results"]["0"]["verdict"] = "accept"
    else:
        repeated["window_results"]["0"]["cases"][0]["candidate"]["completed"] = True
    assert reason in repeat_stability(primary, repeated, variant)


def test_nested_case_windows_are_supported_but_aggregate_verdict_cannot_replace_case_decisions():
    source = subset_job()
    variant = next(value for value in probe_jobs(source)[0] if value["diagnostic_kind"] == "repeat-1")
    primary, repeated = outcome(source), outcome(variant)
    primary["window_results"] = {"nested": {"window_results": primary["window_results"]}}
    repeated["verdict"] = "reject"  # The actual Case verdicts remain unchanged.
    assert repeat_stability(primary, repeated, variant) == []
    repeated.pop("window_results")
    assert "repeat_case_coverage_mismatch" in repeat_stability(primary, repeated, variant)


@pytest.mark.parametrize("empty", [True, False])
def test_unselected_primary_exception_leaf_is_ignored_before_pair_shape_validation(empty):
    source = subset_job()
    variant = next(value for value in probe_jobs(source)[0] if value["diagnostic_kind"] == "repeat-1")
    primary, repeated = outcome(source, verdict="reject"), outcome(variant)
    for leaf in list(primary["window_results"].values())[:2]:
        leaf["verdict"] = "accept"
    failed = {"case_id": "unselected", "runtime_type": "other-runtime", "status": "failed",
              "verdict": "inconclusive"}
    if empty:
        failed["cases"] = []
    primary["window_results"]["unselected-exception"] = failed
    frozen = copy.deepcopy(primary)
    assert repeat_stability(primary, repeated, variant) == []
    assert primary == frozen and primary["verdict"] == "reject"


@pytest.mark.parametrize("fault", ["selected", "identity_missing", "runtime_missing"])
def test_selected_or_ambiguous_primary_exception_leaf_still_blocks_repeat(fault):
    source = subset_job()
    variant = next(value for value in probe_jobs(source)[0] if value["diagnostic_kind"] == "repeat-1")
    primary, repeated = outcome(source), outcome(variant)
    failed = {"case_id": "unselected", "runtime_type": "observed_http", "status": "failed",
              "verdict": "inconclusive", "cases": []}
    if fault == "selected":
        failed["case_id"] = variant["replay_cases"][0]["case_id"]
        primary["window_results"]["0"] = failed
    else:
        failed.pop("case_id" if fault == "identity_missing" else "runtime_type")
        primary["window_results"]["ambiguous"] = failed
    reasons = repeat_stability(primary, repeated, variant)
    assert "probe_unverified:repeat-1" in reasons
    if fault == "selected":
        assert "repeat_case_coverage_mismatch" in reasons


def shared_job():
    value = job()
    for branch in ("baseline", "candidate"):
        plan = value[branch + "_execution_plan"]
        plan["environment"].update(
            template_variables={"serviceName": "default"},
            template_input_contract={"schema_version": TEMPLATE_INPUT_CONTRACT_SCHEMA,
                                     "variable_names": ["serviceName"]},
        )
        component = plan["components"][0]
        component["binding"]["template_id"] = "shared-template"
        component["content"] += " For {{serviceName}}."
        component["content_hash"] = hashlib.sha256(component["content"].encode()).hexdigest()
    component = value["baseline_execution_plan"]["components"][0]
    template = copy.deepcopy(value["replay_cases"][0])
    value["replay_cases"] = []
    for index in (1, 2):
        case = copy.deepcopy(template)
        case["case_id"] = f"shared-{index}"
        inputs = {"schema_version": EXECUTION_INPUTS_SCHEMA,
                  "source_ref": {"session_id": f"source-{index}", "source_session_key": f"root-{index}",
                                 "trace_id": f"trace-{index}", "revision": "1"},
                  "template_refs": {component["component_id"]: {
                      "template_id": "shared-template", "revision": component["revision"],
                      "content_hash": component["content_hash"]}},
                  "template_variables": {"serviceName": f"service-{index}"},
                  "input_messages": [{"role": "user", "content": f"Prior public context {index}"}]}
        case["provenance"] = {"source_runtime": {"type": "observed_http"}, "execution_inputs": inputs,
                              "replay_selection": "locked_acceptance" if index == 1 else "current"}
        value["replay_cases"].append(case)
    value["validation_plan"]["case_plan_hashes"] = {}
    for case in value["replay_cases"]:
        before, after = case_execution_plan_pair(
            value["baseline_execution_plan"], value["candidate_execution_plan"], case,
        )
        value["validation_plan"]["case_plan_hashes"][case["case_id"]] = {
            "baseline": execution_plan_hash(before), "candidate": execution_plan_hash(after),
            "execution_inputs_hash": stable_hash(case["provenance"]["execution_inputs"]),
        }
    return value


@pytest.mark.asyncio
async def test_shared_template_aa_repeat_and_ablation_keep_per_case_frozen_hashes():
    source = shared_job()
    frozen = copy.deepcopy(source)
    variants, reasons = probe_jobs(source)
    assert not reasons and source == frozen
    for variant in variants:
        hashes = variant["validation_plan"]["case_plan_hashes"]
        assert set(hashes) == {case["case_id"] for case in variant["replay_cases"]}
        assert hashes["shared-1"]["baseline"] != hashes["shared-2"]["baseline"]
        if variant["diagnostic_kind"] == "aa":
            assert all(item["baseline"] == item["candidate"] for item in hashes.values())
        assert execution_gate(outcome(variant), variant) == {"status": "passed", "reasons": []}

    async def evaluate(variant):
        return outcome(variant)

    result = await run_probes(source, outcome(source), evaluate)
    assert result["status"] == "passed", result["reasons"]
    assert len(result["runs"]) == 5


@pytest.mark.parametrize("fault", ["global_hash", "other_case", "missing_frozen_hash"])
def test_shared_repeat_rejects_global_cross_case_or_unfrozen_plan_hashes(fault):
    source = shared_job()
    variant = next(item for item in probe_jobs(source)[0] if item["diagnostic_kind"] == "repeat-1")
    primary, repeated = outcome(source), outcome(variant)
    if fault == "missing_frozen_hash":
        variant["validation_plan"]["case_plan_hashes"].pop("shared-1")
    else:
        value = (execution_plan_hash(variant["baseline_execution_plan"]) if fault == "global_hash" else
                 variant["validation_plan"]["case_plan_hashes"]["shared-2"]["baseline"])
        repeated["window_results"]["0"]["cases"][0]["baseline"]["execution_plan_hash"] = value
    assert "probe_unverified:repeat-1" in repeat_stability(primary, repeated, variant)
