"""Atomic, tenant-scoped Worker receipts for Skill mining operations."""

from __future__ import annotations

import hashlib
import json
import uuid

from starlette.exceptions import HTTPException

from teamEvolver.storage.postgres import PostgresObjectStore
from teamEvolver.tenants.registry import current_tenant_id

from .config import freeze
from .repository import decode, encode

_ACTIVE = {"preparing", "queued", "running", "stopping"}
_RESERVED = {"_accepted_job_revision", "_accepted_files_revision"}


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class State:
    def __init__(self, services, tenant):
        self.store = PostgresObjectStore(services.tx, tenant)

    async def get(self, conn, kind, identifier):
        try:
            return json.loads(await self.store.get(conn, f"skill_miner/{kind}/{identifier}"))
        except FileNotFoundError:
            return None

    async def put(self, conn, kind, identifier, value):
        data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        await self.store.put(conn, f"skill_miner/{kind}/{identifier}", data)


async def accept(request, services, operation, raw, settings, *, job_id=""):
    tenant = current_tenant_id()
    key = request.headers.get("Idempotency-Key", "")
    if len(key) > 200:
        raise HTTPException(400, "Idempotency-Key too long")
    if _RESERVED.intersection(raw):
        raise HTTPException(400, "Worker revision fields cannot be supplied by the client")
    identifier = "skill-op:" + (digest([tenant, operation, key]) if key else uuid.uuid4().hex)
    state = State(services, tenant)
    payload = {"operation": operation, "body": raw, "settings": freeze(settings), "job_id": job_id,
               "submitted_by": str((getattr(request.state, "console_user", None) or {}).get("id") or "admin")}
    async with services.tx.tenant_tx(tenant) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", identifier)
        previous = await state.get(conn, "payload", identifier)
        if previous:
            prior_request = [previous["operation"], previous["body"], previous["job_id"]]
            if digest(prior_request) != digest([operation, raw, job_id]):
                raise HTTPException(409, "Idempotency-Key reused with different request")
        else:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                               tenant + ":skill-mining-operation-queue")
            stopping_active = False
            if operation == "job.detail":
                pending = await conn.fetchval(
                    f"SELECT task_id FROM {services.tx.pool.schema}.worker_tasks WHERE tenant_id=$1 "
                    "AND kind='miner.skill_operation' AND subject_type='skill_mining_detail' AND subject_id=$2 "
                    "AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1", tenant, job_id,
                )
                if pending:
                    return receipt(pending)
            if operation == "job.stop":
                stopping_active = await request_stop(conn, services, tenant, job_id, settings)
                pending = await conn.fetchval(
                    f"SELECT task_id FROM {services.tx.pool.schema}.worker_tasks WHERE tenant_id=$1 "
                    "AND kind='miner.skill_operation' AND subject_type='skill_mining_stop' AND subject_id=$2 "
                    "AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1", tenant, job_id,
                )
                if pending:
                    identifier = pending
                    return receipt(identifier)
            if operation == "candidate.submit":
                row = await conn.fetchrow(
                    f"SELECT revision,files_revision,document FROM {services.tx.pool.schema}.skill_mining_jobs "
                    "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR SHARE", tenant, job_id,
                )
                if row is None:
                    raise HTTPException(404, "挖掘任务不存在")
                if decode(row["document"]).get("account") != settings.miner_config["account_id"]:
                    raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
                payload["accepted_job_revision"] = int(row["revision"])
                payload["accepted_files_revision"] = int(row["files_revision"])
            queued = await conn.fetchval(f"SELECT count(*) FROM {services.tx.pool.schema}.worker_tasks "
                                        "WHERE kind LIKE 'miner.skill_%' AND status IN ('queued','retry_wait')")
            if queued >= 100 and not stopping_active:
                raise HTTPException(429, "Skill mining Worker queue is full")
            await state.put(conn, "payload", identifier, payload)
            await services.tasks.enqueue(conn, tenant_id=tenant, task_id=identifier, kind="miner.skill_operation",
                subject_type={"job.stop": "skill_mining_stop", "job.detail": "skill_mining_detail"}.get(
                    operation, "skill_mining"),
                subject_id=job_id, payload_ref=identifier,
                payload_hash=digest(payload), config_revision=settings.miner_revision, dedupe_key=identifier)
    return receipt(identifier)


def receipt(identifier):
    return {"ok": True, "job_id": identifier, "status": "queued", "status_url": "/api/mined-jobs/" + identifier}


async def request_stop(conn, services, tenant, job_id, settings):
    """Persist stop intent and cancel monitors in the acceptance transaction."""
    from .mining_contracts import utc_now

    schema = services.tx.pool.schema
    # Domain writers lock their Worker row before the job. Keep that order to
    # avoid a stop request and a running monitor deadlocking each other.
    monitors = await conn.fetch(
        f"SELECT task_id FROM {schema}.worker_tasks WHERE tenant_id=$1 "
        "AND kind='miner.skill_compile' AND subject_id=$2 AND status NOT IN ('succeeded','failed','cancelled') "
        "ORDER BY task_id FOR UPDATE", tenant, job_id,
    )
    row = await conn.fetchrow(
        f"SELECT document FROM {schema}.skill_mining_jobs WHERE tenant_id=$1 AND job_id=$2 "
        "AND NOT deleted FOR UPDATE", tenant, job_id,
    )
    if row is None:
        raise HTTPException(404, "挖掘任务不存在")
    value = decode(row["document"])
    if value.get("account") != settings.miner_config["account_id"]:
        raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
    active = value.get("status") in _ACTIVE
    if active:
        if value.get("status") == "preparing" and not value.get("ov_task_id") and not value.get("compile_request"):
            value.update(status="stopped", finished_at=utc_now(), stop_reason="用户手动停止")
        else:
            value.update(status="stopping", ov_cancel_reason="用户手动停止",
                         ov_cancel_requested_at=value.get("ov_cancel_requested_at") or utc_now())
        value["updated_at"] = utc_now()
        await conn.execute(
            f"UPDATE {schema}.skill_mining_jobs SET status=$3,document=$4::jsonb,"
            "revision=revision+1,updated_at=now() WHERE tenant_id=$1 AND job_id=$2",
            tenant, job_id, value["status"], encode(value),
        )
        await conn.execute(
            f"UPDATE {schema}.worker_tasks SET cancel_requested=TRUE WHERE tenant_id=$1 "
            "AND task_id=ANY($2::text[])", tenant, [row["task_id"] for row in monitors],
        )
    return active


def compile_generation(job):
    return digest([job["job_id"], int(job.get("current_round") or 1), job.get("submission_idempotency_key", "")])


async def enqueue_compile(services, tenant, job, settings):
    """Dedupe this submission generation without reusing a finishing older round."""
    identifier = "skill-run:" + uuid.uuid4().hex
    state = State(services, tenant)
    generation = compile_generation(job)
    payload = {"job_id": job["job_id"], "settings": settings, "generation": generation}
    async with services.tx.tenant_tx(tenant) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "skill-run:" + job["job_id"])
        existing = await conn.fetchval(f"SELECT task_id FROM {services.tx.pool.schema}.worker_tasks "
            "WHERE kind='miner.skill_compile' AND subject_id=$1 AND status NOT IN ('succeeded','failed','cancelled')",
            job["job_id"])
        if existing:
            return existing
        await state.put(conn, "payload", identifier, payload)
        await services.tasks.enqueue(conn, tenant_id=tenant, task_id=identifier, kind="miner.skill_compile",
            subject_type="skill_mining", subject_id=job["job_id"], payload_ref=identifier,
            payload_hash=digest(payload), config_revision=settings["miner_revision"], dedupe_key=identifier)
    return identifier


async def advance_compile(services, task, settings, *, delay=2):
    """Atomically finish one monitor tick and queue its successor when needed."""
    state = State(services, task.tenant_id)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "skill-run:" + task.subject_id)
        await services.tasks.require_current(conn, task, allow_cancel=True)
        row = await conn.fetchrow(
            f"SELECT document FROM {services.tx.pool.schema}.skill_mining_jobs "
            "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR SHARE", task.tenant_id, task.subject_id,
        )
        job = decode(row["document"]) if row else {}
        active = job.get("status") in _ACTIVE or bool(job.get("remote_terminal_unconfirmed"))
        if active:
            existing = await conn.fetchval(
                f"SELECT task_id FROM {services.tx.pool.schema}.worker_tasks "
                "WHERE kind='miner.skill_compile' AND subject_id=$1 AND task_id<>$2 "
                "AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1", task.subject_id, task.task_id,
            )
            if existing is None:
                identifier = "skill-run:" + uuid.uuid4().hex
                payload = {"job_id": task.subject_id, "settings": job.get("config_snapshot") or settings,
                           "generation": compile_generation(job)}
                await state.put(conn, "payload", identifier, payload)
                await services.tasks.enqueue(
                    conn, tenant_id=task.tenant_id, task_id=identifier, kind="miner.skill_compile",
                    subject_type="skill_mining", subject_id=task.subject_id, payload_ref=identifier,
                    payload_hash=digest(payload), config_revision=payload["settings"].get("miner_revision", 0),
                    dedupe_key=identifier, available_in_seconds=delay,
                )
        if task.cancel_requested and not active:
            completed = await services.tasks.mark_cancelled(
                conn, task_id=task.task_id, owner_id=task.owner_id, fence=task.fence)
        else:
            completed = await services.tasks.succeed(
                conn, task_id=task.task_id, owner_id=task.owner_id, fence=task.fence, result_ref=task.subject_id)
        if not completed:
            raise RuntimeError("monitor tick lost its Worker fence")
