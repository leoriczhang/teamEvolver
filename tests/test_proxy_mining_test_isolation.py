"""A test service's background monitor must never observe operator mining jobs."""

from __future__ import annotations

import asyncio

import pytest

from team_miner import bridge
from team_miner.ov_compile_mining import OvSkillMiningRepository
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer


def _server(tmp_path):
    return ProxyServer(
        TeamEvolverConfig(
            _config_file=str(tmp_path / "config.yaml"),
            skills_runtime_cache_dir=str(tmp_path / "skills"),
            sharing_skill_mirror_enabled=False,
        )
    )


@pytest.mark.asyncio
async def test_default_test_service_monitor_cannot_read_or_mutate_operator_job_sentinel(tmp_path, monkeypatch):
    # Model the production path entirely under tmp_path; do not inspect or
    # touch the operator's actual running task files even in this regression.
    production_project = tmp_path / "operator-checkout"
    production_state = tmp_path / "operator-config" / "skill_mining"
    operator_repository = OvSkillMiningRepository(production_state, production_project)
    sentinel = operator_repository.create_job(
        name="Operator running task",
        account="default",
        source_uri="viking://resources/operator/source",
        source_snapshot_uri="viking://resources/operator/frozen",
        output_uri="viking://resources/operator/output",
        document_count=1,
        max_rounds=2,
    )
    sentinel = operator_repository.update_job(sentinel["job_id"], status="running", ov_task_id="operator-remote-task")
    sentinel_path = operator_repository.jobs_root / sentinel["job_id"] / "job.json"
    before = sentinel_path.read_bytes()
    monkeypatch.setattr(bridge, "_SKILLMINER_ROOT", production_project)
    monkeypatch.setattr(bridge, "CONFIG_DIR", production_state.parent)
    server = _server(tmp_path)
    test_repository = server._ov_skill_mining_repository()
    assert test_repository.jobs_root != operator_repository.jobs_root
    assert test_repository.list_jobs() == []

    loop = asyncio.get_running_loop()
    inspected = asyncio.Event()
    observed_jobs = []
    original_list = test_repository.list_jobs

    def monitored_list():
        rows = original_list()
        observed_jobs.extend(rows)
        loop.call_soon_threadsafe(inspected.set)
        return rows

    monkeypatch.setattr(test_repository, "list_jobs", monitored_list)
    server._start_ov_skill_monitor()
    try:
        await asyncio.wait_for(inspected.wait(), timeout=2)
        assert observed_jobs == []
    finally:
        await server._stop_ov_skill_monitor()
    assert sentinel_path.read_bytes() == before
    assert not (sentinel_path.parent / "run.log").exists()


def test_explicit_test_repository_is_preserved(tmp_path):
    server = _server(tmp_path)
    explicit = OvSkillMiningRepository(tmp_path / "explicit-state", tmp_path / "explicit-project")
    server._ov_skill_mining_repo = explicit
    assert server._ov_skill_mining_repository() is explicit
