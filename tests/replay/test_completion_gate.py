"""A full Checklist is required on both sides before efficiency comparison."""

import pytest

from team_replay.execution import run_branch
from team_replay.hooks import AgentObservation, ReplayContext, ReplayTreatment
from team_replay.metrics import compare_efficiency
from team_replay.policy import aggregate_case_checklists, progressive_replay_decision


def test_candidate_cannot_win_against_an_unfinished_baseline():
    efficiency = compare_efficiency(
        {"interaction_turns": 8, "total_tokens": 100},
        {"interaction_turns": 2, "total_tokens": 20},
    )
    policy = progressive_replay_decision(
        efficiency=efficiency,
        baseline_checklist={"judge": "model", "total": 2, "all_satisfied": False},
        candidate_checklist={"judge": "model", "total": 2, "all_satisfied": True},
    )
    assert policy["verdict"] == "inconclusive"
    assert not policy["accepted"] and policy["decisive_metrics"] == []
    assert policy["metric_changes"] == {}


def test_branch_continues_for_every_unmet_item_despite_claimed_completion():
    sent, closed = [], []

    class Session:
        def send(self, message):
            sent.append(message)
            return AgentObservation("Working", metrics={"tool_call_count": 1, "total_tokens": 10})

        def close(self):
            closed.append(True)

    class Factory:
        def open(self, context):
            return Session()

    def judge(**kwargs):
        done = len(kwargs["interactions"]) >= 9
        return {"judge": "model", "all_satisfied": True, "items": [
            {"id": "R1", "satisfied": done, "evidence": "actual output" if done else ""},
        ]}

    context = ReplayContext(
        request_id="full", runtime_type="fake", treatment=ReplayTreatment("baseline", None),
        materials=(), context_snapshot={}, timeout_seconds=30, source_session={},
    )
    result = run_branch(
        Factory(), context,
        {"query": "Complete the video", "checks": [{"id": "R1", "kind": "output", "text": "Render the video"}]},
        harness={}, max_interactions=20, judge=judge, feedback=lambda **kw: "Please finish rendering",
    )
    assert result["completed"] and result["interaction_turns"] == 9
    assert result["total_tokens"] == 90 and result["tool_call_count"] == 9
    assert len(sent) == 9 and len(closed) == 1


@pytest.mark.parametrize("incomplete", ["baseline", "candidate", "both"])
def test_partial_runs_keep_actual_metrics_without_computing_deltas(incomplete):
    reports = {
        branch: {"judge": "model", "total": 2, "all_satisfied": incomplete not in (branch, "both")}
        for branch in ("baseline", "candidate")
    }
    efficiency = compare_efficiency(
        {"interaction_turns": 20, "total_tokens": 100},
        {"interaction_turns": 3, "total_tokens": 20}, checklists=reports,
    )
    assert efficiency["comparison_ready"] is False
    assert efficiency["baseline"]["interaction_turns"] == 20
    assert efficiency["candidate"]["total_tokens"] == 20
    assert all(d["delta"] is None and d["winner"] == "unavailable" for d in efficiency["dimensions"].values())
    assert progressive_replay_decision(
        efficiency=efficiency, baseline_checklist=reports["baseline"], candidate_checklist=reports["candidate"],
    )["verdict"] == "inconclusive"


def test_missing_case_report_cannot_disappear_from_aggregate_gate():
    result = aggregate_case_checklists([
        {"baseline": {"checklist_report": {"judge": "model", "items": [
            {"id": "R1", "satisfied": True, "evidence": "a rendered video"},
        ]}}},
        {"baseline": {}},
    ], branch="baseline")
    assert not result["all_satisfied"] and result["judge"] == "unavailable"
