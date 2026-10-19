"""Evolution runtime budgets must accommodate multi-call reasoning models."""

from team_skills.evolution.cycle import PLANNER_TIMEOUT_SECONDS


def test_planner_timeout_covers_seed_evolving_multi_call_budget():
    assert PLANNER_TIMEOUT_SECONDS >= 1_800
