"""Worker-computed display snapshots; API reads never rebuild a mining workspace."""

from __future__ import annotations

import asyncio

from .mining_completion import revision_checkpoint
from .repository import decode
from .task_io import State

_HIDDEN = {"workspace", "input_manifest", "compile_request", "workspace_reset_pending", "compiler_path",
           "staged_state_path", "native_allowed_source_uris", "config_snapshot", "compiler_body_snapshot",
           "delete_receipt", "_deleted", "local_path"}


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if key not in _HIDDEN}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def metadata(job):
    result = clean(job)
    result["optional_revision"] = revision_checkpoint(job)
    result["remote_state_stale"] = bool(job.get("status") in {"preparing", "queued", "running", "stopping"}
                                        and job.get("ov_sync_error"))
    return result


async def publish(engine, job):
    if not job or job.get("_deleted"):
        return
    backend = engine.repository.backend
    value = clean(await asyncio.to_thread(engine.repository.public_job, job, detail=True))
    async with engine.services.tx.tenant_tx(engine.tenant) as conn:
        if engine.task is not None:
            await engine.services.tasks.require_current(conn, engine.task, allow_cancel=True)
        row = await conn.fetchrow(
            f"SELECT revision,files_revision FROM {engine.services.tx.pool.schema}.skill_mining_jobs "
            "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR SHARE", engine.tenant, job["job_id"],
        )
        if not row or (int(row["revision"]) != backend._job_revisions.get(job["job_id"])
                       or int(row["files_revision"]) != backend._file_revisions.get(job["job_id"])):
            return
        await State(engine.services, engine.tenant).put(conn, "view", job["job_id"], {
            "revision": int(row["revision"]), "files_revision": int(row["files_revision"]), "job": value,
        })


async def read(services, tenant, job_id):
    async with services.tx.tenant_tx(tenant) as conn:
        row = await conn.fetchrow(
            f"SELECT document,revision,files_revision FROM {services.tx.pool.schema}.skill_mining_jobs "
            "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR SHARE", tenant, job_id,
        )
        if row is None:
            raise KeyError(job_id)
        job = decode(row["document"])
        view = await State(services, tenant).get(conn, "view", job_id)
    current = bool(view and view["revision"] == row["revision"] and view["files_revision"] == row["files_revision"])
    return job, clean(view["job"]) if current else None
