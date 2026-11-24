"""Capacity isolation and live configuration with real worker state transitions."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from teamEvolver import llm
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.tasks import HandlerResult


@pytest.mark.asyncio
async def test_full_model_capacity_does_not_block_other_tenant():
    release = threading.Event()
    started = threading.Barrier(9)
    first = llm._dispatcher_for("isolation-first", 8, 16)
    second = llm._dispatcher_for("isolation-second", 1, 2)

    def blocked():
        started.wait(timeout=3)
        release.wait(timeout=5)

    tasks = [asyncio.create_task(first.call(blocked)) for _ in range(8)]
    try:
        await asyncio.to_thread(started.wait, 3)
        assert await asyncio.wait_for(second.call(lambda: "available"), 0.5) == "available"
    finally:
        release.set()
        await asyncio.gather(*tasks)
        llm._reset_dispatchers_for_testing()


@pytest.mark.asyncio
async def test_model_resize_keeps_inflight_calls_in_same_budget():
    release, started = threading.Event(), threading.Event()
    original = llm._dispatcher_for("resize", 2, 8)

    def blocked():
        started.set()
        release.wait(timeout=3)

    task = asyncio.create_task(original.call(blocked))
    try:
        await asyncio.to_thread(started.wait, 1)
        resized = llm._dispatcher_for("resize", 1, 4)
        assert resized is original
        waiting = asyncio.create_task(resized.call(lambda: "finished"))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        release.set()
        assert await asyncio.wait_for(waiting, 1) == "finished"
    finally:
        release.set()
        await task
        llm._reset_dispatchers_for_testing()


@pytest.mark.asyncio
async def test_cancelled_model_request_keeps_slot_until_http_call_finishes():
    release, started = threading.Event(), threading.Event()
    dispatcher = llm._dispatcher_for("cancelled-request", 1, 1)

    def blocked():
        started.set()
        release.wait(timeout=3)

    task = asyncio.create_task(dispatcher.call(blocked))
    try:
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(llm.LLMOverloadedError):
            await dispatcher.call(lambda: "must not start")
    finally:
        release.set()
        await asyncio.to_thread(llm._reset_dispatchers_for_testing)


@pytest.mark.asyncio
async def test_tenant_lanes_new_tenant_and_replicas(tx, db):
    from teamEvolver.runtime.tenant_workers import run_tenant_workers

    blocked, first_started, other_done, archive_done = (asyncio.Event() for _ in range(4))
    active = ["busy"]
    stop = asyncio.Event()

    async def analyze(task, services):
        if task.tenant_id == "busy":
            first_started.set()
            await blocked.wait()
        else:
            other_done.set()
        return HandlerResult.succeed()

    async def archive(task, services):
        archive_done.set()
        return HandlerResult.succeed()

    services = build_services(role="worker", tx=tx, handlers={
        "session.analyze": analyze, "session.archive": archive,
    }, extras={"config_repo": SimpleNamespace(read_tenant_module=AsyncMock(
        return_value=SimpleNamespace(config={"analysis_concurrency": 1}, revision=1)))})
    for tenant, kind in (("busy", "session.analyze"), ("busy", "session.archive"), ("new", "session.analyze")):
        async with tx.tenant_tx(tenant) as conn:
            await services.tasks.enqueue(conn, tenant_id=tenant, task_id=kind, kind=kind)

    async def tenants():
        return tuple(active)

    runners = [asyncio.create_task(run_tenant_workers(
        services, owner_id=owner, stop_event=stop, tenant_source=tenants,
        on_health=lambda _: None, refresh_seconds=0.02,
    )) for owner in ("replica-a", "replica-b")]
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(archive_done.wait(), 1)
        active.append("new")
        await asyncio.wait_for(other_done.wait(), 1)
        assert db.tasks[("busy", "session.analyze")].attempt == 1
        assert db.tasks[("new", "session.analyze")].attempt == 1
        assert len([lease for lease in db.leases.values() if lease.owner_id]) == 2
    finally:
        blocked.set()
        stop.set()
        await asyncio.gather(*runners)


@pytest.mark.asyncio
async def test_shrink_drains_and_disabled_tenant_stops_claiming(tx, db):
    from teamEvolver.runtime.tenant_workers import run_tenant_workers

    started, release = asyncio.Event(), asyncio.Event()
    active = ["tenant"]
    slots = {"analysis_concurrency": 2, "llm_max_concurrency": 2}
    stop = asyncio.Event()
    calls = []

    async def analyze(task, services):
        calls.append(task.task_id)
        if len(calls) == 2:
            started.set()
        await release.wait()
        return HandlerResult.succeed()

    async def read(*args):
        return SimpleNamespace(config=dict(slots), revision=1)

    services = build_services(role="worker", tx=tx, handlers={"session.analyze": analyze},
                              extras={"config_repo": SimpleNamespace(read_tenant_module=read)})
    async with tx.tenant_tx("tenant") as conn:
        for index in range(3):
            await services.tasks.enqueue(conn, tenant_id="tenant", task_id=str(index), kind="session.analyze")

    async def tenants():
        return tuple(active)

    runner = asyncio.create_task(run_tenant_workers(
        services, owner_id="owner", stop_event=stop, tenant_source=tenants,
        on_health=lambda _: None, refresh_seconds=0.01,
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        slots["analysis_concurrency"] = 1
        slots["llm_max_concurrency"] = 1
        await asyncio.sleep(0.04)
        assert llm._DISPATCHERS["tenant"].concurrency == 1
        active.clear()
        await asyncio.sleep(0.04)
        release.set()
        await asyncio.sleep(0.08)
        assert len(calls) == 2
        assert db.tasks[("tenant", "2")].status == "queued"
        assert all(row.status == "succeeded" for key, row in db.tasks.items() if key[1] != "2")
    finally:
        release.set()
        stop.set()
        await runner


@pytest.mark.asyncio
async def test_shrink_does_not_refill_while_retired_slot_is_running(tx, db):
    from teamEvolver.runtime.tenant_workers import run_tenant_workers

    releases = [asyncio.Event(), asyncio.Event()]
    started, third = asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()
    slots = {"analysis_concurrency": 2}
    calls = []

    async def analyze(task, services):
        index = len(calls)
        calls.append(task.task_id)
        if index < 2:
            if index == 1:
                started.set()
            await releases[index].wait()
        else:
            third.set()
        return HandlerResult.succeed()

    async def read(*args):
        return SimpleNamespace(config=dict(slots), revision=1)

    services = build_services(role="worker", tx=tx, handlers={"session.analyze": analyze},
                              extras={"config_repo": SimpleNamespace(read_tenant_module=read)})
    async with tx.tenant_tx("tenant") as conn:
        for index in range(3):
            await services.tasks.enqueue(conn, tenant_id="tenant", task_id=str(index), kind="session.analyze")
    runner = asyncio.create_task(run_tenant_workers(
        services, owner_id="owner", stop_event=stop, tenant_source=AsyncMock(return_value=("tenant",)),
        on_health=lambda _: None, refresh_seconds=0.01,
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        slots["analysis_concurrency"] = 1
        await asyncio.sleep(0.04)
        releases[0].set()
        await asyncio.sleep(0.06)
        assert not third.is_set()
        releases[1].set()
        await asyncio.wait_for(third.wait(), 1)
    finally:
        for release in releases:
            release.set()
        stop.set()
        await runner
