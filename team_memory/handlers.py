"""Memory Worker lifecycle with a PG lease for each account's output tree."""

from __future__ import annotations

import asyncio
import contextlib

from teamEvolver.runtime import HandlerResult

from .merge import compile_stage
from .openviking import clients
from .pipeline import pipeline
from .repository import MemoryRepository
from .runs import guard


async def aggregate(task, services):
    repo = MemoryRepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        record = await repo.get(conn, task.payload_ref)
        cancelled = await guard(conn, services, task, allow_cancel=True)
    if record["input_hash"] != task.payload_hash:
        return HandlerResult.fail("memory_input_hash_mismatch")
    if record["result"] is not None:
        return HandlerResult.succeed(task.task_id)
    payload = record["input"]
    # Account-wide exclusivity includes ancestors/descendants and aliases of a
    # target, avoiding a race between checking overlap and acquiring a row.
    key = "memory:account:" + payload["account_id"]
    owner = task.task_id
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await guard(conn, services, task, allow_cancel=True)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", task.tenant_id + ":" + key)
        blocked = await conn.fetchval(
            f"SELECT 1 FROM {repo.schema}.resource_leases l JOIN {repo.schema}.worker_tasks t "
            "ON t.tenant_id=l.tenant_id AND t.task_id=l.owner_id WHERE l.resource_key=$1 "
            "AND l.owner_id<>$2 AND t.status IN ('running','reconciling','retry_wait') LIMIT 1",
            key, task.task_id)
        blocked = blocked or await conn.fetchval(
            f"SELECT 1 FROM {repo.schema}.memory_stages s JOIN {repo.schema}.memory_runs r "
            "USING(tenant_id,run_id) WHERE s.status='running' AND r.input->>'account_id'=$1 "
            "AND s.run_id<>$2 LIMIT 1", payload["account_id"], task.task_id)
        if blocked:
            return HandlerResult.retry("memory_target_busy", backoff_seconds=5)
        fence = await services.leases.acquire(
            conn, tenant_id=task.tenant_id, resource_key=key, owner_id=owner, lease_seconds=60)
    if fence is None:
        return HandlerResult.retry("memory_target_busy", backoff_seconds=5)

    async def renew():
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task, allow_cancel=True)
            alive = await services.leases.heartbeat(
                conn, tenant_id=task.tenant_id, resource_key=key, owner_id=owner, fence=fence, lease_seconds=60)
            if not alive:
                raise RuntimeError("Memory resource lease lost")

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            await renew()

    async def recover_and_run():
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            active = await conn.fetch(
                f"SELECT stage_key FROM {repo.schema}.memory_stages WHERE run_id=$1 AND status='running'",
                task.task_id)
            stages = [await repo.stage(conn, task.task_id, r["stage_key"]) for r in active]
        if stages:
            client, compiler = await clients(services, payload)
            try:
                for stage in stages:
                    await compile_stage(task, services, client, compiler, stage["stage_key"],
                                        stage["result"]["input"], renew)
            finally:
                await client.aclose()
        if cancelled:
            raise InterruptedError("Memory task cancelled")
        return await pipeline(task, services, payload, renew)

    work = asyncio.create_task(recover_and_run())
    pulse = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait({work, pulse}, return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            pulse.result()
        await work
        return HandlerResult.succeed(task.task_id)
    except InterruptedError:
        return HandlerResult.cancelled()
    except ValueError:
        return HandlerResult.fail("memory_pipeline_invalid")
    except Exception:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            active = await conn.fetchval(
                f"SELECT 1 FROM {repo.schema}.memory_stages WHERE run_id=$1 AND status='running' LIMIT 1",
                task.task_id)
        if active:
            return HandlerResult.reconcile()
        raise
    finally:
        for pending in (work, pulse):
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            active = await conn.fetchval(
                f"SELECT 1 FROM {repo.schema}.memory_stages WHERE run_id=$1 AND status='running' LIMIT 1",
                task.task_id)
            if not active:
                await services.leases.release(
                    conn, tenant_id=task.tenant_id, resource_key=key, owner_id=owner, fence=fence)


aggregate.reconcile_cancellation = True


async def replay(task, services):
    runner = services.extras.get("memory_replay_handler")
    if runner is None:
        return HandlerResult.fail("memory_replay_handler_unavailable")
    return await runner(task, services)


def register_handlers():
    return {"memory.aggregate": aggregate, "memory.compile": aggregate, "memory.replay": replay}
