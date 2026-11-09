"""Candidate Replay must run every Case and retain progress across attempts."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from team_replay._util import stable_hash
from team_replay.contracts import CHECKLIST_FIRST_POLICY
from team_replay.evaluation import (
    EXTERNAL_AGENT_SCORE_POLICY,
    evaluation_policy_hash,
    external_case_request,
    external_score_decision,
    normalize_evaluation,
    normalize_external_report,
)
from team_skills.evolution.validation import TrueReplayValidator
from team_skills.execution.contracts import plan_for, validate_profile
from tests.support.execution import profile_raw
from tests.support.replay import document


def job():
    cases = []
    for number in range(3):
        case = copy.deepcopy(document()["cases"][0])
        case["case_id"] = f"case-{number}"
        cases.append(case)
    return {
        "job_id": "candidate", "skill_name": "demo", "candidate_skill": {"name": "demo"},
        "replay_cases": cases,
    }


def outcome(case_id):
    return {
        "status": "evaluated", "case_id": case_id, "case_count": 1,
        "runtime_type": "fake", "verdict": "accept",
        "cases": [{
            branch: {
                "ok": True, "completed": True,
                "checklist_report": {
                    "judge": "model", "all_satisfied": True, "items": [{"id": "R1", "satisfied": True}],
                },
            } for branch in ("baseline", "candidate")
        }],
        "efficiency": {
            "baseline": {"interaction_turns": 1, "tool_call_count": 2, "total_tokens": 20},
            "candidate": {"interaction_turns": 1, "tool_call_count": 1, "total_tokens": 10},
        },
    }


@pytest.mark.asyncio
async def test_checkpoint_resume_skips_completed_case_and_continues_after_exception():
    calls, checkpoints = [], []

    def evaluate(spec, *, case_index, **options):
        calls.append(case_index)
        if case_index == 1:
            raise TimeoutError("Agent timeout")
        return outcome(spec.cases[case_index]["case_id"])

    async def checkpoint(case, result):
        checkpoints.append((case["case_id"], result))

    validator = TrueReplayValidator(SimpleNamespace(evaluate=evaluate))
    result = await validator(
        job(), {"concurrency": 1},
        cached={"case-0": outcome("case-0")}, on_case=checkpoint,
    )
    assert calls == [1, 2]
    assert [item[0] for item in checkpoints] == ["case-1", "case-2"]
    assert checkpoints[0][1]["reason"] == "TimeoutError: Agent timeout"
    assert result["completed"] == 3 and result["total"] == 3
    assert result["failed"] == 1 and result["verdict"] == "inconclusive"


@pytest.mark.asyncio
async def test_cases_execute_with_configured_bounded_concurrency(monkeypatch):
    active, peak = 0, 0
    observed_options = []

    async def execute(function, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        observed_options.append(kwargs)
        await asyncio.sleep(0.01)
        active -= 1
        return outcome(args[0].cases[kwargs["case_index"]]["case_id"])

    monkeypatch.setattr(asyncio, "to_thread", execute)
    validator = TrueReplayValidator(SimpleNamespace(evaluate=lambda *_: None))
    result = await validator(job(), {"concurrency": 2, "timeout_seconds": 900, "max_interactions": 5})
    assert peak == 2
    assert result["completed"] == 3 and result["succeeded"] == 3
    assert all(options["timeout_seconds"] == 900 for options in observed_options)
    assert all(options["max_interactions"] == 5 for options in observed_options)


@pytest.mark.asyncio
async def test_cached_legacy_case_cannot_gain_new_provider_accounting_coverage():
    profile = validate_profile(profile_raw())
    plan = plan_for(profile, profile["components"])
    value = {**job(), "baseline_execution_plan": plan, "candidate_execution_plan": plan}
    checkpoints = {}

    async def checkpoint(case, result):
        checkpoints[case["case_id"]] = result

    validator = TrueReplayValidator(SimpleNamespace(evaluate=lambda spec, *, case_index, **kw: outcome(
        spec.cases[case_index]["case_id"],
    )))
    fresh = await validator(value, {}, on_case=checkpoint)
    assert fresh["replay_te_llm_accounting"] == "platform-provider-attempts-v1"
    assert fresh["te_llm_usage"]["complete"] is True  # The fake gateway makes no model calls.
    mixed = copy.deepcopy(checkpoints)
    mixed["case-0"].pop("replay_te_llm_accounting")
    resumed = await validator(value, {}, cached=mixed)
    assert resumed["replay_te_llm_accounting"] == "unverified"
    assert "replay_te_llm_accounting" not in mixed["case-0"]


def external_config(**changes):
    return normalize_evaluation({
        "mode": "external_agent", "external_agent": {
            "url": "https://evaluator.example.test/score", "rubric": "Assess the business response", **changes,
        },
    })


def scored_outcome(spec, index, *, baseline=50, candidate=60):
    case = spec.cases[index]
    result = outcome(case["case_id"])
    result["runtime_type"] = case["provenance"]["source_runtime"]["type"]
    for branch in ("baseline", "candidate"):
        result["cases"][0][branch].update(
            completed=False, final_response=f"{branch} response",
            checklist_report={"judge": "model", "all_satisfied": False, "items": [{"id": "R1", "satisfied": False}]},
        )
    result["efficiency"]["candidate"]["total_tokens"] = 100
    report = normalize_external_report({
        "baseline": {"score": baseline, "reason": "Baseline assessment"},
        "candidate": {"score": candidate, "reason": "Candidate assessment"},
    }, external_case_request(spec, case, result["cases"][0]))
    decision = external_score_decision([report], spec.evaluation)
    result.update(
        **decision, evaluation=copy.deepcopy(spec.evaluation),
        evaluation_policy_hash=evaluation_policy_hash(spec.evaluation), external_evaluation=report,
        case_hash=stable_hash(case),
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_frozen_config_pins_evaluation_and_policy_before_gateway(external):
    evaluation = external_config() if external else normalize_evaluation(None)
    observed = []

    def evaluate(spec, *, case_index, **options):
        observed.append((spec.evaluation, spec.policy_id))
        return scored_outcome(spec, case_index) if external else outcome(spec.cases[case_index]["case_id"])

    value = {**job(), "evaluation": {"mode": "untrusted_job_override"}}
    await TrueReplayValidator(SimpleNamespace(evaluate=evaluate))(value, {"evaluation": evaluation})
    assert observed == [(evaluation, EXTERNAL_AGENT_SCORE_POLICY if external else CHECKLIST_FIRST_POLICY)] * 3
    assert value["evaluation"] == {"mode": "untrusted_job_override"}


@pytest.mark.asyncio
async def test_external_runtime_uses_mean_scores_without_checklist_or_efficiency_veto():
    def evaluate(spec, *, case_index, **options):
        return scored_outcome(spec, case_index, candidate=(40, 80, 90)[case_index])

    result = await TrueReplayValidator(SimpleNamespace(evaluate=evaluate))(
        job(), {"evaluation": external_config()},
    )
    assert result["verdict"] == "accept"
    assert result["runtime_gate"]["status"] == "passed"
    assert result["succeeded"] == 3 and result["failed"] == 0
    assert result["decision_policy"]["candidate_score"] == 70
    assert result["datasets"][0]["result"]["verdict"] == "accept"


@pytest.mark.asyncio
async def test_external_scores_must_pass_independently_for_each_required_runtime():
    value = job()
    for case in value["replay_cases"][1:]:
        case["provenance"]["source_runtime"]["type"] = "other-runtime"

    def evaluate(spec, *, case_index, **options):
        return scored_outcome(spec, case_index, candidate=40 if case_index == 0 else 80)

    result = await TrueReplayValidator(SimpleNamespace(evaluate=evaluate))(
        value, {"evaluation": external_config()},
    )
    assert result["verdict"] == "accept"  # Global mean rises; the required runtime still regresses.
    assert result["runtime_gate"]["status"] == "rejected"
    assert result["runtime_gate"]["matrix"]["fake"]["status"] == "rejected"
    assert result["runtime_gate"]["matrix"]["other-runtime"]["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "legacy", "different_policy", "missing_score", "wrong_case", "branch_failure", "different_output",
])
async def test_external_incompatible_checkpoint_is_inconclusive_without_redispatch(invalid):
    calls, checkpoints = [], {}

    def evaluate(spec, *, case_index, **options):
        calls.append(case_index)
        return scored_outcome(spec, case_index)

    async def checkpoint(case, result):
        checkpoints[case["case_id"]] = result

    validator = TrueReplayValidator(SimpleNamespace(evaluate=evaluate))
    config = {"evaluation": external_config()}
    await validator(job(), config, on_case=checkpoint)
    if invalid == "legacy":
        checkpoints["case-0"] = outcome("case-0")
    elif invalid == "different_policy":
        checkpoints["case-0"]["evaluation_policy_hash"] = evaluation_policy_hash(external_config(rubric="Changed"))
    elif invalid == "missing_score":
        checkpoints["case-0"]["external_evaluation"].pop("candidate")
    elif invalid == "wrong_case":
        checkpoints["case-0"]["case_id"] = "another-case"
    elif invalid == "branch_failure":
        checkpoints["case-0"]["cases"][0]["candidate"]["ok"] = False
    else:
        checkpoints["case-0"]["cases"][0]["candidate"]["final_response"] = "A different response"
    original = copy.deepcopy(checkpoints)
    calls.clear()
    result = await validator(job(), config, cached=checkpoints)
    assert calls == []
    assert checkpoints == original
    assert result["verdict"] == "inconclusive"
    assert result["runtime_gate"]["status"] == "inconclusive"
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_external_matching_checkpoint_reuses_scores_when_checklist_is_incomplete():
    calls, checkpoints = [], {}

    def evaluate(spec, *, case_index, **options):
        calls.append(case_index)
        return scored_outcome(spec, case_index)

    async def checkpoint(case, result):
        checkpoints[case["case_id"]] = result

    validator = TrueReplayValidator(SimpleNamespace(evaluate=evaluate))
    config = {"evaluation": external_config()}
    await validator(job(), config, on_case=checkpoint)
    calls.clear()
    result = await validator(job(), config, cached=checkpoints)
    assert calls == []
    assert result["verdict"] == "accept" and result["runtime_gate"]["status"] == "passed"
    assert result["succeeded"] == 3


@pytest.mark.asyncio
async def test_external_gateway_failure_cannot_be_hidden_by_other_case_scores():
    def evaluate(spec, *, case_index, **options):
        if case_index == 1:
            raise TimeoutError("Evaluator unavailable")
        return scored_outcome(spec, case_index)

    result = await TrueReplayValidator(SimpleNamespace(evaluate=evaluate))(
        job(), {"evaluation": external_config()},
    )
    assert result["verdict"] == "inconclusive"
    assert result["runtime_gate"]["status"] == "inconclusive"
    assert result["succeeded"] == 2 and result["failed"] == 1


@pytest.mark.asyncio
async def test_default_mode_cannot_reuse_an_external_score_checkpoint():
    checkpoints, calls = {}, []

    def evaluate(spec, *, case_index, **options):
        calls.append(case_index)
        return scored_outcome(spec, case_index)

    async def checkpoint(case, result):
        checkpoints[case["case_id"]] = result

    validator = TrueReplayValidator(SimpleNamespace(evaluate=evaluate))
    await validator(job(), {"evaluation": external_config()}, on_case=checkpoint)
    calls.clear()
    original = copy.deepcopy(checkpoints)
    result = await validator(job(), {}, cached=checkpoints)
    assert calls == [] and checkpoints == original
    assert result["verdict"] == "inconclusive"
    assert result["runtime_gate"]["status"] == "inconclusive"
    assert result["succeeded"] == 0
