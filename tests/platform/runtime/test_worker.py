"""test worker."""

from __future__ import annotations

import pytest

from teamEvolver.runtime import (
    RETRY_BACKOFF_SECONDS,
    HandlerResult,
    WorkerStats,
    WorkerTaskRepository,
    build_services,
    process_once,
)
from tests.support.runtime import _DB, _FakeTx, _FlakyTx, _services


@pytest.mark.asyncio
async def test_worker_runs_handler_to_success(tx: _FakeTx):
    async def handler(task, services):
        return HandlerResult.succeed(result_ref="artifact://r1")

    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
    services = _services(tx, {"session.pull": handler})
    stats = WorkerStats()
    claimed = await process_once(services, owner_id="w-a", tenants=("default",), stats=stats, rand=lambda: 0.0)
    assert claimed is not None and stats.succeeded == 1
    async with tx.tenant_tx("default") as conn:
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "succeeded" and row["result_ref"] == "artifact://r1"


@pytest.mark.asyncio
async def test_retry_backoff_then_exhaustion_fails(tx: _FakeTx, db: _DB):
    async def handler(task, services):
        return HandlerResult.retry(error_code="transient")

    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
    services = _services(tx, {"session.pull": handler})
    stats = WorkerStats()

    # Attempt 1 → retry_wait, parked for 5s.
    await process_once(services, owner_id="w-a", tenants=("default",), stats=stats, rand=lambda: 0.0)
    async with tx.tenant_tx("default") as conn:
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "retry_wait"
    # Not claimable before the backoff elapses.
    assert await process_once(services, owner_id="w-a", tenants=("default",), stats=stats, rand=lambda: 0.0) is None

    db.clock.advance(RETRY_BACKOFF_SECONDS[0])
    await process_once(services, owner_id="w-a", tenants=("default",), stats=stats, rand=lambda: 0.0)  # attempt 2
    db.clock.advance(RETRY_BACKOFF_SECONDS[1])
    # attempt 3 → fail (exhausted)
    await process_once(services, owner_id="w-a", tenants=("default",), stats=stats, rand=lambda: 0.0)
    async with tx.tenant_tx("default") as conn:
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "failed" and row["error_code"] == "retries_exhausted"
    assert stats.retried == 2 and stats.failed == 1


@pytest.mark.asyncio
async def test_handler_exception_becomes_retry(tx: _FakeTx):
    async def handler(task, services):
        raise RuntimeError("boom")

    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
    services = _services(tx, {"session.pull": handler})
    await process_once(services, owner_id="w-a", tenants=("default",), rand=lambda: 0.0)
    async with tx.tenant_tx("default") as conn:
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "retry_wait" and row["error_code"].startswith("handler_exception:")


@pytest.mark.asyncio
async def test_worker_loop_survives_database_outage_and_resumes(db: _DB, monkeypatch):
    import asyncio

    from teamEvolver.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "LOOP_BACKOFF_MAX_SECONDS", 0.01)
    monkeypatch.setattr(worker_module.random, "uniform", lambda *_: 0.0)
    tx = _FlakyTx(db, outage=3)
    done = asyncio.Event()

    async def handler(task, services):
        done.set()
        return HandlerResult.succeed()

    async def tenants():
        return ("default",)

    async with _FakeTx.tenant_tx(tx, "default") as conn:
        await WorkerTaskRepository(tx).enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
    health: list[bool] = []
    stop = asyncio.Event()
    loop = asyncio.create_task(
        worker_module.run_worker(
            _services(tx, {"session.pull": handler}),
            owner_id="w-a",
            stop_event=stop,
            tenant_source=tenants,
            idle_sleep=0.01,
            on_health=health.append,
        )
    )
    await asyncio.wait_for(done.wait(), timeout=5)
    stop.set()
    stats = await asyncio.wait_for(loop, timeout=5)
    assert stats.succeeded == 1
    assert health == [False, False, False, True]
    assert db.tasks[("default", "t1")].status == "succeeded"


@pytest.mark.asyncio
async def test_worker_loop_picks_up_tenants_created_after_start(tx: _FakeTx, monkeypatch):
    import asyncio

    from teamEvolver.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "TENANT_REFRESH_SECONDS", 0.0)
    seen = asyncio.Event()
    active = ["default"]

    async def handler(task, services):
        seen.set()
        return HandlerResult.succeed()

    async def tenants():
        return tuple(active)

    stop = asyncio.Event()
    loop = asyncio.create_task(
        worker_module.run_worker(
            _services(tx, {"session.pull": handler}),
            owner_id="w-a",
            stop_event=stop,
            tenant_source=tenants,
            idle_sleep=0.01,
        )
    )
    await asyncio.sleep(0.05)
    active.append("late")
    async with tx.tenant_tx("late") as conn:
        await WorkerTaskRepository(tx).enqueue(conn, tenant_id="late", task_id="t1", kind="session.pull")
    await asyncio.wait_for(seen.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(loop, timeout=5)


@pytest.mark.asyncio
async def test_readiness_follows_worker_health_and_loop_liveness(tx: _FakeTx):
    import asyncio

    from teamEvolver.runtime import build_lifecycle

    async def crashing(stop):
        raise RuntimeError("worker loop ended")

    lifecycle = build_lifecycle(build_services(role="worker", tx=tx, handlers={}), worker_main=crashing)
    await lifecycle.startup()
    await asyncio.sleep(0)
    assert lifecycle.ready is False  # a dead worker loop is not ready

    healthy = build_lifecycle(build_services(role="worker", tx=tx, handlers={}))
    await healthy.startup()
    assert healthy.ready is True
    healthy.set_healthy(False)
    assert healthy.ready is False
    healthy.set_healthy(True)
    assert healthy.ready is True
    await healthy.shutdown()
    assert healthy.ready is False
    await lifecycle.shutdown()
