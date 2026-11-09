"""Explicit unlimited Candidate authority does not change frozen Agent environments."""
from __future__ import annotations

import copy

import pytest

from team_skills.execution.holdout import candidate_validation_policy, validation_policy
from team_skills.execution.recovery import budget_override
from team_skills.library.repository import SkillLibraryError


def detail():
    return {"candidate_execution_plan": {"environment": {"validation": {"max_total_tokens": 1000}}},
            "baseline_execution_plan": {"environment": {"validation": {"max_total_tokens": 1000}}},
            "generation_cost": {"known_total_tokens": 117}, "replay_cases": [{"case_id": "unchanged"}]}


def test_explicit_unlimited_override_after_finite_increase_retains_all_execution_and_cost_data():
    original = detail()
    finite = budget_override(original, 2000, "An authorized finite increase")
    frozen = copy.deepcopy(finite)
    unlimited = budget_override(finite, None, "The owner explicitly requested no token ceiling")
    assert finite == frozen and original == detail()
    assert unlimited["validation_budget"] == {
        "max_total_tokens": None, "previous_max_total_tokens": 2000,
        "reason": "The owner explicitly requested no token ceiling",
    }
    assert {k: v for k, v in unlimited.items() if k != "validation_budget"} == original
    assert candidate_validation_policy(unlimited) == {**validation_policy(
        original["candidate_execution_plan"]["environment"]), "max_total_tokens": None}


@pytest.mark.parametrize("maximum", [None, 3000, 10_000_000, True, -1, "unlimited"])
def test_unlimited_authority_cannot_be_silently_replaced_or_reapplied(maximum):
    unlimited = budget_override(detail(), None, "Explicit no-ceiling authorization")
    with pytest.raises(SkillLibraryError):
        budget_override(unlimited, maximum, "A stale client must not reset this authority")


@pytest.mark.parametrize("maximum", [True, False, 0, -1, 10_000_001, 1.5, "null", "unlimited", {}])
def test_unsupported_budget_values_are_not_unlimited(maximum):
    with pytest.raises(SkillLibraryError):
        budget_override(detail(), maximum, "Authorized only with a valid amount or explicit JSON null")


@pytest.mark.parametrize("reason", [None, "", "  ", True, "x" * 2001])
def test_unlimited_authority_requires_a_bounded_explicit_reason(reason):
    with pytest.raises(SkillLibraryError):
        budget_override(detail(), None, reason)


@pytest.mark.parametrize("override", [
    {"max_total_tokens": None, "reason": "authorized"},
    {"max_total_tokens": None, "previous_max_total_tokens": None, "reason": "authorized"},
    {"max_total_tokens": None, "previous_max_total_tokens": True, "reason": "authorized"},
    {"max_total_tokens": None, "previous_max_total_tokens": 999, "reason": "authorized"},
    {"max_total_tokens": None, "previous_max_total_tokens": 10_000_001, "reason": "authorized"},
    {"max_total_tokens": None, "previous_max_total_tokens": 1000, "reason": ""},
    {"max_total_tokens": 2000, "previous_max_total_tokens": None, "reason": "authorized"},
])
def test_malformed_unlimited_history_fails_closed(override):
    with pytest.raises(SkillLibraryError):
        candidate_validation_policy({**detail(), "validation_budget": override})


def test_default_and_execution_profile_validation_still_require_finite_limits():
    assert validation_policy({})["max_total_tokens"] == 1_000_000
    with pytest.raises(SkillLibraryError):
        validation_policy({"validation": {"max_total_tokens": None}})
    assert candidate_validation_policy(detail())["max_total_tokens"] == 1000
