"""Exercise candidate queueing against the real Replay preparation contract."""

import pytest

from team_skills.candidates.store import CENTRAL_VALIDATOR_ID, ValidationStore
from team_skills.candidates.worker import ValidationWorker
from team_skills.evolution.kernel.settings import EvolveServerConfig
from team_skills.evolution.runtime.orchestrator import EvolveServer
from team_skills.library.bundle import attach_bundle_payload
from team_skills.library.registry import SkillIDRegistry
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.storage import LocalObjectStore


def candidate(name: str = "example", description: str = "Example"):
    payload = {
        "name": name,
        "description": description,
        "content": "Follow the task.",
    }
    return attach_bundle_payload(payload, {})


def test_pinned_dataset_retains_all_canonical_cases_and_negative_checks(tmp_path):
    from team_replay.datasets.schema import make_document
    from team_replay.datasets.store import SkillDatasetStore

    server = object.__new__(EvolveServer)
    server._skill_bucket = LocalObjectStore(tmp_path)
    server._skill_prefix = ""
    cases = [{
        "case_id": f"case-{index}", "query": f"Check receipt {index}",
        "checks": [{"id": "R01", "kind": "output", "text": "Never sign without authorization", "polarity": "must_not"}],
        "provenance": {"session_id": f"session-{index}"},
    } for index in (1, 2)]
    SkillDatasetStore(server._skill_bucket).save_dataset(make_document(
        dataset_id="pinned", name="Receipt regressions", skills=["example"], cases=cases,
        metadata={"enabled_for_evolution": True},
    ))

    selected = server._managed_evolution_datasets("example", 2)

    assert [case["case_id"] for case in selected] == ["case-1", "case-2"]
    assert all(case["checks"][0]["polarity"] == "must_not" for case in selected)
    assert all(case["provenance"]["evidence_window"] == "historical" for case in selected)
    assert server._managed_evolution_datasets("example", 1) == selected[:1]


@pytest.mark.asyncio
async def test_generated_canonical_case_reaches_validation_without_being_dropped(tmp_path):
    import json

    class Model:
        async def chat(self, *args, **kwargs):
            return json.dumps({"test_datasets": [{
                "query": "核验签收授权", "source_session_ids": ["s1"],
                "checks": [{"id": "R01", "kind": "output", "text": "核验授权"}],
            }]})

    server = EvolveServer(EvolveServerConfig(
        storage_local_root=str(tmp_path / "sessions"), skill_storage_local_root=str(tmp_path / "skills"),
        dataset_test_cases=1, dataset_min_requirements=1, validation_runtimes=["langfuse"],
    ))
    server._llm = Model()
    sessions = [{"session_id": "s1", "source": "langfuse", "turns": []}]
    datasets = await server._synthesize_candidate_datasets(
        skill_name="example", sessions=sessions, candidate_skill=candidate(),
        evidence_classification={}, evolution_context={}, replay_windows={},
    )
    assert len(datasets) == 1
    result = server._queue_validation_job(candidate(), "create", sessions, "Regression", "skill_group",
                                          test_datasets=datasets)
    job = server._validation_store.load_job(result["validation_job_id"])
    assert job["replay_cases"][0]["query"] == "核验签收授权"
    assert job["replay_cases"][0]["provenance"]["session_id"] == "s1"
    assert result["test_dataset_ids"] == [datasets[0]["case_id"]]


@pytest.mark.asyncio
async def test_old_central_result_cannot_publish_a_new_candidate_revision(tmp_path):
    server = object.__new__(EvolveServer)
    server.config = EvolveServerConfig(publish_mode="validated")
    server._validation_store = ValidationStore.from_bucket(bucket=LocalObjectStore(tmp_path))
    server._validation_store.save_job({"job_id": "changed", "candidate_revision": 2,
                                       "candidate_skill": candidate()})
    server._validation_store.save_result("changed", CENTRAL_VALIDATOR_ID, {
        "candidate_revision": 1, "decision": "accept", "accepted": True,
    })
    records, summary = await server._finalize_validation_jobs()
    assert records == []
    assert summary["pending"] == 1
    assert server._validation_store.load_decision("changed") is None


@pytest.mark.parametrize("runtimes,missing", [(["langfuse"], []), (["deap"], ["langfuse"])])
def test_queue_candidate_uses_configured_validation_runtimes(tmp_path, runtimes, missing):
    server = object.__new__(EvolveServer)
    server.config = EvolveServerConfig.from_teamEvolver_config(
        TeamEvolverConfig(validation_runtimes=runtimes)
    )
    server._id_registry = SkillIDRegistry()
    server._validation_store = ValidationStore.from_bucket(bucket=LocalObjectStore(tmp_path))

    result = server._queue_validation_job(
        candidate(),
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
        candidate("example", "Complete the example task."), "improve_skill",
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


@pytest.mark.asyncio
async def test_central_accept_result_publishes_without_quorum(tmp_path):
    server = object.__new__(EvolveServer)
    server.config = EvolveServerConfig(
        publish_mode="validated",
        evidence_enabled=False,
    )
    server._validation_store = ValidationStore.from_bucket(
        bucket=LocalObjectStore(tmp_path)
    )
    server._id_registry = SkillIDRegistry()

    async def resolve_and_upload(candidate, action):
        return action, bool(candidate)

    server._resolve_and_upload = resolve_and_upload
    job_id = "central-validation"
    server._validation_store.save_job(
        {
            "job_id": job_id,
            "candidate_revision": 1,
            "candidate_skill": candidate(
                "centralized-example",
                "Use the centralized validation workflow.",
            ),
            "proposed_action": "create",
            "runtime_validation_policy": {
                "required_runtimes": ["langfuse"],
                "missing_replay_runtimes": [],
                "incompatible_runtimes": {},
            },
        }
    )
    server._validation_store.save_result(
        job_id,
        CENTRAL_VALIDATOR_ID,
        {
            "candidate_revision": 1,
            "decision": "accept",
            "accepted": True,
            "runtime_type": "langfuse",
            "validator_capabilities": sorted(ValidationWorker.CAPABILITIES),
        },
    )

    records, summary = await server._finalize_validation_jobs()

    decision = server._validation_store.load_decision(job_id)
    assert summary["published"] == 1
    assert records[0]["validation_result"]["decision"] == "accept"
    assert decision["status"] == "published"
    assert "result_count" not in decision


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["reject", "inconclusive"])
async def test_central_non_accept_result_never_waits_for_more_votes(
    tmp_path,
    verdict,
):
    server = object.__new__(EvolveServer)
    server.config = EvolveServerConfig(
        publish_mode="validated",
        evidence_enabled=False,
    )
    server._validation_store = ValidationStore.from_bucket(
        bucket=LocalObjectStore(tmp_path)
    )
    server._id_registry = SkillIDRegistry()
    job_id = f"central-{verdict}"
    server._validation_store.save_job(
        {
            "job_id": job_id,
            "candidate_revision": 1,
            "candidate_skill": candidate(
                f"{verdict}-example",
                "Exercise the central validation state machine.",
            ),
            "proposed_action": "create",
            "runtime_validation_policy": {},
        }
    )
    server._validation_store.save_result(
        job_id,
        CENTRAL_VALIDATOR_ID,
        {
            "candidate_revision": 1,
            "decision": verdict,
            "accepted": False,
            "reason": f"central result: {verdict}",
            "validator_capabilities": sorted(ValidationWorker.CAPABILITIES),
        },
    )

    records, summary = await server._finalize_validation_jobs()

    decision = server._validation_store.load_decision(job_id)
    if verdict == "reject":
        assert decision["status"] == "rejected"
        assert summary["rejected"] == 1
        assert records[0]["validation_result"]["decision"] == "reject"
    else:
        assert decision is None
        assert summary["pending"] == 1
        assert summary["inconclusive"] == 1
        assert records[0]["action"] == "escalated_to_human_review"
