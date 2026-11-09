"""Interrupted Replay costs remain cumulative without inventing validation results."""
from __future__ import annotations

import copy

import pytest

from team_replay.datasets.repository import DatasetNotFound
from team_skills.execution.cost_history import historical_cost_output
from team_skills.execution.costs import budget_gate

MARKER = "platform-provider-attempts-v1"


def _usage(*attempts, **extra):
    return {
        "attempts": list(attempts), "attempt_count": len(attempts),
        "known_total_tokens": sum(a.get("usage", {}).get("total_tokens", 0) for a in attempts),
        "unknown_attempts": sum(not a["usage_known"] for a in attempts),
        "complete": all(a["usage_known"] for a in attempts), **extra,
    }


def _attempt(identity, tokens, *, failed=False):
    return {"attempt_id": identity, "model": "fixture", "status": "failed" if failed else "succeeded",
            "usage_known": not failed, "usage": {"total_tokens": tokens}}


def _values(total, count):
    quotient, remainder = divmod(total, count)
    return [quotient + (index < remainder) for index in range(count)]


def _leaf(identity, baseline=10, candidate=20, judge=5, *, attempts=None):
    return {
        "verdict": "reject", "runtime_gate": {"status": "rejected"},
        "replay_te_llm_accounting": MARKER,
        "te_llm_usage": _usage(*(attempts or [_attempt(identity + ":judge", judge)])),
        "cases": [{"case_id": identity, **{
            branch: {"total_tokens": tokens, "observed_requests": [{"dispatch_id": identity + ":" + branch}],
                     "completed": False, "response": "A response must not become a cost quality verdict."}
            for branch, tokens in [("baseline", baseline), ("candidate", candidate)]
        }}],
    }


def _aggregate(leaves):
    return {"accepted": False, "status": "rejected", "runtime_gate": {"status": "rejected"},
            "cases": [pair for leaf in leaves for pair in leaf["cases"]],
            "replay_te_llm_accounting": MARKER,
            "te_llm_usage": _usage(*(a for leaf in leaves for a in leaf["te_llm_usage"]["attempts"]))}


def _saved(run_id="validation", *, current_count=1, locked_count=1):
    plan = {"binding": {"allow_skill_change": False}, "target_component_ids": ["prompt"],
            "components": [{"component_id": "prompt", "kind": "agent_prompt"}]}
    cases = [{"case_id": f"locked-{i}", "skill_ids": [],
              "provenance": {"replay_selection": "locked_acceptance"}} for i in range(locked_count)]
    cases += [{"case_id": f"current-{i}", "skill_ids": [],
               "provenance": {"replay_selection": "current"}} for i in range(current_count)]
    return {"run_id": run_id, "result": None, "input": {"job": {
        "job_id": run_id, "replay_cases": cases, "actual_modules": ["prompt"], "affected_skill_ids": [],
        "current_skill": {}, "candidate_skill": {},
        "baseline_execution_plan": copy.deepcopy(plan), "candidate_execution_plan": copy.deepcopy(plan),
        "validation_plan": {"probe_policy": {"max_extra_runs": 5, "max_probe_cases": 4, "repetitions": 2}},
    }}}


class History:
    def __init__(self, saved, main, aa, repeat):
        self.rows = {saved["run_id"]: copy.deepcopy(saved)}
        self.checkpoints = {saved["run_id"]: copy.deepcopy(main)}
        self.reads = []
        for kind, value in [("aa", aa), ("repeat-1", repeat)]:
            if value is None:
                continue
            result, checkpoints = value
            self.rows[saved["run_id"] + ":" + kind] = {"run_id": saved["run_id"] + ":" + kind,
                                                       "result": copy.deepcopy(result)}
            self.checkpoints[saved["run_id"] + ":" + kind] = copy.deepcopy(checkpoints)

    async def get(self, conn, domain, run_id):
        assert domain == "skill"
        self.reads.append(("get", run_id))
        if run_id not in self.rows:
            raise DatasetNotFound("missing fixture run")
        return copy.deepcopy(self.rows[run_id])

    async def cases(self, conn, domain, run_id):
        assert domain == "skill"
        self.reads.append(("cases", run_id))
        return copy.deepcopy(self.checkpoints[run_id])


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [{}, {"replay": {"accepted": False}, "status": "evaluated"}])
async def test_complete_history_is_returned_unchanged_without_extra_reads(finished):
    saved = {"run_id": "complete", "result": finished}
    repo = History(_saved(), {}, None, None)
    result = await historical_cost_output(None, repo, saved)
    assert result is finished
    assert repo.reads == []


@pytest.mark.asyncio
async def test_partial_history_uses_actual_checkpoints_and_bounded_frozen_probe_names():
    saved = _saved()
    main = {"locked-0": _leaf("primary-locked"), "current-0": _leaf("primary-current")}
    aa = [_leaf("aa-locked"), _leaf("aa-current")]
    repeat = {"locked-0": _leaf("repeat-locked")}
    repo = History(saved, main, (_aggregate(aa), {}), (None, repeat))
    before = copy.deepcopy((saved, repo.rows, repo.checkpoints))

    result = await historical_cost_output(None, repo, saved)

    assert (saved, repo.rows, repo.checkpoints) == before  # no writes/finish or mutation
    assert repo.reads == [("cases", "validation"), ("get", "validation:aa"),
                          ("get", "validation:repeat-1"), ("cases", "validation:repeat-1")]
    assert result["cost_history_complete"] is False and result["te_llm_usage"]["complete"] is False
    assert result["partial_cost_history"]["missing_case_ids"] == []
    probes = {r["kind"]: r["result"] for r in result["probe_gate"]["runs"]}
    assert probes["repeat-1"]["partial_cost_history"]["missing_case_ids"] == ["current-0"]
    assert probes["repeat-1"]["te_llm_usage"]["complete"] is False
    assert probes["aa"]["te_llm_usage"]["known_total_tokens"] == 10
    for view in [result, *probes.values()]:
        assert not {"accepted", "status", "verdict", "runtime_gate", "execution_gate"} & view.keys()
        for pair in view["cases"]:
            assert all(set(pair[branch]) == {"total_tokens", "observed_requests"}
                       for branch in ("baseline", "candidate"))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["run", "checkpoint"])
async def test_missing_probe_is_unknown_and_does_not_fabricate_branch_zero(missing):
    saved = _saved()
    repo = History(saved, {"locked-0": _leaf("primary")}, None,
                   None if missing == "run" else (None, {}))
    result = await historical_cost_output(None, repo, saved)
    repeat = result["probe_gate"]["runs"][1]["result"]
    assert repeat["cases"] == [] and repeat["cost_history_complete"] is False
    assert repeat["te_llm_usage"]["complete"] is False
    assert repeat["te_llm_usage"]["attempts"] == []
    assert "total_tokens" not in repeat
    gate = budget_gate({"generation_cost": _usage(), "validation_plan": {"probe_policy": {
        "max_total_tokens": None}}}, result)
    assert gate["known_total_tokens"] == 35  # the actually recorded 5 + 10 + 20
    assert gate["status"] == "passed" and gate["total_tokens"] is None
    assert gate["agent_cost_complete"] is False and gate["te_llm_usage"]["complete"] is False


@pytest.mark.asyncio
async def test_failed_provider_attempt_retains_partial_usage_and_unknown_marker():
    saved = _saved()
    repo = History(saved, {"locked-0": _leaf("case", attempts=[
        _attempt("failed", 7, failed=True), _attempt("retry", 9),
    ])}, None, None)
    output = await historical_cost_output(None, repo, saved)
    assert output["te_llm_usage"]["known_total_tokens"] == 16
    assert output["te_llm_usage"]["unknown_attempts"] == 1
    assert output["te_llm_usage"]["complete"] is False
    assert [r["attempt_id"] for r in output["te_llm_usage"]["attempts"]] == ["failed", "retry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [{}, {"max_extra_runs": 999, "max_probe_cases": 4, "repetitions": 2}])
async def test_unavailable_or_unbounded_frozen_probe_plan_never_scans_other_runs(policy):
    saved = _saved()
    saved["input"]["job"]["validation_plan"]["probe_policy"] = policy
    repo = History(saved, {"locked-0": _leaf("recorded")}, None, None)
    output = await historical_cost_output(None, repo, saved)
    assert repo.reads == [("cases", "validation")]
    assert output["probe_gate"]["runs"] == []
    assert output["cost_history_complete"] is False


def _r28_fixture():
    saved = _saved(current_count=32, locked_count=4)
    main_ids = [case["case_id"] for case in saved["input"]["job"]["replay_cases"]]
    groups = [("primary", main_ids, 277851, 240845, 77),
              ("aa", ["locked-0", "current-0"], 12455, 14821, 4),
              ("repeat", ["locked-0"], 4959, 5704, 2)]
    values = {}
    for kind, ids, te, agent, count in groups:
        provider_values, agent_values = _values(te, count), _values(agent, 2 * len(ids))
        leaves, offset = {}, 0
        for index, identity in enumerate(ids):
            attempts = 2 + (index < count - 2 * len(ids))
            records = [_attempt(f"{kind}-attempt-{j}", provider_values[j])
                       for j in range(offset, offset + attempts)]
            offset += attempts
            leaves[identity] = _leaf(f"{kind}:{identity}", agent_values[index * 2],
                                     agent_values[index * 2 + 1], attempts=records)
        assert offset == count
        values[kind] = leaves
    return saved, History(saved, values["primary"], (_aggregate(values["aa"].values()), {}),
                          (None, values["repeat"]))


def _old25():
    generation = _usage(_attempt("generation-1", 60000), _attempt("generation-2", 69928),
                        complete=False, unreconciled_worker_attempts=3)
    judges, agents = _values(3540808 - 129928, 25), _values(2851559, 50)
    previous = []
    for i, judge in enumerate(judges):
        value = _leaf(f"old-{i}", agents[i * 2], agents[i * 2 + 1], judge)
        value["generation_cost"] = copy.deepcopy(generation)
        previous.append(value)
    detail = {"generation_cost": generation, "validation_plan": {"probe_policy": {"max_total_tokens": None}}}
    return detail, previous


@pytest.mark.asyncio
async def test_25_old_histories_plus_recorded_r28_556635_remain_idempotent_and_incomplete():
    saved, repo = _r28_fixture()
    first = await historical_cost_output(None, repo, saved)
    second = await historical_cost_output(None, repo, saved)
    assert first == second
    detail, previous = _old25()
    empty_current = {"cases": [], "te_llm_usage": _usage(), "replay_te_llm_accounting": MARKER}
    old = budget_gate(detail, empty_current, prior_evaluations=previous)
    assert old["known_total_tokens"] == 6392367

    once = budget_gate(detail, empty_current, prior_evaluations=[*previous, first])
    twice = budget_gate(detail, empty_current, prior_evaluations=[*previous, first, second])
    assert once["known_total_tokens"] == twice["known_total_tokens"] == 6949002
    assert once["known_total_tokens"] - old["known_total_tokens"] == 556635
    assert once["te_llm_usage"]["known_total_tokens"] - old["te_llm_usage"]["known_total_tokens"] == 295265
    assert once["agent_replay_tokens"] - old["agent_replay_tokens"] == 261370
    assert once["te_llm_usage"]["attempt_count"] == twice["te_llm_usage"]["attempt_count"] == 110
    assert once["agent_cost_complete"] is False and once["te_llm_usage"]["complete"] is False
    assert once["total_tokens"] is None and once["accounting_status"] == "incomplete"
    assert once["status"] == "passed" and once["warnings"] == ["total_model_cost_unknown"]
    assert saved["result"] is None and repo.rows["validation"]["result"] is None

    detail["validation_plan"]["probe_policy"]["max_total_tokens"] = 10000000
    finite = budget_gate(detail, empty_current, prior_evaluations=[*previous, first])
    assert finite["status"] == "inconclusive" and "total_model_cost_unknown" in finite["reasons"]
