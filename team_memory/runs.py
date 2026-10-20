"""Short Memory acceptance and fenced run mutations."""
from __future__ import annotations

from teamEvolver.context.repository import stable_hash

from .repository import MemoryRepository


async def accept(conn, services, tenant, run_id, raw, payload):
    repo = MemoryRepository(services.tx)
    record, created = await repo.accept(conn, tenant, run_id, raw, payload)
    if created:
        await services.tasks.enqueue(
            conn, tenant_id=tenant, task_id=run_id, kind="memory.aggregate",
            subject_type="memory_run", subject_id=payload["target_uri"],
            payload_ref=run_id, payload_hash=record["input_hash"], dedupe_key=run_id,
            config_revision=payload["config_revision"],
        )
    return record


async def guard(conn, services, task, *, allow_cancel=False):
    await services.tasks.require_current(conn, task, allow_cancel=True)
    row = await services.tasks.get(conn, task_id=task.task_id)
    if row["cancel_requested"] and not allow_cancel:
        raise InterruptedError("Memory task cancelled")
    return row["cancel_requested"]


def input_hash(payload):
    return stable_hash(payload)
