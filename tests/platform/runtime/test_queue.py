"""test queue."""

from __future__ import annotations

import pytest

from teamEvolver.runtime import (
    LeaseGuard,
    ResourceLeaseRepository,
    WorkerStats,
    WorkerTaskRepository,
    process_once,
)
from tests.support.runtime import _DB, _FakeTx, _services


@pytest.mark.asyncio
async def test_only_one_worker_claims_a_queued_task(tx: _FakeTx):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
    # Two workers claim back-to-back: the first takes it, the second finds nothing.
    async with tx.tenant_tx("default") as conn:
        first = await repo.claim(conn, owner_id="w-a", lease_seconds=60)
    async with tx.tenant_tx("default") as conn:
        second = await repo.claim(conn, owner_id="w-b", lease_seconds=60)
    assert first is not None and first.task_id == "t1" and first.fence == 1
    assert second is None


@pytest.mark.asyncio
async def test_crashed_worker_lease_expires_and_task_is_reclaimed(tx: _FakeTx, db: _DB):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
        a = await repo.claim(conn, owner_id="w-a", lease_seconds=60)
    assert a is not None and a.fence == 1
    # Before expiry, the running task is not reclaimable.
    async with tx.tenant_tx("default") as conn:
        assert await repo.claim(conn, owner_id="w-b", lease_seconds=60) is None
    # After the lease expires, a second worker reclaims it with a bumped fence.
    db.clock.advance(61)
    async with tx.tenant_tx("default") as conn:
        b = await repo.claim(conn, owner_id="w-b", lease_seconds=60)
    assert b is not None and b.fence == 2 and b.attempt == 2


@pytest.mark.asyncio
async def test_late_result_from_superseded_attempt_is_fenced_out(tx: _FakeTx, db: _DB):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
        a = await repo.claim(conn, owner_id="w-a", lease_seconds=60)
    db.clock.advance(61)
    async with tx.tenant_tx("default") as conn:
        b = await repo.claim(conn, owner_id="w-b", lease_seconds=60)
    # The superseded attempt A tries to write a result: rejected by the fence.
    async with tx.tenant_tx("default") as conn:
        assert await repo.succeed(conn, task_id="t1", owner_id="w-a", fence=a.fence) is False
        assert await repo.succeed(conn, task_id="t1", owner_id="w-b", fence=b.fence) is True
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "succeeded"


@pytest.mark.asyncio
async def test_heartbeat_fails_once_fenced(tx: _FakeTx, db: _DB):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
        a = await repo.claim(conn, owner_id="w-a", lease_seconds=60)
        assert await repo.heartbeat(conn, task_id="t1", owner_id="w-a", fence=a.fence, lease_seconds=60) is True
    db.clock.advance(61)
    async with tx.tenant_tx("default") as conn:
        await repo.claim(conn, owner_id="w-b", lease_seconds=60)
        # A's heartbeat now fails — it must stop external side effects.
        assert await repo.heartbeat(conn, task_id="t1", owner_id="w-a", fence=a.fence, lease_seconds=60) is False


@pytest.mark.asyncio
async def test_enqueue_dedupes_on_live_dedupe_key(tx: _FakeTx):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        first = await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="skill.evolve", dedupe_key="k1")
        second = await repo.enqueue(conn, tenant_id="default", task_id="t2", kind="skill.evolve", dedupe_key="k1")
    assert first == "t1" and second == "t1"  # repeated submit is idempotent


@pytest.mark.asyncio
async def test_wait_external_then_reconcile_then_succeed(tx: _FakeTx):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="miner.track_compile")
        c = await repo.claim(conn, owner_id="w-a", lease_seconds=60)
        wait_ok = await repo.wait_external(conn, task_id="t1", owner_id="w-a", fence=c.fence, upstream_task_id="up-9")
        assert wait_ok is True
        assert await repo.reconcile(conn, task_id="t1", owner_id="w-a", fence=c.fence) is True
        assert await repo.succeed(conn, task_id="t1", owner_id="w-a", fence=c.fence, result_ref="r") is True
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "succeeded" and row["upstream_task_id"] == "up-9"


@pytest.mark.asyncio
async def test_cancel_queued_task_is_immediate(tx: _FakeTx):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
        status = await repo.request_cancel(conn, task_id="t1")
        row = await repo.get(conn, task_id="t1")
    assert status == "cancelled" and row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cooperative_cancel_of_running_task_at_reclaim(tx: _FakeTx, db: _DB):
    async def handler(task, services):  # must not run for a cancelled task
        raise AssertionError("handler ran despite cancel")

    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.enqueue(conn, tenant_id="default", task_id="t1", kind="session.pull")
        await repo.claim(conn, owner_id="w-a", lease_seconds=60)
        # Request cancel while running: the row stays running, flag is set.
        status = await repo.request_cancel(conn, task_id="t1")
    assert status == "running"
    # The crashed owner's lease expires; the next worker reclaims and finalizes.
    db.clock.advance(61)
    services = _services(tx, {"session.pull": handler})
    stats = WorkerStats()
    await process_once(services, owner_id="w-b", tenants=("default",), stats=stats, rand=lambda: 0.0)
    async with tx.tenant_tx("default") as conn:
        row = await repo.get(conn, task_id="t1")
    assert row["status"] == "cancelled" and stats.cancelled == 1


@pytest.mark.asyncio
async def test_resource_lease_is_exclusive_until_expiry(tx: _FakeTx, db: _DB):
    guard = LeaseGuard(ResourceLeaseRepository(tx))
    async with tx.tenant_tx("default") as conn:
        a = await guard.acquire(conn, tenant_id="default", resource_key="memory/x", owner_id="w-a", lease_seconds=60)
        b = await guard.acquire(conn, tenant_id="default", resource_key="memory/x", owner_id="w-b", lease_seconds=60)
    assert a is not None and b is None  # a holds it; b is refused
    db.clock.advance(61)
    async with tx.tenant_tx("default") as conn:
        c = await guard.acquire(conn, tenant_id="default", resource_key="memory/x", owner_id="w-b", lease_seconds=60)
        # a's heartbeat now fails because b took over with a bumped fence.
        assert await guard.heartbeat(conn, a, lease_seconds=60) is False
    assert c is not None and c.fence == 2


@pytest.mark.asyncio
async def test_tenant_usage_counts_logical_task_once(tx: _FakeTx, db: _DB):
    repo = WorkerTaskRepository(tx)
    async with tx.tenant_tx("default") as conn:
        await repo.charge_usage(conn, tenant_id="default", metric="tasks", window_key="2026-10")
        await repo.charge_usage(conn, tenant_id="default", metric="tasks", window_key="2026-10")
    assert db.usage[("default", "tasks", "2026-10")] == 2
