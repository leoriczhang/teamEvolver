import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from team_miner.bridge import SkillMinerBridgeMixin
from team_miner.ov_compile_mining import OvSkillMiningRepository


def job_repo(tmp_path):
    r = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
    j = r.create_job(
        name="deadline test",
        account="default",
        source_uri="viking://resources/test",
        source_snapshot_uri="viking://resources/test-snapshot",
        output_uri="viking://resources/test-output",
        document_count=3,
        max_rounds=3,
        runtime_timeout_seconds=60,
    )
    j = r.update_job(
        j["job_id"],
        status="running",
        ov_task_id="fake",
        started_at=(datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(),
    )
    return r, j


@pytest.mark.asyncio
async def test_deadline_cancellation_and_terminal(tmp_path):
    r, j = job_repo(tmp_path)

    class Owner(SkillMinerBridgeMixin):
        status = "running"
        cancels = 0

        def _ov_skill_mining_repository(self):
            return r

        async def _knowledge_request(self, account, method, path, **kwargs):
            if method == "POST":
                self.cancels += 1
                return {"status": "cancelling"}
            return {"status": self.status, "stage": "compile: pipeline"}

    o = Owner()
    first = await o._refresh_ov_skill_job(j)
    assert first["status"] == "stopping" and not first["finished_at"] and o.cancels == 1
    second = await o._refresh_ov_skill_job(r.get_job(j["job_id"]))
    assert second["status"] == "stopping" and o.cancels == 1
    o.status = "cancelled"
    last = await o._refresh_ov_skill_job(r.get_job(j["job_id"]))
    assert last["status"] == "stopped" and last["finished_at"] and "60" in last["stop_reason"]


@pytest.mark.asyncio
async def test_terminal_wins_over_deadline(tmp_path):
    r, j = job_repo(tmp_path)

    class Owner(SkillMinerBridgeMixin):
        async def _knowledge_request(self, *a, **kw):
            raise AssertionError("must not cancel a terminal task")

    result = await Owner()._ov_enforce_deadline(r, j, "completed")
    assert not result["ov_cancel_requested_at"]


@pytest.mark.asyncio
async def test_cancel_transport_failure_retry(tmp_path):
    r, j = job_repo(tmp_path)

    class Owner(SkillMinerBridgeMixin):
        calls = 0

        async def _knowledge_request(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                raise OSError("offline")

    o = Owner()
    j = await o._ov_enforce_deadline(r, j, "running")
    assert j["status"] == "stopping" and j["ov_cancel_error"] == "offline" and not j["ov_cancel_sent_at"]
    j = await o._ov_enforce_deadline(r, j, "running")
    assert j["ov_cancel_sent_at"] and not j["ov_cancel_error"]


@pytest.mark.asyncio
async def test_monitor_single_instance_shutdown(tmp_path):
    r, j = job_repo(tmp_path)

    class Owner(SkillMinerBridgeMixin):
        observed = asyncio.Event()

        def _ov_skill_mining_repository(self):
            return r

        def _schedule_ov_skill_job_refresh(self, job, **kw):
            self.observed.set()

    o = Owner()
    o._start_ov_skill_monitor()
    t = o._ov_skill_monitor_task
    o._start_ov_skill_monitor()
    assert t is o._ov_skill_monitor_task
    await asyncio.wait_for(o.observed.wait(), 1)
    await o._stop_ov_skill_monitor()
    assert t.done()


def test_options_truthful_and_timeout_range(tmp_path):
    r, j = job_repo(tmp_path)
    assert j["current_round"] == 1 and j["human_checkpoints_requested"] and j["human_checkpoints_effective"]
    assert j["rounds_mode"] == "orchestrated"
    with pytest.raises(ValueError):
        r.create_job(
            name="bad",
            account="default",
            source_uri="viking://resources/test",
            source_snapshot_uri="viking://resources/snapshot",
            output_uri="viking://resources/output",
            document_count=1,
            max_rounds=3,
            runtime_timeout_seconds=59,
        )


def test_missing_reports_cannot_be_success(tmp_path):
    r, j = job_repo(tmp_path)
    with pytest.raises(ValueError, match="analysis/evidence-map.md"):
        r.finalize_materialized(j["job_id"])


@pytest.mark.asyncio
async def test_monitor_starts_in_existing_lifespan(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI

    r, _ = job_repo(tmp_path)

    class Owner(SkillMinerBridgeMixin):
        config = SimpleNamespace()

        def _ov_skill_mining_repository(self):
            return r

        def _schedule_ov_skill_job_refresh(self, job, **kwargs):
            pass

    app = FastAPI()
    owner = Owner()
    owner._register_skillminer_routes(app)
    async with app.router.lifespan_context(app):
        task = owner._ov_skill_monitor_task
        assert not task.done()
    assert task.done()


def test_actual_execution_events_keep_server_timestamps():
    observed = SkillMinerBridgeMixin._ov_task_observability(
        {},
        {
            "created_at": 1791450000.0,
            "created_at_iso": "2026-10-08T07:48:08+00:00",
            "execution_events": {
                "items": [
                    {
                        "kind": "stage_changed",
                        "stage": "compile: pipeline",
                        "status": "running",
                        "recorded_at": "2026-10-08T07:48:39+00:00",
                    }
                ],
                "dropped_count": 0,
            },
        },
        "compile: pipeline",
        "2026-10-08T09:00:00+00:00",
    )
    assert observed["ov_event_timeline"] == [
        {"label": "compile: pipeline", "at": "2026-10-08T07:48:39+00:00", "status": "running"}
    ]
    assert observed["ov_task_created_at"] == "2026-10-08T07:48:08+00:00"




@pytest.mark.parametrize("bad_field", ["checks", "session_ids"])
def test_actual_output_contract_rejects_numeric_rubric_or_missing_sources(tmp_path, bad_field):
    import json

    repository, job = job_repo(tmp_path)
    repository.write_output(job["job_id"], "analysis/evidence-map.md", "# Offline fixture evidence")
    repository.write_output(job["job_id"], "analysis/gaps.md", "# Offline fixture gaps")
    repository.write_output(
        job["job_id"],
        "compiled_skill/test-skill/SKILL.md",
        "---\nname: test-skill\ndescription: Offline contract fixture.\n---\n# Test skill",
    )
    repository.write_output(job["job_id"], "compiled_skill/test-skill/EVALUATION.md", "# Offline evaluation")
    cases = [
        {
            "input": f"Offline scenario {n}",
            "requirements": [f"Requirement {i}" for i in range(12)],
            "source_session_ids": ["viking://resources/test/document.md"],
        }
        for n in range(12)
    ]
    from team_miner.tests.dataset_fixtures import build_document

    document = build_document("test-skill", cases)
    if bad_field == "checks":
        for check in document["cases"][0]["checks"]:
            check["text"] = 123
    else:
        document["cases"][0]["provenance"]["session_ids"] = []
    repository.write_output(job["job_id"], "compiled_skill/test-skill/benchmark.json", json.dumps(document))
    with pytest.raises(ValueError):
        repository.finalize_materialized(job["job_id"])
    assert not repository.get_job(job["job_id"])["materialized"]


@pytest.mark.asyncio
async def test_remote_partial_output_never_publishes_success_during_materialization(tmp_path):
    repository, job = job_repo(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    class Owner(SkillMinerBridgeMixin):
        def _ov_skill_mining_repository(self):
            return repository

        async def _knowledge_request(self, *args, **kwargs):
            return {"status": "completed", "stage": "compile: completed"}

        async def _materialize_ov_skill_job(self, job):
            entered.set()
            await release.wait()
            raise ValueError("Missing required files")

    refresh = asyncio.create_task(Owner()._refresh_ov_skill_job(job))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        visible = repository.get_job(job["job_id"])
        assert visible["status"] == "running"
        assert not visible["finished_at"]
        assert not visible["materialized"]
    finally:
        release.set()
        result = await refresh
    assert result["status"] == "running"
    assert result["materialize_attempts"] == 1
