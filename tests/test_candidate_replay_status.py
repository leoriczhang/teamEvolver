"""A skipped/failed Replay must remain reviewable and cannot authorize Publish."""

import copy

import pytest
from fastapi.testclient import TestClient

from team_replay.aggregation import aggregate_true_replay_windows
from team_replay.metrics import compare_efficiency
from team_replay.results import replay_execution_summary
from team_skills.candidates.store import ValidationStore
from team_skills.library.bundle import attach_bundle_payload
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.proxy.routes import _evaluation_payload


def completed_evaluation():
    branches = {
        branch: {
            "ok": True,
            "final_response": "Verified receipt.",
            "interaction_turns": turns,
            "tool_call_count": 0,
            "total_tokens": turns * 10,
            "checklist_report": {"items": [{
                "id": "receipt", "text": "Verify receipt", "kind": "output",
                "satisfied": True, "evidence": "Verified receipt.",
            }]},
        }
        for branch, turns in (("baseline", 2), ("candidate", 1))
    }
    return {"accepted": True, "validator_mode": "true_replay", "replay_summary": {
        "status": "evaluated", "case_count": 1, "cases": [branches],
        "efficiency": compare_efficiency(branches["baseline"], branches["candidate"]),
    }}


@pytest.mark.parametrize("overall", ["evaluated", "completed"])
@pytest.mark.parametrize(("branch_status", "expected"), [
    ("failed", "failed"), ("error", "failed"),
    ("unsupported", "skipped"), ("skipped", "skipped"),
    ("not_run", "not_run"), ("pending", "not_run"), ("running", "not_run"),
])
def test_explicit_incomplete_branch_overrules_positive_cached_observations(overall, branch_status, expected):
    result = completed_evaluation()
    summary = result["replay_summary"]
    summary["status"] = overall
    summary["cases"][0]["candidate"]["status"] = branch_status
    # Even contradictory positive flags, responses and counters cannot override this status.
    assert summary["cases"][0]["candidate"]["ok"] is True
    execution = replay_execution_summary(summary)
    assert execution["status"] == expected
    assert execution["case_count"] == 0
    assert execution["reason"]
    payload = _evaluation_payload({}, result)
    assert payload["status"] == expected
    assert payload["recommended_publish"] is False
    assert ValidationStore._replay_has_completed_metrics(result) is False


def metadata_only_evaluation():
    result = completed_evaluation()
    result["replay_summary"]["cases"] = [{
        branch: {
            "branch": branch, "runtime": "document", "request_id": f"request-{branch}",
            "ok": True, "status": "completed",
            "checklist_report": side["checklist_report"],
        }
        for branch, side in result["replay_summary"]["cases"][0].items()
    }]
    return result


@pytest.mark.parametrize("overall", ["", "evaluated", "completed"])
def test_metadata_and_positive_success_flags_are_not_execution_observations(overall):
    result = metadata_only_evaluation()
    result["replay_summary"]["status"] = overall
    observed = replay_execution_summary(result["replay_summary"])
    assert observed["status"] == "not_run" and observed["case_count"] == 0
    assert _evaluation_payload({}, result)["recommended_publish"] is False
    assert ValidationStore._replay_has_completed_metrics(result) is False


@pytest.mark.parametrize("metric", ["interaction_turns", "tool_call_count", "total_tokens"])
def test_observed_zero_metric_is_valid_execution_evidence(metric):
    summary = {"status": "evaluated", "cases": [{
        "baseline": {metric: 0}, "candidate": {metric: 0},
    }]}
    assert replay_execution_summary(summary) == {"status": "completed", "reason": "", "case_count": 1}


@pytest.mark.parametrize("value", [None, "unavailable", "", False, float("nan"), float("inf")])
def test_missing_or_invalid_metrics_are_not_execution_observations(value):
    summary = {"status": "completed", "cases": [{
        "baseline": {"total_tokens": value}, "candidate": {"total_tokens": value},
    }]}
    observed = replay_execution_summary(summary)
    assert observed["status"] == "not_run" and observed["case_count"] == 0


@pytest.mark.parametrize(("side", "expected"), [
    ({"status": "failed"}, "failed"),
    ({"status": "unsupported"}, "skipped"),
    ({"status": "not_run"}, "not_run"),
    ({"runtime": "document"}, "not_run"),
    (None, "not_run"),
])
def test_partial_results_count_only_successfully_observed_pairs(side, expected):
    summary = copy.deepcopy(completed_evaluation()["replay_summary"])
    partial = copy.deepcopy(summary["cases"][0])
    if side is None:
        partial.pop("candidate")
    else:
        partial["candidate"] = side
    summary["cases"].append(partial)
    observed = replay_execution_summary(summary)
    assert observed["status"] == expected and observed["case_count"] == 1


@pytest.mark.parametrize("status", ["skipped", "not_run", "failed"])
def test_unfinished_result_never_becomes_completed_even_with_metrics(status):
    result = completed_evaluation()
    result["replay_summary"].update(status=status, reason="Runtime cannot execute this case")
    payload = _evaluation_payload({"skill_name": "example"}, result, cached=True)
    assert payload["status"] == payload["replay"]["status"] == status
    assert payload["replay"]["reason"] == "Runtime cannot execute this case"
    assert payload["recommended_publish"] is False
    assert payload["replay"]["verdict"] == "inconclusive"
    assert payload["replay"]["no_regression"] is False


@pytest.mark.parametrize("status", ["", "evaluated", "completed"])
def test_status_without_actual_paired_results_is_not_execution(status):
    summary = {"status": status, "case_count": 12, "cases": []}
    payload = _evaluation_payload({}, {"accepted": True, "replay_summary": summary})
    assert payload["status"] == "not_run"
    assert payload["replay"]["case_count"] == 0
    assert payload["recommended_publish"] is False


def test_real_pair_is_completed_but_a_branch_error_is_failed():
    result = completed_evaluation()
    payload = _evaluation_payload({}, result)
    assert payload["status"] == "completed"
    assert payload["replay"]["case_count"] == 1
    assert payload["recommended_publish"] is True
    result["replay_summary"]["cases"][0]["candidate"].update(ok=False, error="Agent timeout")
    assert _evaluation_payload({}, result)["status"] == "failed"
    assert _evaluation_payload({}, result)["recommended_publish"] is False


def test_completed_result_cannot_overrule_an_explicit_runtime_rejection():
    result = completed_evaluation()
    result["accepted"] = False
    assert _evaluation_payload({}, result)["recommended_publish"] is False


def test_candidate_cache_prefers_execution_evidence_over_positive_failed_metrics():
    result = completed_evaluation()
    assert ValidationStore._replay_has_completed_metrics(result) is True
    result["replay_summary"]["status"] = "failed"
    assert ValidationStore._replay_has_completed_metrics(result) is False
    result["replay_summary"]["status"] = "evaluated"
    result["replay_summary"]["efficiency"] = {}
    # An unobserved metric does not erase observed execution.
    assert ValidationStore._replay_has_completed_metrics(result) is True


@pytest.mark.parametrize("status", ["skipped", "failed", "not_run"])
def test_aggregation_retains_incomplete_status_reason_and_partial_execution(status):
    unfinished = {"status": status, "reason": "No Replay adapter bound to this tenant", "cases": []}
    empty = aggregate_true_replay_windows([("missing", unfinished)])
    assert empty["status"] == status
    assert empty["reason"] == unfinished["reason"]
    assert empty["case_count"] == 0
    partial = aggregate_true_replay_windows([
        ("real", completed_evaluation()["replay_summary"]), ("missing", unfinished),
    ])
    assert partial["status"] == status
    assert partial["reason"] == unfinished["reason"]
    assert partial["case_count"] == 1
    assert partial["accepted"] is False
    assert replay_execution_summary(partial)["status"] == status


@pytest.fixture
def replay_candidate_api(tmp_path, monkeypatch):
    config = TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "skills"),
        sharing_skill_mirror_enabled=False,
        sharing_skill_backend="local",
        sharing_local_root=str(tmp_path / "storage"),
    )
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-root-key")
    client = TestClient(ProxyServer(config).app, headers={
        "Authorization": "Bearer test-root-key", "X-Tenant-Id": "default",
    })
    store = ValidationStore.from_config(config)
    store.save_job({
        "job_id": "job", "candidate_revision": 1, "proposed_action": "create",
        "candidate_skill": attach_bundle_payload({
            "name": "example", "description": "Example candidate", "content": "Verify receipt.",
        }, {}),
    })
    return client, store


@pytest.mark.parametrize("status", ["skipped", "failed", "not_run", "evaluated"])
def test_api_cannot_publish_unexecuted_cache_and_keeps_candidate_open(replay_candidate_api, status):
    client, store = replay_candidate_api
    store.save_evaluation("job", {"accepted": True, "reason": "Runtime unavailable", "replay_summary": {
        "status": status, "cases": [],
    }})
    response = client.post("/api/skill-candidates/job/validate", json={"mode": "auto"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "replay_incomplete"
    assert store.load_decision("job") is None
    assert client.get("/api/skill-candidates/job/detail").json()["review_status"] == "open"


def test_api_allows_completed_accepted_pair_to_publish(replay_candidate_api):
    client, store = replay_candidate_api
    store.save_evaluation("job", completed_evaluation())
    detail = client.get("/api/skill-candidates/job/detail").json()
    assert detail["evaluation"]["status"] == "completed"
    assert detail["recommended_publish"] is True
    response = client.post("/api/skill-candidates/job/validate", json={"mode": "auto"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"


@pytest.mark.parametrize("overall", ["evaluated", "completed"])
@pytest.mark.parametrize("branch_status", ["unsupported", "skipped", "not_run", "metadata_only"])
def test_api_cannot_publish_cached_incomplete_branches_or_metadata(replay_candidate_api, overall, branch_status):
    client, store = replay_candidate_api
    if branch_status == "metadata_only":
        result = metadata_only_evaluation()
    else:
        result = completed_evaluation()
        result["replay_summary"]["cases"][0]["candidate"]["status"] = branch_status
    result["replay_summary"]["status"] = overall
    store.save_evaluation("job", result)
    response = client.post("/api/skill-candidates/job/validate", json={"mode": "auto"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "replay_incomplete"
    assert store.load_decision("job") is None
    detail = client.get("/api/skill-candidates/job/detail").json()
    assert detail["review_status"] == "open"
    assert detail["recommended_publish"] is False


def test_explicit_force_mode_retains_its_existing_admin_behavior(replay_candidate_api):
    client, store = replay_candidate_api
    store.save_evaluation("job", {"accepted": False, "replay_summary": {"status": "skipped", "cases": []}})
    response = client.post("/api/skill-candidates/job/validate", json={"mode": "force"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
