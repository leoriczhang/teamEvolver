"""Independent tenant supervisors, with one fenced owner across worker replicas."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from ..llm import configure_tenant_model_capacity
from ..tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from .capacity import read_capacity
from .leases import LeaseGuard
from .worker import run_worker

logger = logging.getLogger(__name__)
OWNER_RESOURCE = "runtime:tenant-worker"
OWNER_LEASE_SECONDS = 60.0


async def _pause(stop, seconds):
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def _schedule(services, tenant, stop):
    scheduler = services.extras.get("scheduled_tasks")
    while scheduler and not stop.is_set():
        token = set_current_tenant(TenantContext(tenant_id=tenant))
        try:
            await scheduler(tenant)
        except Exception:
            logger.exception("worker.schedule_error", extra={"tenant": tenant})
        finally:
            reset_current_tenant(token)
        await _pause(stop, 30.0)


async def _owned_lanes(services, tenant, owner, stop, handle, guard, report, refresh):
    lanes = {}
    scheduler_stop = asyncio.Event()
    scheduler = asyncio.create_task(_schedule(services, tenant, scheduler_stop))
    health = {}

    def lane_health(key, healthy):
        health[key] = healthy
        report(all(health.values()))

    try:
        while True:
            async with services.tx.tenant_tx(tenant) as conn:
                if not await guard.heartbeat(conn, handle, lease_seconds=OWNER_LEASE_SECONDS):
                    raise RuntimeError("tenant worker ownership lease lost")
            _, capacity = await read_capacity(services, tenant)
            configure_tenant_model_capacity(tenant, capacity.llm_max_concurrency, capacity.llm_queue_capacity)
            desired = {} if stop.is_set() else {
                (name, index): kinds
                for name, (kinds, count) in capacity.lanes(services.registry.kinds()).items()
                for index in range(count)
            }
            shrinking = {
                name for name, _ in lanes
                if sum(key[0] == name for key in lanes) > sum(key[0] == name for key in desired)
            }
            for key, (task, lane_stop) in list(lanes.items()):
                if key not in desired or key[0] in shrinking:
                    lane_stop.set()  # shrink/disable drains existing attempts before freeing ownership
                if task.done():
                    task.result()
                    lanes.pop(key)
                    health.pop(key, None)
            for key, kinds in desired.items():
                if key in lanes:
                    continue  # a draining slot still counts until its handler finishes
                if any(slot[0] == key[0] and lane_stop.is_set() for slot, (_, lane_stop) in lanes.items()):
                    continue  # do not refill while a retired slot still consumes the old budget
                lane_stop = asyncio.Event()
                health[key] = True
                lanes[key] = (asyncio.create_task(run_worker(
                    services, owner_id=f"{owner}:{tenant}:{key[0]}-{key[1]}",
                    stop_event=lane_stop, tenants=(tenant,), kinds=kinds, schedule_tasks=False,
                    on_health=lambda healthy, key=key: lane_health(key, healthy),
                ), name=f"tenant-{tenant}-{key[0]}-{key[1]}"), lane_stop)
            if stop.is_set():
                scheduler_stop.set()
                if not lanes:
                    break
            report(all(health.values()))
            # Keep renewing ownership during graceful drain.
            await asyncio.sleep(refresh)
    finally:
        scheduler_stop.set()
        scheduler.cancel()
        for task, lane_stop in lanes.values():
            lane_stop.set()
            task.cancel()
        await asyncio.gather(scheduler, *(task for task, _ in lanes.values()), return_exceptions=True)


async def _tenant_worker(services, tenant, owner, stop, report, refresh):
    guard = LeaseGuard(services.leases)
    while not stop.is_set():
        handle = None
        try:
            await read_capacity(services, tenant)  # missing configuration fails closed
            async with services.tx.tenant_tx(tenant) as conn:
                handle = await guard.acquire(
                    conn, tenant_id=tenant, resource_key=OWNER_RESOURCE, owner_id=owner,
                    lease_seconds=OWNER_LEASE_SECONDS,
                )
            report(True)
            if handle:
                logger.info("worker.tenant_owned tenant=%s owner=%s", tenant, owner)
                await _owned_lanes(services, tenant, owner, stop, handle, guard, report, refresh)
        except Exception:
            report(False)
            logger.exception("worker.tenant_loop_error", extra={"tenant": tenant})
        finally:
            if handle:
                with contextlib.suppress(Exception):
                    async with services.tx.tenant_tx(tenant) as conn:
                        await guard.release(conn, handle)
        await _pause(stop, refresh)


async def run_tenant_workers(
    services, *, owner_id, stop_event, tenant_source, on_health, refresh_seconds=5.0,
):
    """Discover tenants independently of handlers and drain disabled tenant work."""
    workers, health = {}, {}
    source_healthy = True

    def report(tenant, healthy):
        health[tenant] = healthy
        on_health(source_healthy and all(health.values()))

    try:
        while not stop_event.is_set():
            try:
                active = set(await tenant_source())
                source_healthy = True
            except Exception:
                source_healthy = False
                on_health(False)
                logger.exception("worker.tenant_refresh_error")
                await _pause(stop_event, refresh_seconds)
                continue
            for tenant, (task, tenant_stop) in list(workers.items()):
                if tenant not in active:
                    tenant_stop.set()
                if task.done():
                    task.result()
                    workers.pop(tenant)
                    health.pop(tenant, None)
            for tenant in sorted(active - workers.keys()):
                tenant_stop = asyncio.Event()
                health[tenant] = True
                workers[tenant] = (asyncio.create_task(_tenant_worker(
                    services, tenant, owner_id, tenant_stop,
                    lambda healthy, tenant=tenant: report(tenant, healthy), refresh_seconds,
                ), name=f"tenant-{tenant}"), tenant_stop)
            on_health(all(health.values()))
            await _pause(stop_event, refresh_seconds)
        for _, tenant_stop in workers.values():
            tenant_stop.set()
        await asyncio.gather(*(task for task, _ in workers.values()))
    finally:
        for task, tenant_stop in workers.values():
            tenant_stop.set()
            task.cancel()
        await asyncio.gather(*(task for task, _ in workers.values()), return_exceptions=True)
