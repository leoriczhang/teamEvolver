"""Candidate Replay must run every Case and retain progress across attempts."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from team_skills.evolution.validation import TrueReplayValidator
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
