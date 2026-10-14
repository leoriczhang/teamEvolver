"""A durable user stop wins over stale worker writes without hiding Native acceptance."""

import asyncio
import copy

import pytest

from team_miner.ov_compile_mining import SKILL_BODY, OvSkillMiningRepository
from team_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.tests.test_extract_batches import begin
from team_miner.tests.test_ov_staged_orchestration import Owner, drive, start
from team_miner.tests.test_round_reuse import POLICY, next_inputs
from team_miner.tests.test_selection_window_recovery import InvalidSelectionFake, RecoveryOwner


def new_job(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path)
    job = repository.create_job(
        name="stop-race", account="default", source_uri="viking://resources/source",
        source_snapshot_uri="viking://resources/snapshot", output_uri="viking://resources/output",
        document_count=1, max_rounds=2,
    )
    return repository, job


@pytest.mark.parametrize("stop_status", ["stopping", "stopped"])
@pytest.mark.parametrize("late_status", ["preparing", "queued", "running", "failed", "succeeded", "waiting_human"])
def test_stale_transition_preserves_stop_and_accepted_task_identity(tmp_path, stop_status, late_status):
    repository, job = new_job(tmp_path)
    stopped = repository.update_job(
        job["job_id"], status=stop_status, finished_at="stop-finished", error="stop-error",
        stop_reason="user stop", ov_cancel_reason="user stop", ov_cancel_requested_at="stop-requested",
        ov_task_id="accepted-native",
    )
    observed = repository.update_job(
        job["job_id"], status=late_status, finished_at="", error="late worker error", stop_reason="",
        ov_cancel_reason="", ov_cancel_requested_at="", ov_task_id="", staged_progress={"completed": 3},
    )
    for field in ("status", "finished_at", "error", "stop_reason", "ov_cancel_reason",
                  "ov_cancel_requested_at", "ov_task_id"):
        assert observed[field] == stopped[field]
    assert observed["staged_progress"] == {"completed": 3}
    assert repository.get_job(job["job_id"]) == observed


def test_cancel_intent_and_metadata_only_updates_cannot_clear_stop_lifecycle(tmp_path):
    repository, job = new_job(tmp_path)
    stopping = repository.update_job(
        job["job_id"], status="running", ov_cancel_reason="user stop", error="stop-error", finished_at="",
    )
    observed = repository.update_job(job["job_id"], status="failed", error="worker failure", finished_at="late")
    assert observed["status"] == stopping["status"]
    assert observed["error"] == "stop-error" and observed["finished_at"] == ""
    observed = repository.update_job(job["job_id"], error="", finished_at="late", ov_cancel_sent_at="sent")
    assert observed["error"] == "stop-error" and observed["finished_at"] == ""
    assert observed["ov_cancel_sent_at"] == "sent"
    completed = repository.update_job(job["job_id"], status="stopped", finished_at="cancel-finished", error="")
    assert completed["status"] == "stopped" and completed["finished_at"] == "cancel-finished"
    assert completed["error"] == "" and completed["ov_cancel_reason"] == "user stop"


@pytest.mark.parametrize("stop_status", ["stopping", "stopped"])
def test_stop_after_native_ack_preserves_real_acceptance_receipt_and_exact_request(tmp_path, stop_status):
    async def check():
        owner = Owner(tmp_path)
        injected = False
        accepted_id = ""

        async def stop_after_ack(repository, job, accepted, started, revision=""):
            nonlocal injected, accepted_id
            injected = True
            accepted_id = accepted["task_id"]
            repository.update_job(
                job["job_id"], status=stop_status, finished_at="stop-finished", error="stop-error",
                ov_cancel_reason="user stop", ov_cancel_requested_at="stop-requested",
            )
            # Exercise the actual acceptance recorder using its stale pre-stop job.
            return await OvRoundOrchestrationMixin._record_round_acceptance(
                owner, repository, job, accepted, started, revision,
            )

        owner._record_round_acceptance = stop_after_ack
        job = await start(owner)
        assert injected and job["status"] == stop_status
        assert job["error"] == "stop-error" and job["finished_at"] == "stop-finished"
        assert accepted_id and job["ov_task_id"] == accepted_id and job["submission_finished_at"]
        assert isinstance(job["submission_seconds"], float)
        assert len(owner.ov.calls) == 1
        root, state = await owner._staged_load(job)
        current = state["current"]
        assert current["task_id"] == job["ov_task_id"]
        assert _read(root / current["request_file"]) == owner.ov.calls[0][1]
        original = (root / current["request_file"]).read_bytes()
        resumed = await owner._resume_staged_round(owner.repo.get_job(job["job_id"]))
        assert resumed["status"] in {"stopping", "stopped"}
        assert resumed["ov_task_id"] == accepted_id and len(owner.ov.calls) == 1
        assert (root / current["request_file"]).read_bytes() == original

    asyncio.run(check())


def test_stop_after_split_proof_before_save_remains_terminal_on_restart(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake()
        save = owner._staged_save
        injected = False
        rejected_files = {}

        async def stop_before_split_save(job, root, state, **changes):
            nonlocal injected, rejected_files
            if (state.get("validation_splits") and state.get("current") is None
                    and changes.get("status") == "preparing" and not injected):
                injected = True
                rejected_files = {str(path.relative_to(root)): path.read_bytes()
                                  for folder in (root / "children", root / "compilers")
                                  for path in folder.rglob("*") if path.is_file()}
                owner.repo.update_job(
                    job["job_id"], status="stopped", finished_at="stop-finished", error="stop-error",
                    ov_cancel_reason="user stopped while split proof was read",
                )
            return await save(job, root, state, **changes)

        owner._staged_save = stop_before_split_save
        job = await drive(owner, await begin(owner, 8))
        assert injected and job["status"] == "stopped"
        assert job["finished_at"] == "stop-finished" and job["error"] == "stop-error"
        root, state = await owner._staged_load(job)
        assert state["current"] is None and state["validation_splits"] == 1
        assert state["selection_cursor"] == 0 and not job["materialized"]
        before = len(owner.ov.calls)
        resumed = await owner._resume_staged_round(owner.repo.get_job(job["job_id"]))
        assert resumed["status"] == "stopped" and len(owner.ov.calls) == before
        assert resumed["error"] == "stop-error" and resumed["finished_at"] == "stop-finished"
        assert all((root / relative).read_bytes() == content for relative, content in rejected_files.items())

    asyncio.run(check())


def test_reviewed_waiting_human_can_continue_to_second_round_with_twelve_cases(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        job, entries = owner.new(4)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human" and not first["ov_cancel_reason"]
        second, entries = await next_inputs(owner, copy.deepcopy(first))
        assert second["status"] == "preparing" and second["current_round"] == 2
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human" and not second["ov_cancel_reason"]
        dataset = _read(owner.repo.workspace(second) / "compiled_skill/refund-review/benchmark.json")
        assert len(dataset["cases"]) == 12

    asyncio.run(check())
