"""Operations router factory.

Exposes ``create_router`` for the composition root. Hosts the unified task
(job) interface the fixes in §5.1::

  GET /api/jobs/{job_id}
  POST /api/jobs/{job_id}/cancel
  POST /api/jobs/{job_id}/retry

These read/drive the platform ``worker_tasks`` table through the runtime
``WorkerTaskRepository`` — the only state a job exposes. The router performs no
long work itself : cancel is cooperative and
retry only re-queues a terminally-failed job.

The job is addressed by its ``task_id``; the tenant comes from the bound
``TenantContext`` so RLS scopes every read/write to the caller's tenant — a job
from another tenant is simply not found.
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.exceptions import HTTPException

from ...tenants.registry import current_tenant_id


def create_router(services=None, **kwargs) -> APIRouter:
    """Return the operations ``APIRouter`` for the composition root to mount.

    ``services`` is the :class:`~teamEvolver.runtime.services.RuntimeServices`
    container the composition root built. When absent (offline route inspection)
    the factory still returns a valid empty-contract router so the composition
    guardrail holds without a live database.
    """
    router = APIRouter(tags=["operations"])
    if services is None:
        return router

    tx = services.tx
    tasks = services.tasks

    def _public(job: dict) -> dict:
        """Project a worker_tasks row to the public job view (no internals)."""
        return {
            "job_id": job["task_id"],
            "kind": job["kind"],
            "status": job["status"],
            "attempt": job["attempt"],
            "cancel_requested": job["cancel_requested"],
            "upstream_task_id": job.get("upstream_task_id") or "",
            "result_ref": job.get("result_ref") or "",
            "error_code": job.get("error_code") or "",
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }

    async def _load(tenant_id: str, job_id: str) -> dict:
        async def _body(conn):
            return await tasks.get(conn, task_id=job_id)

        job = await _run_tenant(tx, tenant_id, _body)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):  # noqa: ANN202
        tenant_id = current_tenant_id()
        return _public(await _load(tenant_id, job_id))

    @router.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):  # noqa: ANN202
        tenant_id = current_tenant_id()
        await _load(tenant_id, job_id)  # 404 before any write

        async def _body(conn):
            return await tasks.request_cancel(conn, task_id=job_id)

        status = await _run_tenant(tx, tenant_id, _body)
        if status is None:
            # Already terminal: report the current state instead of a spurious error.
            return _public(await _load(tenant_id, job_id))
        return {"job_id": job_id, "status": status, "cancel_requested": True}

    @router.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str):  # noqa: ANN202
        tenant_id = current_tenant_id()
        job = await _load(tenant_id, job_id)
        if job["status"] != "failed":
            raise HTTPException(
                status_code=409,
                detail=f"only a failed job can be retried (status={job['status']})",
            )

        async def _body(conn):
            return await tasks.requeue(conn, task_id=job_id)

        status = await _run_tenant(tx, tenant_id, _body)
        if status is None:  # raced to a non-failed state between read and write
            raise HTTPException(status_code=409, detail="job is no longer failed")
        return {"job_id": job_id, "status": status}

    return router


async def _run_tenant(tx, tenant_id: str, body):
    """Run ``await body(conn)`` inside one tenant-scoped transaction.

    Mirrors ``TransactionManager.run_tenant_tx`` but stays on the event loop the
    request handler already runs on (the pool's own IO thread owns the asyncpg
    connection; the handler only awaits the result).
    """
    async with tx.tenant_tx(tenant_id) as conn:
        return await body(conn)
