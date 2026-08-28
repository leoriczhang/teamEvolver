from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.aggregation.service import (
    MemoryAggregationService,
    RunConflictError,
)
from teamEvolver.aggregation.state import AggregationState


def _service(tmp_path, *, batch_users: int = 8) -> MemoryAggregationService:
    return MemoryAggregationService(
        SimpleNamespace(
            aggregation_incremental_publish=True,
            aggregation_publish_batch_users=batch_users,
            aggregation_merge_concurrency=1,
            aggregation_phase1_concurrency=1,
            aggregation_compile_runtime_timeout_seconds=3000,
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            sharing_viking_account="default",
            sharing_viking_endpoint="http://openviking.example",
        )
    )


def _seed_state(service, state_path, target_uri, work_root, count):
    state = AggregationState(path=state_path, account_id="default", skill_fingerprint="skill-v1")
    roots = []
    for index in range(count):
        fp = f"sha256:{index:064x}"
        uri = service._staging_uri(
            f"user-{index}", target_uri, source_fingerprint=fp, work_root=work_root
        )
        roots.append(uri)
        state.mark_stage_ok(
            f"stage:user-{index}", fp, staging_uri=uri, source_count=1, total_bytes=1
        )
    state.save(skill_fingerprint="skill-v1")
    return roots


def test_incremental_publish_writes_each_batch_onto_target(tmp_path) -> None:
    service = _service(tmp_path, batch_users=2)
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    state_path = service._state_path("default", target_uri, "http://openviking.example")
    roots = _seed_state(service, state_path, target_uri, work_root, 5)

    calls: list[dict] = []

    class Client:
        async def run_batch(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    async def scenario() -> None:
        run = service.new_run("default", endpoint="http://openviking.example", target_uri=target_uri)
        run.work_root = work_root
        state = AggregationState.load(state_path, "default")
        ok = await service._publish_incremental(
            run=run,
            staged_roots=roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="rev-1",
            state=state,
        )
        assert ok is True

    asyncio.run(scenario())

    # 5 users / batch 2 -> 3 batches, each compiled onto the real target.
    assert len(calls) == 3
    assert all(c["target_uri"] == target_uri for c in calls)
    # Every staged root is covered exactly once across the batches.
    covered = [uri for c in calls for uri in c["source_uris"]]
    assert sorted(covered) == sorted(roots)
    assert all(len(c["source_uris"]) <= 2 for c in calls)


def test_incremental_publish_skips_unchanged_batches_on_rerun(tmp_path) -> None:
    service = _service(tmp_path, batch_users=2)
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    state_path = service._state_path("default", target_uri, "http://openviking.example")
    roots = _seed_state(service, state_path, target_uri, work_root, 4)

    calls: list[dict] = []

    class Client:
        async def run_batch(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    async def run_once() -> None:
        run = service.new_run("default", endpoint="http://openviking.example", target_uri=target_uri)
        run.work_root = work_root
        state = AggregationState.load(state_path, "default")
        ok = await service._publish_incremental(
            run=run, staged_roots=roots, client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="rev-1", state=state,
        )
        assert ok is True

    asyncio.run(run_once())
    first = len(calls)
    asyncio.run(run_once())
    # Second run: nothing changed, so no new compiles.
    assert first == 2
    assert len(calls) == first


def test_incremental_publish_bisects_on_page_limit_overflow(tmp_path) -> None:
    service = _service(tmp_path, batch_users=4)
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    state_path = service._state_path("default", target_uri, "http://openviking.example")
    roots = _seed_state(service, state_path, target_uri, work_root, 4)

    attempts: list[int] = []

    class Client:
        async def run_batch(self, **kwargs):
            n = len(kwargs["source_uris"])
            attempts.append(n)
            # A 4-source batch overflows 128; halves (<=2) succeed.
            if n > 2:
                return {"ok": False, "stderr": "output_pages 128 exceeded"}
            return {"ok": True}

    async def scenario() -> None:
        run = service.new_run("default", endpoint="http://openviking.example", target_uri=target_uri)
        run.work_root = work_root
        state = AggregationState.load(state_path, "default")
        ok = await service._publish_incremental(
            run=run, staged_roots=roots, client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="rev-1", state=state,
        )
        assert ok is True

    asyncio.run(scenario())

    # One 4-source attempt (fails), then two 2-source halves (succeed).
    assert attempts[0] == 4
    assert sorted(attempts[1:]) == [2, 2]


def test_same_target_concurrent_run_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    r1 = service.new_run("default", endpoint="http://openviking.example",
                         target_uri="viking://resources/team-memory")
    r2 = service.new_run("default", endpoint="http://openviking.example",
                         target_uri="viking://resources/team-memory")

    service._acquire_target(r1)
    try:
        assert service.target_is_active(r2) is True
        try:
            service._acquire_target(r2)
            assert False, "expected RunConflictError"
        except RunConflictError:
            pass
    finally:
        service._release_target(r1)

    # After release, the target is free again.
    assert service.target_is_active(r2) is False


def test_different_targets_run_in_parallel(tmp_path) -> None:
    service = _service(tmp_path)
    r1 = service.new_run("default", endpoint="http://openviking.example",
                         target_uri="viking://resources/team-a")
    r2 = service.new_run("default", endpoint="http://openviking.example",
                         target_uri="viking://resources/team-b")
    service._acquire_target(r1)
    try:
        # Different target -> not blocked, acquire succeeds.
        assert service.target_is_active(r2) is False
        service._acquire_target(r2)
        service._release_target(r2)
    finally:
        service._release_target(r1)


def test_run_route_returns_409_when_target_is_active(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    from fastapi.testclient import TestClient

    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.proxy import ProxyServer

    config = TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=False,
        sharing_skill_reload_mode="off",
        sharing_viking_endpoint="http://127.0.0.1:1933",
        sharing_viking_team_api_key="configured-root-secret",
        sharing_viking_account="default",
        sharing_viking_user="team",
        aggregation_state_dir=str(tmp_path),
    )
    server = ProxyServer(config)
    service = server._aggregation_service()

    # Simulate an in-flight run holding the target lock.
    holder = service.new_run(
        "default",
        endpoint="http://127.0.0.1:1933",
        target_uri="viking://resources/shared-knowledge",
    )
    service._acquire_target(holder)

    client = TestClient(server.app)
    resp = client.post(
        "/api/aggregation/run",
        json={
            "root_key": "external-root-secret",
            "target_uri": "viking://resources/shared-knowledge",
        },
    )
    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]

    service._release_target(holder)

