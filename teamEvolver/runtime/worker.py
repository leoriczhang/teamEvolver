"""Worker loop: claim → run handler → write fenced result.

Drives the ``worker_tasks`` state machine::

  queued -> running -> succeeded
           -> retry_wait -> queued
           -> waiting_external -> reconciling -> succeeded/failed
           -> failed
           -> cancelled

Invariants enforced here (not in the handler):

- Every result write is guarded by the claimed ``owner_id`` + ``fence``; a late
 write from a superseded attempt is silently rejected by the repository.
- Retries use the fixed 5/30/120s backoff plus jitter and a default cap of 3
 attempts; exhausting the cap fails the task.
- A cooperative ``cancel_requested`` observed at claim finalizes the task as
 ``cancelled`` before any external side effect, so a non-idempotent action is
 never abandoned midway.
- Claiming round-robins across tenants (``worker_tasks`` is RLS-scoped, so each
 claim runs on a tenant-scoped connection) for fairness.

The loop is role-gated: only ``worker``/``all`` run it; the ``api`` role never
starts a handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from ..tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from .repository import ClaimedTask
from .services import RuntimeServices
from .tasks import HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

# Fixed retry backoff schedule in seconds.
RETRY_BACKOFF_SECONDS = (5.0, 30.0, 120.0)
DEFAULT_MAX_ATTEMPTS = 3
# Loop-level recovery (database unavailable) is not a task retry: back off
# 1, 2, 4 … seconds capped here so a short outage resumes promptly.
LOOP_BACKOFF_MAX_SECONDS = 30.0
# Tenant list / scheduled-task refresh cadence.
TENANT_REFRESH_SECONDS = 30.0


def backoff_for_attempt(attempt: int, *, jitter: float = 0.0) -> float:
    """Return the backoff for the *attempt*-th try (1-based), with jitter.

    Attempts beyond the fixed schedule clamp to its last entry. ``jitter`` adds
    up to ``jitter`` fractional seconds of uniform noise to avoid thundering
    herds; callers pass a real random value, tests pass 0 for determinism.
    """
    idx = max(0, min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1))
    return RETRY_BACKOFF_SECONDS[idx] + max(0.0, jitter)


@dataclass
class WorkerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    cancelled: int = 0
    reconciling: int = 0
    waiting_external: int = 0


async def process_once(
    services: RuntimeServices,
    *,
    owner_id: str,
    lease_seconds: float = 60.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    tenants: Sequence[str] = ("default",),
    kinds: Sequence[str] | None = None,
    stats: WorkerStats | None = None,
    rand: Callable[[], float] | None = None,
) -> ClaimedTask | None:
    """Claim and process at most one task across *tenants* (round-robin).

    Returns the claimed task (after its result was written) or ``None`` when no
    tenant had a ready task. Separating this from the loop makes the state
    machine unit-testable without timers.
    """
    if not services.runs_worker:
        raise RuntimeError("process_once requires a worker-capable role")
    stats = stats or WorkerStats()
    rand = rand or (lambda: random.uniform(0.0, 1.0))

    for tenant_id in tenants:
        claimed = await _claim(services, tenant_id, owner_id=owner_id, lease_seconds=lease_seconds, kinds=kinds)
        if claimed is None:
            continue
        stats.claimed += 1
        # Cooperative cancellation observed at claim: finalize before any effect.
        reconcile_cancel = claimed.kind in services.registry and getattr(
            services.registry.get(claimed.kind), "reconcile_cancellation", False)
        if claimed.cancel_requested and not reconcile_cancel:
            await _write(
                services, claimed, HandlerResult.cancelled(),
                owner_id=owner_id, max_attempts=max_attempts, stats=stats, rand=rand,
            )
            return claimed
        try:
            handler = services.registry.get(claimed.kind)
            token = set_current_tenant(TenantContext(tenant_id=claimed.tenant_id))
            try:
                result = await _run_renewing(handler, claimed, services, owner_id, lease_seconds)
            finally:
                reset_current_tenant(token)
            if result is None:
                return claimed  # Lost lease: neither domain nor task state may be written.
        except Exception as exc:  # noqa: BLE001 - handler errors become task retries
            logger.exception("worker.handler_error", extra={"task_id": claimed.task_id, "kind": claimed.kind})
            result = HandlerResult.retry(error_code=f"handler_exception:{type(exc).__name__}")
        await _write(services, claimed, result, owner_id=owner_id, max_attempts=max_attempts, stats=stats, rand=rand)
        return claimed
    return None


async def _run_renewing(handler, task, services, owner_id, lease_seconds):
    work = asyncio.create_task(handler(task, services))
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=max(0.01, lease_seconds / 3))
            if done:
                return work.result()
            try:
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    alive = await services.tasks.heartbeat(
                        conn, task_id=task.task_id, owner_id=owner_id,
                        fence=task.fence, lease_seconds=lease_seconds,
                    )
            except Exception:
                alive = False
            if not alive:
                return None
    finally:
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work


async def _claim(services: RuntimeServices, tenant_id: str, *, owner_id: str, lease_seconds: float, kinds):
    async with services.tx.tenant_tx(tenant_id) as conn:
        return await services.tasks.claim(conn, owner_id=owner_id, lease_seconds=lease_seconds, kinds=kinds)


async def _write(
    services: RuntimeServices,
    task: ClaimedTask,
    result: HandlerResult,
    *,
    owner_id: str,
    max_attempts: int,
    stats: WorkerStats,
    rand: Callable[[], float],
) -> None:
    repo = services.tasks
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        guard = {"task_id": task.task_id, "owner_id": owner_id, "fence": task.fence}
        outcome = result.outcome
        if outcome == HandlerOutcome.SUCCEED:
            if await repo.succeed(conn, result_ref=result.result_ref, **guard):
                stats.succeeded += 1
        elif outcome == HandlerOutcome.FAIL:
            if await repo.fail(conn, error_code=result.error_code or "handler_failed", **guard):
                stats.failed += 1
        elif outcome == HandlerOutcome.CANCELLED:
            if await repo.mark_cancelled(conn, **guard):
                stats.cancelled += 1
        elif outcome == HandlerOutcome.WAIT_EXTERNAL:
            if await repo.wait_external(conn, upstream_task_id=result.upstream_task_id, **guard):
                stats.waiting_external += 1
        elif outcome == HandlerOutcome.RECONCILE:
            if await repo.reconcile(conn, **guard):
                stats.reconciling += 1
        elif outcome == HandlerOutcome.RETRY:
            if task.attempt >= max_attempts:
                if await repo.fail(conn, error_code="retries_exhausted", **guard):
                    stats.failed += 1
            else:
                backoff = result.backoff_seconds or backoff_for_attempt(task.attempt, jitter=rand())
                if await repo.retry(conn, backoff_seconds=backoff, error_code=result.error_code, **guard):
                    stats.retried += 1
        else:  # pragma: no cover
            raise ValueError(f"unknown handler outcome: {outcome!r}")


async def run_worker(
    services: RuntimeServices,
    *,
    owner_id: str,
    stop_event: asyncio.Event,
    tenants: Sequence[str] = ("default",),
    tenant_source: Callable[[], Awaitable[Sequence[str]]] | None = None,
    lease_seconds: float = 60.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    idle_sleep: float = 1.0,
    kinds: Sequence[str] | None = None,
    on_health: Callable[[bool], None] | None = None,
) -> WorkerStats:
    """Run the worker claim/execute loop until *stop_event* is set.

    Backs off ``idle_sleep`` seconds when a full round of tenants yields nothing,
    so an empty queue does not spin. Stops claiming as soon as the stop event is
    observed.

    ``tenant_source`` (when given) is re-read every refresh interval so tenants
    created after the worker started are claimed without a restart.

    A database outage (claim, refresh or result write raising) must not end the
    loop: the worker reports itself unhealthy through *on_health*, backs off
    (capped exponential) and resumes once PostgreSQL answers again. A task whose
    result could not be written keeps its lease and is reclaimed by the normal
    lease-expiry path.
    """
    stats = WorkerStats()
    active = tuple(tenants)
    last_refresh = 0.0
    failures = 0
    while not stop_event.is_set():
        try:
            now = asyncio.get_running_loop().time()
            if now - last_refresh > TENANT_REFRESH_SECONDS or not last_refresh:
                if tenant_source is not None:
                    active = tuple(await tenant_source())
                last_refresh = now
                scheduler = services.extras.get("scheduled_tasks")
                for tenant in active if scheduler else ():
                    token = set_current_tenant(TenantContext(tenant_id=tenant))
                    try:
                        await scheduler(tenant)
                    except Exception:
                        logger.exception("worker.schedule_error", extra={"tenant": tenant})
                    finally:
                        reset_current_tenant(token)
            claimed = await process_once(
                services,
                owner_id=owner_id,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
                tenants=active,
                kinds=kinds,
                stats=stats,
            )
        except Exception:  # noqa: BLE001 - infrastructure errors must not end the loop
            failures += 1
            logger.exception("worker.loop_error", extra={"consecutive_failures": failures})
            if on_health is not None:
                on_health(False)
            delay = min(LOOP_BACKOFF_MAX_SECONDS, 2.0 ** (failures - 1)) + random.uniform(0.0, 1.0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            continue
        if failures:
            failures = 0
            logger.info("worker.loop_recovered")
            if on_health is not None:
                on_health(True)
        if claimed is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep)
            except asyncio.TimeoutError:
                pass
    return stats
