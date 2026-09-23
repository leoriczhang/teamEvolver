"""Exercise candidate queueing against the real Replay preparation contract."""

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.storage import LocalObjectStore
from team_skills.candidates.store import ValidationStore
from team_skills.evolution.kernel.settings import EvolveServerConfig
from team_skills.evolution.runtime.orchestrator import EvolveServer
from team_skills.library.registry import SkillIDRegistry


@pytest.mark.parametrize("runtimes,missing", [(["langfuse"], []), (["hermes"], ["langfuse"])])
def test_queue_candidate_uses_configured_validation_runtimes(tmp_path, runtimes, missing):
    server = object.__new__(EvolveServer)
    server.config = EvolveServerConfig.from_teamEvolver_config(
        TeamEvolverConfig(validation_runtimes=runtimes)
    )
    server._id_registry = SkillIDRegistry()
    server._validation_store = ValidationStore.from_bucket(bucket=LocalObjectStore(tmp_path))

    result = server._queue_validation_job(
        {"name": "example", "description": "Example", "content": "Follow the task."},
        "improve_skill",
        [{"session_id": "s1", "source": "langfuse", "turns": [
            {"prompt_text": "Complete the task", "response_text": "Completed"},
        ]}],
        "Trace evidence",
        "skill_group",
    )

    job = server._validation_store.load_job(result["validation_job_id"])
    assert result["action"] == "queued_for_validation"
    assert job["session_ids"] == ["s1"]
    assert job["runtime_validation_policy"]["required_runtimes"] == ["langfuse"]
    assert job["runtime_validation_policy"]["missing_replay_runtimes"] == missing


def test_engine_candidate_is_visible_to_tenant_validation_worker(tmp_path):
    config = TeamEvolverConfig(
        sharing_session_backend="local",
        sharing_local_root=str(tmp_path / "sessions"),
        sharing_skill_backend="local",
        sharing_skill_local_root=str(tmp_path / "skills"),
        sharing_skill_mirror_enabled=False,
        validation_runtimes=["langfuse"],
    )
    engine_config = EvolveServerConfig.from_teamEvolver_config(config)
    engine_config.pg_tenant_id = "product-design"
    server = EvolveServer(engine_config)
    result = server._queue_validation_job(
        {"name": "example", "content": "Follow the task."}, "improve_skill",
        [{"session_id": "s1", "source": "langfuse", "turns": [
            {"prompt_text": "Complete the task", "response_text": "Completed"},
        ]}], "Trace evidence", "skill_group",
    )
    job_id = result["validation_job_id"]
    assert ValidationStore.from_config(config, tenant_id="product-design").load_job(job_id)
    assert ValidationStore.from_config(config, tenant_id="another-tenant").load_job(job_id) is None


@pytest.mark.parametrize("candidate_turns,accepted", [(1, True), (2, False)])
def test_replay_aggregation_preserves_unavailable_deap_metrics(candidate_turns, accepted):
    from team_replay.metrics import compare_efficiency

    windows = []
    for window, tools in (("recent", "unavailable"), ("historical", 3)):
        windows.append((window, {
            "status": "evaluated", "case_count": 1,
            "efficiency": compare_efficiency(
                {"interaction_turns": 2, "tool_call_count": tools, "total_tokens": "unavailable"},
                {"interaction_turns": candidate_turns, "tool_call_count": tools, "total_tokens": "unavailable"},
            ),
            "cases": [{branch: {"checklist_report": {
                "all_satisfied": True, "items": [{
                    "id": "R1", "text": "Respond", "satisfied": True, "evidence": "response observed",
                }],
            }} for branch in ("baseline", "candidate")}],
        }))

    result = EvolveServer._aggregate_replay_windows(windows, max_interactions=8)

    assert result["accepted"] is accepted
    assert result["case_count"] == 2
    assert result["efficiency"]["candidate"]["interaction_turns"] == candidate_turns * 2
    for branch in ("baseline", "candidate"):
        assert result["efficiency"][branch]["tool_call_count"] == "unavailable"
        assert result["efficiency"][branch]["total_tokens"] == "unavailable"
