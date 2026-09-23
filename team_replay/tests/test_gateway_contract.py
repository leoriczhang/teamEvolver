from __future__ import annotations

from team_replay.contracts import (
    CHECKLIST_FIRST_POLICY_V1,
    REPLAY_RUN_RESULT_SCHEMA_V1,
    ReplayContractError,
    skill_job_from_spec,
    skill_replay_spec,
)
from team_replay.gateway import DisabledReplayGateway, EmbeddedReplayGateway


def _job() -> dict:
    return {
        "job_id": "job-1",
        "candidate_revision": 3,
        "candidate_skill": {
            "name": "demo",
            "description": "Candidate",
            "content": "Do the work.",
        },
        "current_skill": {
            "name": "demo",
            "description": "Baseline",
            "content": "Do some work.",
        },
        "replay_cases": [
            {
                "dataset_id": "case-1",
                "dataset_format": "teamEvolver-progressive-test-v1",
                "query": "Complete the task",
                "checklist": [
                    {"id": "R01", "text": "Task is complete", "kind": "output"}
                ],
            }
        ],
        "include_full_trace": True,
        "source": {"kind": "skillminer"},
    }


def test_skill_job_round_trip_preserves_checklist_first_contract() -> None:
    job = _job()

    spec = skill_replay_spec(job, account_id="account-a")
    projected = skill_job_from_spec(spec)

    assert spec.policy_id == CHECKLIST_FIRST_POLICY_V1
    assert spec.account_id == "account-a"
    assert spec.baseline.kind == "skill_bundle"
    assert spec.candidate.revision == "3"
    assert projected["replay_cases"] == job["replay_cases"]
    assert projected["include_full_trace"] is True


def test_skill_set_treatments_round_trip_without_assigning_a_primary_skill() -> None:
    job = _job()
    job["candidate_skill"] = {
        "kind": "skill_set",
        "skills": [
            {"name": "skill-a", "description": "A", "content": "A"},
            {"name": "skill-b", "description": "B", "content": "B"},
        ],
    }
    job["current_skill"] = {
        "kind": "skill_set",
        "skills": [
            {"name": "skill-a", "description": "A", "content": "old A"},
            {"name": "skill-b", "description": "B", "content": "B"},
        ],
    }
    job["skill_ids"] = ["skill-a", "skill-b"]

    spec = skill_replay_spec(job)
    projected = skill_job_from_spec(spec)

    assert spec.baseline.kind == spec.candidate.kind == "skill_set"
    assert projected["skill_ids"] == ["skill-a", "skill-b"]
    assert [item["name"] for item in projected["candidate_skill"]["skills"]] == [
        "skill-a",
        "skill-b",
    ]


def test_disabled_gateway_reports_not_run_without_rejecting_candidate() -> None:
    result = DisabledReplayGateway().evaluate(skill_replay_spec(_job()))

    assert result["schema_version"] == REPLAY_RUN_RESULT_SCHEMA_V1
    assert result["status"] == "not_run"
    assert result["verdict"] == "inconclusive"
    assert result["accepted"] is False


def test_embedded_gateway_delegates_through_common_spec() -> None:
    captured = {}

    def evaluator(job_id, **kwargs):
        captured.update({"job_id": job_id, **kwargs})
        return {
            "status": "evaluated",
            "verdict": "accept",
            "accepted": True,
            "cases": [],
        }

    result = EmbeddedReplayGateway(evaluator).evaluate(
        skill_replay_spec(_job()),
        case_index=0,
        timeout_seconds=90,
    )

    assert result["schema_version"] == REPLAY_RUN_RESULT_SCHEMA_V1
    assert result["verdict"] == "accept"
    assert captured["job_id"] == "job-1"
    assert captured["job"]["candidate_revision"] == "3"
    assert captured["timeout"] == 90


def test_replay_spec_rejects_empty_test_dataset() -> None:
    job = _job()
    job["replay_cases"] = []

    try:
        skill_replay_spec(job)
    except ReplayContractError as exc:
        assert "Test Dataset" in str(exc)
    else:
        raise AssertionError("empty Test Dataset must be rejected")
