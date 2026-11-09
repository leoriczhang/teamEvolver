"""Cost gates reconcile frozen TE attempts and actual Agent dispatch identities."""

from __future__ import annotations

import copy

import pytest

from team_replay.metrics import compare_efficiency, objective_replay_decision
from team_skills.execution.candidates import publication_gate
from team_skills.execution.costs import budget_gate, summarize_usage


def _attempt(identity, tokens, *, succeeded=True):
    return {
        "attempt_id": identity,
        "model": "fake-model",
        "status": "succeeded" if succeeded else "failed",
        "usage": {} if tokens is None else {"total_tokens": tokens},
        "usage_known": succeeded and tokens is not None,
    }


def _usage(*attempts, **overrides):
    return {
        "channel": "te_owned_llm",
        "attempts": copy.deepcopy(list(attempts)),
        "attempt_count": len(attempts),
        "known_total_tokens": sum(a["usage"].get("total_tokens", 0) for a in attempts),
        "unknown_attempts": sum(not a["usage_known"] for a in attempts),
        "in_flight_attempts": 0,
        "pending_dispatches": 0,
        "complete": all(a["usage_known"] for a in attempts),
        **overrides,
    }


def _detail(*, budget=1000):
    return {
        "generation_cost": _usage(_attempt("generation-and-case-synthesis", 10)),
        "validation_plan": {"probe_policy": {"max_total_tokens": budget}},
    }


def _evaluation(name="main", *, judge=5, baseline=20, candidate=30):
    return {
        "replay_te_llm_accounting": "platform-provider-attempts-v1",
        "te_llm_usage": _usage(_attempt(name + ":judge-and-feedback", judge)),
        "cases": [{
            "case_id": "case-1",
            "baseline": {"total_tokens": baseline, "observed_requests": [{"dispatch_id": name + ":baseline"}]},
            "candidate": {"total_tokens": candidate, "observed_requests": [{"dispatch_id": name + ":candidate"}]},
        }],
    }


def test_same_provider_attempt_and_checkpoint_reads_are_idempotent():
    first = _usage(_attempt("a", 7), _attempt("b", 3))
    result = summarize_usage([first, copy.deepcopy(first), summarize_usage([first])])
    assert result["known_total_tokens"] == 10
    assert result["attempt_count"] == 2
    assert result["unknown_attempts"] == 0
    assert result["complete"] is True


def test_partly_overlapping_attempt_sets_count_each_actual_provider_once():
    result = summarize_usage([
        _usage(_attempt("a", 7), _attempt("b", 3)),
        _usage(_attempt("b", 3), _attempt("c", 11)),
    ])
    assert result["known_total_tokens"] == 21
    assert result["attempt_count"] == 3
    assert result["complete"] is True


def test_overlapping_unknown_attempt_is_not_counted_twice():
    missing = _attempt("failed-provider", None, succeeded=False)
    result = summarize_usage([
        _usage(missing, _attempt("a", 7)), _usage(missing, _attempt("b", 3)),
    ])
    assert result["known_total_tokens"] == 10
    assert result["unknown_attempts"] == 1
    assert result["complete"] is False


def test_conflicting_snapshot_cannot_hide_behind_same_attempt_ids():
    result = summarize_usage([_usage(_attempt("same", 5)), _usage(_attempt("same", 900))])
    assert result["attempt_count"] == 1
    assert result["complete"] is False


def test_worker_retry_marker_cannot_hide_behind_duplicate_scope():
    first = _usage(_attempt("same", 5))
    result = summarize_usage([first, {**copy.deepcopy(first), "unreconciled_worker_attempts": 1}])
    assert result["known_total_tokens"] == 5
    assert result["complete"] is False


@pytest.mark.parametrize("snapshot", [None, {}, {"complete": False}, _usage(complete=False)])
def test_absent_or_incomplete_snapshot_cannot_satisfy_budget(snapshot):
    assert summarize_usage([snapshot])["complete"] is False


def test_empty_verified_scope_proves_zero_without_inventing_provider_attempt():
    result = summarize_usage([_usage()])
    assert result["known_total_tokens"] == result["attempt_count"] == result["unknown_attempts"] == 0
    assert result["complete"] is True


def test_truncated_ledger_retains_reported_known_lower_bound_but_not_complete():
    result = summarize_usage([_usage(
        _attempt("retained", 10), attempt_count=3, known_total_tokens=50,
        truncated=True, omitted_attempts=2,
    )])
    assert result["known_total_tokens"] >= 50
    assert result["complete"] is False


def test_declared_complete_does_not_override_missing_attempt_usage():
    result = summarize_usage([_usage(_attempt("missing-total", None), complete=True, unknown_attempts=0)])
    assert result["complete"] is False


@pytest.mark.parametrize("field", ["pending_dispatches", "in_flight_attempts"])
def test_live_dispatch_or_attempt_cannot_satisfy_budget(field):
    snapshot = _usage(_attempt("completed", 5), **{field: 1})
    assert summarize_usage([snapshot])["complete"] is False


def test_failed_provider_retry_keeps_known_lower_bound_and_unknown_remainder():
    result = summarize_usage([_usage(
        _attempt("failed-stream-with-partial-usage", 2, succeeded=False), _attempt("successful-retry", 7),
    )])
    assert result["attempt_count"] == 2
    assert result["known_total_tokens"] == 9
    assert result["unknown_attempts"] == 1
    assert result["complete"] is False


def test_total_includes_generation_case_synthesis_te_judges_and_both_agent_branches():
    result = budget_gate(_detail(), _evaluation())
    assert result["status"] == "passed" and result["reasons"] == []
    assert result["te_llm_usage"]["known_total_tokens"] == 15
    assert result["agent_replay_tokens"] == 50
    assert result["known_total_tokens"] == 65


@pytest.mark.parametrize("scope", ["main", "probe", "prior"])
def test_legacy_replay_ledger_cannot_claim_complete_cost_even_with_reported_tokens(scope):
    current = _evaluation()
    legacy = _evaluation("legacy")
    legacy.pop("replay_te_llm_accounting")
    prior = []
    if scope == "main":
        current = legacy
    elif scope == "probe":
        current["probe_gate"] = {"runs": [{"result": legacy}]}
    else:
        prior = [{**legacy, "generation_cost": _detail()["generation_cost"]}]
    result = budget_gate(_detail(), current, prior_evaluations=prior)
    assert result["status"] == "inconclusive"
    assert "replay_te_llm_accounting_unverified" in result["reasons"]
    assert "total_model_cost_unknown" in result["reasons"]
    assert result["known_total_tokens"] >= 65


def test_probe_checkpoint_reuse_deduplicates_te_attempts_and_agent_dispatches():
    evaluation = _evaluation()
    probe = _evaluation("aa", judge=7, baseline=3, candidate=4)
    evaluation["probe_gate"] = {"runs": [{"result": probe}, {"result": copy.deepcopy(probe)}]}
    result = budget_gate(_detail(), evaluation)
    assert result["status"] == "passed"
    assert result["te_llm_usage"]["known_total_tokens"] == 22
    assert result["agent_replay_tokens"] == 57
    assert result["known_total_tokens"] == 79


def test_prior_validations_accumulate_without_recounting_same_generation():
    previous = {**_evaluation("previous", judge=8, baseline=9, candidate=11),
                "generation_cost": _detail()["generation_cost"]}
    result = budget_gate(_detail(), _evaluation(), prior_evaluations=[previous, copy.deepcopy(previous)])
    assert result["status"] == "passed"
    assert result["te_llm_usage"]["known_total_tokens"] == 23
    assert result["agent_replay_tokens"] == 70
    assert result["known_total_tokens"] == 93


def test_previous_candidate_generation_revisions_are_part_of_total():
    detail = _detail()
    detail["generation_cost_history"] = [_usage(_attempt("previous-generation", 12))]
    result = budget_gate(detail, _evaluation())
    assert result["known_total_tokens"] == 77
    assert result["status"] == "passed"


def test_unavailable_prior_run_or_trimmed_generation_history_is_unknown():
    detail = _detail()
    detail["generation_cost_history_truncated"] = True
    result = budget_gate(detail, _evaluation(), prior_evaluations=[{}])
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"]
    assert result["known_total_tokens"] >= 65


def test_missing_agent_total_keeps_other_observed_tokens_as_lower_bound():
    result = budget_gate(_detail(), _evaluation(candidate=None))
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"]
    assert result["known_total_tokens"] >= 35  # TE 15 plus the observed baseline 20.


def test_unknown_total_still_blocks_when_known_lower_bound_already_exceeds_budget():
    result = budget_gate(_detail(budget=30), _evaluation(candidate=None))
    assert result["status"] == "inconclusive"
    assert result["known_total_tokens"] >= 35
    assert "total_model_cost_unknown" in result["reasons"]
    assert "total_model_cost_exceeds_budget" in result["reasons"]


@pytest.mark.parametrize("fault", ["tokens", "dispatch_id"])
def test_unknown_agent_channel_cannot_be_treated_as_zero(fault):
    evaluation = _evaluation()
    branch = evaluation["cases"][0]["candidate"]
    if fault == "tokens":
        branch.pop("total_tokens")
    else:
        branch["observed_requests"] = []
    result = budget_gate(_detail(), evaluation)
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"]


def test_conflicting_agent_dispatch_cost_is_unknown():
    evaluation = _evaluation()
    conflicting = copy.deepcopy(evaluation)
    conflicting["cases"][0]["baseline"]["total_tokens"] = 500
    evaluation["probe_gate"] = {"runs": [{"result": conflicting}]}
    result = budget_gate(_detail(), evaluation)
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"]


def test_order_of_same_dispatch_set_does_not_create_new_calls():
    evaluation = _evaluation()
    evaluation["cases"][0]["baseline"]["observed_requests"] = [{"dispatch_id": "a"}, {"dispatch_id": "b"}]
    repeated = copy.deepcopy(evaluation)
    repeated["cases"][0]["baseline"]["observed_requests"].reverse()
    evaluation["probe_gate"] = {"runs": [{"result": repeated}]}
    result = budget_gate(_detail(), evaluation)
    assert result["status"] == "passed"
    assert result["known_total_tokens"] == 65


def test_partly_overlapping_dispatch_groups_require_reconciliation():
    evaluation = _evaluation()
    repeated = copy.deepcopy(evaluation)
    original_id = evaluation["cases"][0]["baseline"]["observed_requests"][0]["dispatch_id"]
    repeated["cases"][0]["baseline"].update(
        total_tokens=25, observed_requests=[{"dispatch_id": original_id}, {"dispatch_id": "additional"}],
    )
    evaluation["probe_gate"] = {"runs": [{"result": repeated}]}
    result = budget_gate(_detail(), evaluation)
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"]


def test_worker_retry_cost_must_be_reconciled_before_publishing():
    result = budget_gate(_detail(), _evaluation(), worker_attempt=2)
    assert result["status"] == "inconclusive"
    assert "worker_retry_cost_unreconciled" in result["reasons"]
    assert result["known_total_tokens"] == 65


def test_generation_worker_retry_cannot_pass_with_only_latest_attempt_cost():
    detail = _detail()
    detail["generation_cost"]["unreconciled_worker_attempts"] = 1
    result = budget_gate(detail, _evaluation())
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"]


@pytest.mark.parametrize("budget,status", [(64, "inconclusive"), (65, "passed")])
def test_frozen_total_budget_is_enforced_even_when_agent_alone_fits(budget, status):
    result = budget_gate(_detail(budget=budget), _evaluation())
    assert result["known_total_tokens"] == 65
    assert result["status"] == status
    assert result["accounting_status"] == "complete"
    assert result["total_tokens"] == 65
    assert result["warnings"] == []
    assert ("total_model_cost_exceeds_budget" in result["reasons"]) is (budget < 65)


def test_historical_validation_can_push_new_run_over_the_same_frozen_budget():
    previous = {**_evaluation("previous", judge=8, baseline=9, candidate=11),
                "generation_cost": _detail()["generation_cost"]}
    result = budget_gate(_detail(budget=80), _evaluation(), prior_evaluations=[previous])
    assert result["known_total_tokens"] == 93
    assert result["status"] == "inconclusive"
    assert "total_model_cost_exceeds_budget" in result["reasons"]


def test_unlimited_ceiling_keeps_historical_attempt_and_dispatch_deduplication():
    previous = {**_evaluation("previous", judge=8, baseline=9, candidate=11),
                "generation_cost": _detail()["generation_cost"]}
    current = _evaluation(baseline=20_000_000, candidate=30_000_000)
    result = budget_gate(_detail(budget=None), current, prior_evaluations=[previous, copy.deepcopy(previous)])
    assert result["status"] == "passed" and result["budget_tokens"] is None
    assert result["known_total_tokens"] == 50_000_043
    assert result["te_llm_usage"]["attempt_count"] == 3
    assert result["agent_replay_tokens"] == 50_000_020
    assert result["prior_validation_runs"] == 2
    assert result["accounting_status"] == "complete"
    assert result["total_tokens"] == result["known_total_tokens"]
    assert result["warnings"] == []


@pytest.mark.parametrize("fault", ["agent_unknown", "te_unknown", "old_accounting", "worker_retry"])
def test_unlimited_ceiling_warns_about_unknown_usage_without_claiming_an_exact_total(fault):
    evaluation = _evaluation(candidate=None if fault == "agent_unknown" else 30)
    if fault == "te_unknown":
        evaluation["te_llm_usage"] = _usage(_attempt("unknown", None))
    if fault == "old_accounting":
        evaluation.pop("replay_te_llm_accounting")
    result = budget_gate(_detail(budget=None), evaluation, worker_attempt=2 if fault == "worker_retry" else 1)
    assert result["status"] == "passed" and result["budget_tokens"] is None
    assert result["reasons"] == []
    assert result["accounting_status"] == "incomplete"
    assert result["total_tokens"] is None
    expected = "worker_retry_cost_unreconciled" if fault == "worker_retry" else "total_model_cost_unknown"
    assert expected in result["warnings"]
    assert "total_model_cost_exceeds_budget" not in result["warnings"]


@pytest.mark.parametrize("budget", [None, 1_000_000])
def test_generation_worker_and_missing_historical_usage_remain_unknown_without_duplicate_charges(budget):
    detail = _detail(budget=budget)
    detail["generation_cost"] = _usage(
        _attempt("new-planner", 113666), _attempt("new-case-synthesis", 16262),
        unreconciled_worker_attempts=3,
    )
    previous_generation = _usage(_attempt("old-planner", 170497), _attempt("old-case-synthesis", 24044))
    detail["generation_cost_history"] = [previous_generation]
    current = _evaluation()
    previous = {**_evaluation("previous", judge=8, baseline=9, candidate=None),
                "generation_cost": copy.deepcopy(previous_generation)}
    previous.pop("replay_te_llm_accounting")
    before = copy.deepcopy((detail, current, previous))

    result = budget_gate(detail, current, prior_evaluations=[previous, copy.deepcopy(previous)])

    assert result["status"] == ("passed" if budget is None else "inconclusive")
    diagnostics = result["warnings"] if budget is None else result["reasons"]
    assert diagnostics == ["total_model_cost_unknown", "replay_te_llm_accounting_unverified"]
    assert result["known_total_tokens"] == 129928 + 194541 + 5 + 8 + 50 + 9
    assert result["agent_replay_tokens"] == 59
    assert result["te_llm_usage"]["attempt_count"] == 6
    assert result["te_llm_usage"]["unknown_attempts"] == 0  # Worker gaps are not invented provider attempts.
    assert result["te_llm_usage"]["complete"] is False
    assert result["agent_cost_complete"] is False
    assert result["replay_te_llm_accounting_complete"] is False
    assert result["accounting_status"] == "incomplete" and result["total_tokens"] is None
    assert (detail, current, previous) == before
    assert detail["generation_cost"]["unreconciled_worker_attempts"] == 3


@pytest.mark.parametrize("fault", ["missing_prior", "truncated_history", "conflicting_dispatch"])
def test_uncapped_history_gaps_or_conflicts_warn_and_preserve_the_same_finite_lower_bound(fault):
    detail, current, prior = _detail(budget=None), _evaluation(), []
    if fault == "missing_prior":
        prior = [{}]
    elif fault == "truncated_history":
        detail["generation_cost_history_truncated"] = True
    else:
        conflicting = copy.deepcopy(current)
        conflicting["cases"][0]["baseline"]["total_tokens"] = 500
        current["probe_gate"] = {"runs": [{"result": conflicting}]}
    before = copy.deepcopy((detail, current, prior))
    uncapped = budget_gate(detail, current, prior_evaluations=prior)
    finite_detail = copy.deepcopy(detail)
    finite_detail["validation_plan"]["probe_policy"]["max_total_tokens"] = 1000
    finite = budget_gate(finite_detail, current, prior_evaluations=prior)
    assert uncapped["status"] == "passed" and uncapped["reasons"] == []
    assert "total_model_cost_unknown" in uncapped["warnings"]
    assert uncapped["accounting_status"] == "incomplete" and uncapped["total_tokens"] is None
    assert uncapped["known_total_tokens"] == finite["known_total_tokens"] >= 65
    assert uncapped["te_llm_usage"] == finite["te_llm_usage"]
    assert uncapped["agent_cost_complete"] == finite["agent_cost_complete"]
    assert finite["status"] == "inconclusive" and "total_model_cost_unknown" in finite["reasons"]
    assert (detail, current, prior) == before


@pytest.mark.parametrize("gate,reason", [
    ("execution_gate", "execution_not_observed"),
    ("probe_gate", "execution_probes_incomplete"),
    ("holdout", "locked_acceptance_missing"),
    ("independent_validation", "independent_validation_missing"),
])
def test_nonblocking_uncapped_cost_does_not_bypass_business_publication_gates(gate, reason):
    detail = _detail(budget=None)
    detail["generation_cost"]["unreconciled_worker_attempts"] = 3
    trusted = {"validation_run_id": "validate:test", "validation_payload_hash": "a" * 64,
               "validation_result_hash": "b" * 64}
    detail.update(candidate_execution_plan={"components": []}, actual_modules=["prompt"],
                  replay_cases=[{"case_id": "case-1", "skill_ids": []}])
    detail["validation_plan"].update(input_hash="c" * 64, holdout={"status": "ready"})
    detail["independent_validation"] = {
        "schema_version": "teamevolver.independent-validation.v2", "status": "passed",
        "candidate_revision": 2, "evidence_ref": "artifact:test", **trusted,
        "validation_input_hash": "c" * 64, "case_ids": ["case-1"],
    }
    current = _evaluation()
    current["cost_gate"] = budget_gate(detail, current)
    current.update(execution_gate={"status": "passed"}, probe_gate={"status": "passed"})
    assert current["cost_gate"]["status"] == "passed"
    assert current["cost_gate"]["accounting_status"] == "incomplete"
    assert publication_gate(detail, current, candidate_revision=2, **trusted)["passed"] is True

    if gate == "holdout":
        detail["validation_plan"]["holdout"]["status"] = "missing"
    elif gate == "independent_validation":
        detail.pop("independent_validation")
    else:
        current[gate]["status"] = "inconclusive"
    publication = publication_gate(detail, current, candidate_revision=2, **trusted)
    assert publication["passed"] is False and reason in publication["reasons"]
    assert "total_model_cost_unverified" not in publication["reasons"]


def test_nonblocking_cost_does_not_override_original_replay_turn_priority():
    current = _evaluation()
    efficiency = compare_efficiency(
        {"interaction_turns": 1, "total_tokens": 100, "tool_call_count": 2},
        {"interaction_turns": 2, "total_tokens": 10, "tool_call_count": 1},
    )
    current["decision_policy"] = objective_replay_decision(efficiency=efficiency)
    before = copy.deepcopy(current)
    assert budget_gate(_detail(budget=None), current)["status"] == "passed"
    assert current["decision_policy"]["verdict"] == "reject"
    assert current["decision_policy"]["decision_basis"] == "interaction_turns_increased"
    assert current == before
