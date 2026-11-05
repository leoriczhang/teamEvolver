"""Durable Worker entry points for Skill operations and Compile monitoring."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from starlette.exceptions import HTTPException

from teamEvolver.runtime import HandlerResult

from .repository import MiningRevisionConflict, PgMiningBackend
from .task_io import State, advance_compile, digest

logger = logging.getLogger(__name__)


async def guard(task, services):
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task, allow_cancel=True)
        row = await services.tasks.get(conn, task_id=task.task_id)
    return bool(row["cancel_requested"])


async def payload_for(task, services):
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        value = await State(services, task.tenant_id).get(conn, "payload", task.payload_ref)
    if value is None or digest(value) != task.payload_hash:
        raise ValueError("skill_mining_payload_hash_mismatch")
    return value


async def operation(task, services):
    from .engine import SkillMiningEngine
    from .job_operations import execute as execute_job
    from .source_operations import execute as execute_source

    state = State(services, task.tenant_id)
    if await guard(task, services):
        return HandlerResult.cancelled()
    payload = await payload_for(task, services)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        previous = await state.get(conn, "result", task.task_id)
    if previous is not None:
        return HandlerResult.succeed(task.task_id)
    settings = payload["settings"]
    if payload["job_id"]:
        current = await asyncio.to_thread(PgMiningBackend(services.tx, task.tenant_id).load_job, payload["job_id"])
        if current and current.get("config_snapshot"):
            settings = current["config_snapshot"]
    engine = SkillMiningEngine(services, task.tenant_id, settings, task=task)
    try:
        await engine.prepare()
        if payload["operation"].startswith("source.") or payload["operation"] == "ov.tree":
            result = await execute_source(payload["operation"], payload["body"], engine)
        elif payload["operation"] == "job.detail":
            result = await execute_job(payload["operation"], payload["body"], engine, payload["job_id"],
                                       identifier=task.task_id, submitted_by=payload["submitted_by"])
        else:
            body = payload["body"]
            if payload["operation"] == "candidate.submit":
                body = {**body, "_accepted_job_revision": payload.get("accepted_job_revision"),
                        "_accepted_files_revision": payload.get("accepted_files_revision")}
            lock_id = payload["job_id"] or "ovmine-" + hashlib.sha256(task.task_id.encode()).hexdigest()[:32]
            async with engine.job_lock(lock_id):
                result = await execute_job(payload["operation"], body, engine, payload["job_id"],
                                           identifier=task.task_id, submitted_by=payload["submitted_by"])
                name = payload["job_id"] or next((row["job_id"] for row in result.get("jobs", [])), "")
                if name and payload["operation"] not in {"job.delete", "candidate.submit"}:
                    from .presentation import publish
                    job = await asyncio.to_thread(engine.repository.get_job, name)
                    await publish(engine, job)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task, allow_cancel=True)
            await state.put(conn, "result", task.task_id, result)
        return HandlerResult.succeed(task.task_id)
    except MiningRevisionConflict as exc:
        return HandlerResult.fail("409:" + str(exc)[:950])
    except (KeyError, ValueError, FileNotFoundError, HTTPException) as exc:
        # These are domain rejections, not transient network/PG outages. Preserve
        # a readable reason on the durable receipt for the console.
        return HandlerResult.fail(str(getattr(exc, "detail", exc))[:1000])
    finally:
        if getattr(engine, "client", None) is not None:
            await engine.close()


async def compile_job(task, services):
    from .engine import SkillMiningEngine
    from .job_operations import restore
    from .presentation import publish

    payload = await payload_for(task, services)
    engine = SkillMiningEngine(services, task.tenant_id, payload["settings"], task=task)
    try:
        await engine.prepare()
        job = await restore(engine, payload["job_id"])
        cancelled = await guard(task, services)
        if cancelled and job.get("status") not in {"stopped", "succeeded", "failed", "interrupted"}:
            job = await engine.stop(job)
        active = job.get("status") in {"preparing", "queued", "running", "stopping"}
        if active or job.get("remote_terminal_unconfirmed"):
            if not job.get("ov_task_id") and job.get("status") in {"preparing", "stopping"}:
                job = await engine.submit(job)
            else:
                job = await engine.poll(job)
        await asyncio.to_thread(engine._checkpoint, job)
        await publish(engine, job)
        await advance_compile(services, task, payload["settings"],
                              delay=60 if job.get("remote_terminal_unconfirmed") else 2)
        return HandlerResult.succeed(job["job_id"])
    except Exception:
        # A lost submission/status response never authorizes duplicate remote
        # work or releases its admission slot. A new fenced owner reconciles.
        logger.exception("skill_mining.reconcile", extra={"task_id": task.task_id, "job_id": payload["job_id"]})
        try:
            await advance_compile(services, task, payload["settings"], delay=60)
            return HandlerResult.succeed(payload["job_id"])
        except Exception:
            # PG unavailability cannot lose either this fenced tick or its
            # successor; keep this attempt recoverable until the atomic handoff.
            return HandlerResult.reconcile()
    finally:
        if getattr(engine, "client", None) is not None:
            await engine.close()


compile_job.reconcile_cancellation = True


def register_handlers():
    return {"miner.skill_operation": operation, "miner.skill_compile": compile_job}
