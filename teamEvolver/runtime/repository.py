"""Repository for the platform ``worker_tasks`` / ``resource_leases`` tables.

This is the only component that reads and writes the runtime's own tables. It
accesses platform tables only; business state stays in the business modules'
own repositories.

Design notes :

- ``worker_tasks`` is tenant-scoped with FORCE RLS, so every statement runs on a
 connection that already carries ``app.tenant_id``. The worker loop claims
 per-tenant and round-robins across tenants for fairness; within a tenant,
 claiming uses ``FOR UPDATE SKIP LOCKED`` so two workers never win the same row.
- Claiming increments ``fence`` and sets ``owner_id``/``attempt``. Every result
 write (``succeed``/``fail``/``retry``/...) is guarded by ``owner_id`` + ``fence``
 so a late write from a superseded attempt is rejected (returns ``None``).
- A ``running``/``reconciling`` task whose lease has expired is reclaimable; the
 reclaim bumps the fence, which is what fences the crashed owner's late writes.
- All time comparisons use the database clock (``now()``), never the app clock.

Each statement starts with a ``-- te:op=<name>`` marker. The marker is an inert
SQL comment for PostgreSQL; the faithful in-process test fake dispatches on it so
the queue contract can be verified without a local database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..database.transaction import TransactionManager

# Terminal states a task can never leave.
TERMINAL_STATES = ("succeeded", "failed", "cancelled")
# States from which a running attempt may write a result.
_ACTIVE_FOR_RESULT = ("running", "reconciling", "waiting_external")


@dataclass(frozen=True)
class ClaimedTask:
    """A task a worker has exclusively claimed for one attempt."""

    tenant_id: str
    task_id: str
    kind: str
    attempt: int
    fence: int
    subject_type: str
    subject_id: str
    payload_ref: str
    payload_hash: str
    config_revision: int
    adapter_revision: int
    trace_id: str
    upstream_task_id: str
    cancel_requested: bool
    owner_id: str


class WorkerTaskRepository:
    """Persistence for ``worker_tasks`` (enqueue, claim, heartbeat, result)."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    # -- enqueue ---------------------------------------------------------- #

    async def enqueue(
        self,
        conn: Any,
        *,
        tenant_id: str,
        task_id: str,
        kind: str,
        subject_type: str = "",
        subject_id: str = "",
        schema_version: str = "",
        dedupe_key: str = "",
        payload_ref: str = "",
        payload_hash: str = "",
        config_revision: int = 0,
        adapter_revision: int = 0,
        trace_id: str = "",
        available_in_seconds: float = 0.0,
    ) -> str:
        """Atomically append a ``queued`` task; dedupe on a live ``dedupe_key``.

        Returns the ``task_id`` of the enqueued task, or of the existing live
        task when ``dedupe_key`` already has one.
        """
        if dedupe_key:
            existing = await conn.fetchrow(
                f"-- te:op=enqueue_dedupe_lookup\n"
                f"SELECT task_id FROM {self._schema}.worker_tasks "
                "WHERE kind = $1 AND dedupe_key = $2 "
                "AND status NOT IN ('succeeded', 'failed', 'cancelled') "
                "LIMIT 1",
                kind,
                dedupe_key,
            )
            if existing is not None:
                return str(existing["task_id"])
        await conn.execute(
            f"-- te:op=enqueue\n"
            f"INSERT INTO {self._schema}.worker_tasks "
            "(tenant_id, task_id, kind, subject_type, subject_id, schema_version, "
            " dedupe_key, payload_ref, payload_hash, config_revision, adapter_revision, "
            " trace_id, status, attempt, fence, available_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'queued', 0, 0, "
            " now() + make_interval(secs => $13))",
            tenant_id,
            task_id,
            kind,
            subject_type,
            subject_id,
            schema_version,
            dedupe_key,
            payload_ref,
            payload_hash,
            int(config_revision),
            int(adapter_revision),
            trace_id,
            float(available_in_seconds),
        )
        return task_id

    # -- claim ------------------------------------------------------------ #

    async def claim(
        self,
        conn: Any,
        *,
        owner_id: str,
        lease_seconds: float,
        kinds: Sequence[str] | None = None,
    ) -> ClaimedTask | None:
        """Claim one ready task on this tenant-scoped connection.

        Ready = ``queued``/``retry_wait`` whose ``available_at`` has passed, or a
        ``running``/``reconciling``/``waiting_external`` task whose lease expired
        (crash recovery). The claim bumps ``fence`` and takes ``owner_id`` under
        ``FOR UPDATE SKIP LOCKED`` so concurrent workers never collide.
        """
        kind_list = list(kinds) if kinds else None
        row = await conn.fetchrow(
            f"-- te:op=claim\n"
            f"UPDATE {self._schema}.worker_tasks AS t SET "
            "  status = 'running', owner_id = $1, attempt = t.attempt + 1, "
            "  fence = t.fence + 1, lease_until = now() + make_interval(secs => $2), "
            "  updated_at = now() "
            "WHERE (t.tenant_id, t.task_id) = ( "
            f"  SELECT c.tenant_id, c.task_id FROM {self._schema}.worker_tasks AS c "
            "   WHERE c.available_at <= now() AND ( "
            "         c.status IN ('queued', 'retry_wait') "
            "      OR (c.status IN ('running', 'reconciling', 'waiting_external') "
            "          AND c.lease_until IS NOT NULL AND c.lease_until < now())) "
            "     AND ($3::text[] IS NULL OR c.kind = ANY($3)) "
            "   ORDER BY c.available_at, c.created_at "
            "   FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING t.tenant_id, t.task_id, t.kind, t.attempt, t.fence, t.subject_type, "
            "  t.subject_id, t.payload_ref, t.payload_hash, t.config_revision, "
            "  t.adapter_revision, t.trace_id, t.upstream_task_id, t.cancel_requested",
            owner_id,
            float(lease_seconds),
            kind_list,
        )
        if row is None:
            return None
        return ClaimedTask(
            tenant_id=str(row["tenant_id"]),
            task_id=str(row["task_id"]),
            kind=str(row["kind"]),
            attempt=int(row["attempt"]),
            fence=int(row["fence"]),
            subject_type=str(row["subject_type"]),
            subject_id=str(row["subject_id"]),
            payload_ref=str(row["payload_ref"]),
            payload_hash=str(row["payload_hash"]),
            config_revision=int(row["config_revision"]),
            adapter_revision=int(row["adapter_revision"]),
            trace_id=str(row["trace_id"]),
            upstream_task_id=str(row["upstream_task_id"]),
            cancel_requested=bool(row["cancel_requested"]),
            owner_id=owner_id,
        )

    # -- lease / heartbeat ------------------------------------------------ #

    async def require_current(self, conn: Any, task: ClaimedTask, *, allow_cancel=False) -> None:
        """Lock this attempt before a handler commits domain state."""
        row = await conn.fetchrow(
            f"SELECT task_id FROM {self._schema}.worker_tasks "
            "WHERE task_id=$1 AND tenant_id=$2 AND fence=$3 AND attempt=$4 "
            "AND owner_id=$5 "
            "AND status IN ('running','reconciling') AND lease_until>clock_timestamp() "
            "AND ($6 OR NOT cancel_requested) FOR UPDATE",
            task.task_id, task.tenant_id, task.fence, task.attempt, task.owner_id,
            allow_cancel,
        )
        if row is None:
            raise RuntimeError("worker attempt lost its lease or was cancelled")

    async def heartbeat(
        self, conn: Any, *, task_id: str, owner_id: str, fence: int, lease_seconds: float
    ) -> bool:
        """Renew the lease on a held task. Fails (``False``) if fenced/not owner.

        A worker that cannot renew must stop new external side effects ; the ``False`` return is that signal.
        """
        updated = await conn.fetchval(
            f"-- te:op=heartbeat\n"
            f"UPDATE {self._schema}.worker_tasks SET "
            "  lease_until = now() + make_interval(secs => $4), updated_at = now() "
            "WHERE task_id = $1 AND owner_id = $2 AND fence = $3 "
            "  AND status IN ('running', 'reconciling', 'waiting_external') "
            "  AND lease_until > clock_timestamp() "
            "RETURNING task_id",
            task_id,
            owner_id,
            int(fence),
            float(lease_seconds),
        )
        return updated is not None

    # -- result transitions (all fenced by owner_id + fence) -------------- #

    async def _transition(
        self,
        conn: Any,
        *,
        op: str,
        task_id: str,
        owner_id: str,
        fence: int,
        sets: str,
        args: list[Any],
        from_states: Sequence[str],
    ) -> bool:
        placeholders = ", ".join(f"'{s}'" for s in from_states)
        updated = await conn.fetchval(
            f"-- te:op={op}\n"
            f"UPDATE {self._schema}.worker_tasks SET {sets}, updated_at = now() "
            f"WHERE task_id = $1 AND owner_id = $2 AND fence = $3 "
            f"  AND status IN ({placeholders}) RETURNING task_id",
            task_id,
            owner_id,
            int(fence),
            *args,
        )
        return updated is not None

    async def succeed(self, conn: Any, *, task_id: str, owner_id: str, fence: int, result_ref: str = "") -> bool:
        """Mark the attempt succeeded. Rejected if a newer attempt fenced it."""
        return await self._transition(
            conn, op="succeed", task_id=task_id, owner_id=owner_id, fence=fence,
            sets="status = 'succeeded', result_ref = $4, error_code = ''",
            args=[result_ref], from_states=_ACTIVE_FOR_RESULT,
        )

    async def fail(self, conn: Any, *, task_id: str, owner_id: str, fence: int, error_code: str) -> bool:
        """Mark the attempt failed (terminal). Fenced by owner_id + fence."""
        return await self._transition(
            conn, op="fail", task_id=task_id, owner_id=owner_id, fence=fence,
            sets="status = 'failed', error_code = $4",
            args=[error_code], from_states=_ACTIVE_FOR_RESULT,
        )

    async def retry(
        self, conn: Any, *, task_id: str, owner_id: str, fence: int, backoff_seconds: float, error_code: str = ""
    ) -> bool:
        """Requeue the task after a backoff; a later attempt will reclaim it."""
        return await self._transition(
            conn, op="retry", task_id=task_id, owner_id=owner_id, fence=fence,
            sets=(
                "status = 'retry_wait', owner_id = '', lease_until = NULL, "
                "error_code = $5, available_at = now() + make_interval(secs => $4)"
            ),
            args=[float(backoff_seconds), error_code], from_states=("running", "reconciling", "waiting_external"),
        )

    async def wait_external(self, conn: Any, *, task_id: str, owner_id: str, fence: int, upstream_task_id: str) -> bool:
        """Park the task waiting on an external upstream (keeps the lease)."""
        return await self._transition(
            conn, op="wait_external", task_id=task_id, owner_id=owner_id, fence=fence,
            sets="status = 'waiting_external', upstream_task_id = $4",
            args=[upstream_task_id], from_states=("running",),
        )

    async def reconcile(self, conn: Any, *, task_id: str, owner_id: str, fence: int) -> bool:
        """Enter reconciliation when the upstream response is unknown."""
        return await self._transition(
            conn, op="reconcile", task_id=task_id, owner_id=owner_id, fence=fence,
            sets="status = 'reconciling'",
            args=[], from_states=("waiting_external", "running"),
        )

    # -- cancellation ----------------------------------------------------- #

    async def request_cancel(self, conn: Any, *, task_id: str) -> str | None:
        """Request cancellation. Non-started tasks go terminal immediately.

        ``queued``/``retry_wait`` → ``cancelled`` at once. In-flight tasks only
        get ``cancel_requested = TRUE``; the worker observes it cooperatively and
        does not abandon a non-idempotent external effect midway.
        Returns the resulting status, or ``None`` when the task is already
        terminal / unknown.
        """
        row = await conn.fetchrow(
            f"-- te:op=request_cancel\n"
            f"UPDATE {self._schema}.worker_tasks SET "
            "  cancel_requested = TRUE, "
            "  status = CASE WHEN status IN ('queued', 'retry_wait') THEN 'cancelled' ELSE status END, "
            "  updated_at = now() "
            "WHERE task_id = $1 AND status NOT IN ('succeeded', 'failed', 'cancelled') "
            "RETURNING status",
            task_id,
        )
        return None if row is None else str(row["status"])

    async def mark_cancelled(self, conn: Any, *, task_id: str, owner_id: str, fence: int) -> bool:
        """The worker acknowledges a cancel request and finalizes the task."""
        return await self._transition(
            conn, op="mark_cancelled", task_id=task_id, owner_id=owner_id, fence=fence,
            sets="status = 'cancelled'",
            args=[], from_states=_ACTIVE_FOR_RESULT,
        )

    async def requeue(self, conn: Any, *, task_id: str) -> str | None:
        """Operator/user re-queue of a *terminal-failed* task.

        Unlike :meth:`retry` (the worker's fenced, in-attempt backoff), this is
        the job-level ``POST /api/jobs/{id}/retry``: it moves a ``failed`` task
        back to ``queued`` for a fresh attempt. ``succeeded``/``cancelled`` and
        in-flight tasks are left untouched (returns ``None``) so a retry never
        resurrects a completed job or races a live attempt. The fence is *not*
        reset here — the next claim bumps it, fencing any late write.
        """
        row = await conn.fetchrow(
            f"-- te:op=requeue\n"
            f"UPDATE {self._schema}.worker_tasks SET "
            "  status = 'queued', owner_id = '', lease_until = NULL, "
            "  cancel_requested = FALSE, error_code = '', available_at = now(), "
            "  updated_at = now() "
            "WHERE task_id = $1 AND status = 'failed' "
            "RETURNING status",
            task_id,
        )
        return None if row is None else str(row["status"])

    # -- read ------------------------------------------------------------- #

    async def get(self, conn: Any, *, task_id: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"-- te:op=get\n"
            f"SELECT tenant_id, task_id, kind, status, attempt, fence, owner_id, "
            "  available_at, lease_until, cancel_requested, upstream_task_id, "
            "  result_ref, error_code, created_at, updated_at "
            f"FROM {self._schema}.worker_tasks WHERE task_id = $1",
            task_id,
        )
        return dict(row) if row is not None else None

    # -- usage accounting (logical tasks; retries do not double-charge) --- #

    async def charge_usage(
        self, conn: Any, *, tenant_id: str, metric: str, window_key: str, amount: int = 1
    ) -> None:
        """Increment ``tenant_usage`` once per logical task."""
        await conn.execute(
            f"-- te:op=charge_usage\n"
            f"INSERT INTO {self._schema}.tenant_usage (tenant_id, metric, window_key, counter) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tenant_id, metric, window_key) "
            "DO UPDATE SET counter = {s}.tenant_usage.counter + EXCLUDED.counter, "
            "updated_at = now()".format(s=self._schema),
            tenant_id,
            metric,
            window_key,
            int(amount),
        )


class ResourceLeaseRepository:
    """Cross-replica exclusivity for tenant concurrency and exclusive targets."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    async def acquire(
        self, conn: Any, *, tenant_id: str, resource_key: str, owner_id: str, lease_seconds: float
    ) -> int | None:
        """Acquire or extend a lease; returns the new fence, or ``None`` if held.

        The lease is grantable when it does not exist, has expired, or is already
        held by ``owner_id`` (renew). Taking it over an expired owner bumps the
        fence so the previous holder's late side effects are fenced out.
        """
        row = await conn.fetchrow(
            f"-- te:op=lease_acquire\n"
            f"INSERT INTO {self._schema}.resource_leases "
            "  (tenant_id, resource_key, owner_id, fence, lease_until) "
            "VALUES ($1, $2, $3, 1, now() + make_interval(secs => $4)) "
            "ON CONFLICT (tenant_id, resource_key) DO UPDATE SET "
            "  owner_id = EXCLUDED.owner_id, "
            "  fence = {s}.resource_leases.fence + 1, "
            "  lease_until = EXCLUDED.lease_until, updated_at = now() "
            "WHERE {s}.resource_leases.owner_id = EXCLUDED.owner_id "
            "   OR {s}.resource_leases.lease_until IS NULL "
            "   OR {s}.resource_leases.lease_until < now() "
            "RETURNING fence".format(s=self._schema),
            tenant_id,
            resource_key,
            owner_id,
            float(lease_seconds),
        )
        return None if row is None else int(row["fence"])

    async def heartbeat(
        self, conn: Any, *, tenant_id: str, resource_key: str, owner_id: str, fence: int, lease_seconds: float
    ) -> bool:
        """Renew a held lease; failure means the holder must stop side effects."""
        updated = await conn.fetchval(
            f"-- te:op=lease_heartbeat\n"
            f"UPDATE {self._schema}.resource_leases SET "
            "  lease_until = now() + make_interval(secs => $5), updated_at = now() "
            "WHERE tenant_id = $1 AND resource_key = $2 AND owner_id = $3 AND fence = $4 "
            "AND lease_until > clock_timestamp() "
            "RETURNING fence",
            tenant_id,
            resource_key,
            owner_id,
            int(fence),
            float(lease_seconds),
        )
        return updated is not None

    async def release(self, conn: Any, *, tenant_id: str, resource_key: str, owner_id: str, fence: int) -> bool:
        """Release a held lease (best-effort; expiry also frees it)."""
        updated = await conn.fetchval(
            f"-- te:op=lease_release\n"
            f"UPDATE {self._schema}.resource_leases SET owner_id = '', lease_until = NULL, updated_at = now() "
            "WHERE tenant_id = $1 AND resource_key = $2 AND owner_id = $3 AND fence = $4 "
            "RETURNING fence",
            tenant_id,
            resource_key,
            owner_id,
            int(fence),
        )
        return updated is not None
