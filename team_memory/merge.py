"""Checkpointed Compile operations; shared protocol, deterministic keys."""
from __future__ import annotations

import asyncio

from teamEvolver.context.repository import stable_hash
from teamEvolver.openviking import CompileState
from teamEvolver.storage.postgres import PostgresObjectStore

from .repository import MemoryRepository
from .runs import guard


async def compile_stage(task, services, client, compiler, key, payload, renew):
    repo = MemoryRepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await guard(conn, services, task, allow_cancel=True)
        stage = await repo.stage(conn, task.task_id, key)
    if stage and stage["input_hash"] != stable_hash(payload):
        raise ValueError("Memory checkpoint input changed")
    if stage and stage["status"] == "succeeded":
        return stage["result_ref"]
    await renew()
    if not stage or not stage["upstream_task_id"]:
        dedupe = stable_hash([task.tenant_id, task.task_id, key])
        submitted = None
        if stage and stage["status"] == "running":
            from teamEvolver.openviking import OpenVikingError
            try:
                submitted = await compiler.submission(dedupe)
            except OpenVikingError as exc:
                if exc.status_code != 404:
                    raise
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task, allow_cancel=bool(stage and stage["status"] == "running"))
            await repo.save_stage(conn, task, key, payload, status="running", result={"input": payload})
        if submitted is None:
            submitted = await compiler.submit(kind="memory.compile", payload=payload, idempotency_key=dedupe)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task, allow_cancel=True)
            await repo.save_stage(conn, task, key, payload, status="running", upstream=submitted.task_id,
                                  result={"input": payload})
            await conn.execute(
                f"UPDATE {repo.schema}.worker_tasks SET upstream_task_id=$2 WHERE task_id=$1",
                task.task_id, submitted.task_id)
        upstream = submitted.task_id
    else:
        upstream = stage["upstream_task_id"]
    while True:
        await renew()
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            cancelled = await guard(conn, services, task, allow_cancel=True)
        if cancelled:
            await compiler.cancel(upstream)
        state = await compiler.status(upstream)
        if state.terminal:
            break
        await asyncio.sleep(0.5)
    if state.state != CompileState.SUCCEEDED or cancelled:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task, allow_cancel=True)
            await repo.save_stage(conn, task, key, payload, status=state.state.value, upstream=upstream)
        if cancelled or state.state == CompileState.CANCELLED:
            raise InterruptedError("Memory Compile cancelled")
        raise ValueError(f"Memory Compile {state.state.value}")
    ref = f"memory/intermediate/{task.task_id}/{stable_hash(key)}.json"
    store = PostgresObjectStore(services.tx, task.tenant_id)
    import json
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        cancelled = await guard(conn, services, task, allow_cancel=True)
        if cancelled:
            await repo.save_stage(conn, task, key, payload, status="cancelled", upstream=upstream)
        else:
            await store.put(conn, ref, json.dumps(state.result))
            await repo.save_stage(conn, task, key, payload, status="succeeded", upstream=upstream,
                                  result_ref=payload["to"], result={"artifact_ref": ref})
    if cancelled:
        raise InterruptedError("Memory Compile cancelled")
    return payload["to"]


def tree_levels(sources, fan_in, work_root):
    level = 0
    roots = list(sources)
    while len(roots) > fan_in:
        groups = []
        for index in range(0, len(roots), fan_in):
            inputs = roots[index:index + fan_in]
            output = f"{work_root}/merge-{level}-{index // fan_in}"
            groups.append((f"merge:{level}:{index // fan_in}", inputs, output))
        yield groups
        roots = [group[2] for group in groups]
        level += 1
    yield [("publish", roots, "")]
