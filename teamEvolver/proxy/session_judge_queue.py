"""Post-ingest async session quality judge (Good/Bad review).

The session value filter marks ``task_only`` / ``chitchat`` sessions as
``skipped`` at ingest time, so they never enter an evolution cycle — and the
cycle is the only place the session-level quality judge (dimension scores +
per-dimension reasons) used to run. Result: most archived sessions had a
binary ``value_judge`` verdict but no detailed review in the detail modal.

This module closes that gap without touching the ingest hot path: after a
session is archived it is pushed onto a bounded in-process queue, and a small
worker reviews it (metadata → trajectory → summarize → judge, reusing the
exact same stages as the evolution cycle) and writes the scores back onto the
archive + session index. All LLM work happens off the request path with
bounded concurrency; scoring failures are logged and dropped (never fatal).

One queue per process, keyed by ``(tenant_id, session_id)`` for dedupe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from ..tenants.registry import (
    DEFAULT_TENANT_ID,
    TenantContext,
    current_tenant_id,
    reset_current_tenant,
    set_current_tenant,
)

logger = logging.getLogger(__name__)

# Bound memory: a burst ingest (e.g. a large Langfuse pull) enqueues at most
# this many pending reviews; overflow is dropped (the admin backfill endpoint
# can sweep the remainder).
_DEFAULT_QUEUE_MAX = int(os.environ.get("TEAMEVOLVER_JUDGE_QUEUE_MAX", "500") or 500)
# Keep post-ingest reviews modest so they don't contend with evolution-cycle
# LLM work (global budget is <=8 across the process).
_DEFAULT_CONCURRENCY = max(
    1, int(os.environ.get("TEAMEVOLVER_JUDGE_CONCURRENCY", "2") or 2)
)


def _judge_concurrency() -> int:
    try:
        return max(1, min(8, int(os.environ.get("TEAMEVOLVER_JUDGE_CONCURRENCY", "2") or 2)))
    except ValueError:
        return _DEFAULT_CONCURRENCY


class AsyncSessionJudgeQueue:
    """Bounded dedupe queue + worker for off-request session judging."""

    def __init__(self, owner: Any, *, maxsize: int | None = None) -> None:
        self._owner = owner
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(
            maxsize=max(1, int(maxsize or _DEFAULT_QUEUE_MAX))
        )
        self._queued: set[tuple[str, str]] = set()
        self._inflight: set[tuple[str, str]] = set()
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # Bounds concurrent summarize+judge LLM chains across all workers.
        self._concurrency = _judge_concurrency()
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._started = False

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        workers = self._concurrency
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"session-judge-{i}")
            for i in range(workers)
        ]
        logger.info(
            "[SessionJudge] async judge queue started (workers=%d, queue_max=%d)",
            workers,
            self._queue.maxsize,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._started = False

    # -- enqueue ------------------------------------------------------------ #

    def pending(self, tenant_id: str, session_id: str) -> bool:
        key = (tenant_id, session_id)
        return key in self._queued or key in self._inflight

    def enqueue(self, tenant_id: str, session_id: str) -> bool:
        """Schedule one session for async review. No-op when full/dup/stopped."""
        if not self._started or self._stop.is_set():
            return False
        tid = str(tenant_id or DEFAULT_TENANT_ID)
        sid = str(session_id or "").strip()
        if not sid:
            return False
        key = (tid, sid)
        if key in self._queued or key in self._inflight:
            return False
        try:
            self._queue.put_nowait(key)
        except asyncio.QueueFull:
            logger.warning(
                "[SessionJudge] queue full (%d); dropping review for %s/%s",
                self._queue.maxsize,
                tid,
                sid,
            )
            return False
        self._queued.add(key)
        return True

    def backlog(self) -> int:
        return self._queue.qsize()

    # -- worker ------------------------------------------------------------- #

    async def _worker(self, index: int) -> None:
        while not self._stop.is_set():
            try:
                key = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                continue
            tenant_id, session_id = key
            self._queued.discard(key)
            self._inflight.add(key)
            try:
                await self._review_one(tenant_id, session_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad session never kills worker
                logger.warning(
                    "[SessionJudge] review failed for %s/%s",
                    tenant_id,
                    session_id,
                    exc_info=True,
                )
            finally:
                self._inflight.discard(key)
                self._queue.task_done()

    def _tenant_context(self, tenant_id: str) -> Optional[TenantContext]:
        try:
            from .tenant_routes import get_tenant_registry

            registry = get_tenant_registry(self._owner)
            if registry is None or registry.mode != "postgres":
                return registry.default_context() if registry is not None else None
            ctx = registry.get(tenant_id)
            return ctx if ctx is not None and ctx.status == "active" else None
        except Exception:  # noqa: BLE001
            return None

    async def _review_one(self, tenant_id: str, session_id: str) -> None:
        from ..session_store import SessionStore

        ctx = await asyncio.to_thread(self._tenant_context, tenant_id)
        if ctx is None and getattr(self._owner.config, "storage_pg_enabled", False):
            return
        token = set_current_tenant(ctx) if ctx is not None else None
        try:
            config = self._owner.config
            if ctx is not None and not ctx.is_default():
                from ..tenants.registry import effective_config

                config = effective_config(
                    self._tenant_registry(), ctx, self._owner.config
                )
            store = await asyncio.to_thread(
                SessionStore.from_config, config, tenant_id
            )
            already = await asyncio.to_thread(store.has_judge_score, session_id)
            if already:
                return
            session = await asyncio.to_thread(store.load_archived, session_id)
            if not isinstance(session, dict):
                return
            turns = session.get("turns")
            if not isinstance(turns, list) or not turns:
                # Nothing judgeable (metadata-only / pre-archive sessions).
                return

            engine = await asyncio.to_thread(self._owner._get_embedded_evolve_server, tenant_id)
            if engine is None:
                logger.debug(
                    "[SessionJudge] no engine for tenant %s; skip %s",
                    tenant_id,
                    session_id,
                )
                return
            if not bool(getattr(engine.config, "use_session_judge", True)):
                return

            scores = await self._judge(engine, session)
            if not scores:
                return
            judge_payload = self._judge_payload(scores)
            saved = await asyncio.to_thread(
                store.save_session_judge, session_id, judge_payload
            )
            if saved:
                # Drop the cached ledger so the next poll shows the new score
                # immediately instead of waiting for the 15s TTL. We're already
                # inside this tenant's context, so the scoped cache key matches.
                try:
                    from .routes import _invalidate_dashboard_cache

                    _invalidate_dashboard_cache(
                        f"conversations:{id(self._owner.config)}"
                    )
                except Exception:  # noqa: BLE001 - cache invalidation is best-effort
                    pass
                logger.info(
                    "[SessionJudge] scored %s/%s overall=%s",
                    tenant_id,
                    session_id,
                    judge_payload.get("overall_score"),
                )
        finally:
            if token is not None:
                reset_current_tenant(token)

    def _tenant_registry(self) -> Any:
        from .tenant_routes import get_tenant_registry

        return get_tenant_registry(self._owner)

    async def _judge(self, engine: Any, session: dict[str, Any]) -> Optional[dict[str, Any]]:
        from ..evolve.stages.judge import _should_skip_judging, judge_session
        from ..evolve.stages.summarize import (
            _extract_session_metadata,
            build_session_trajectory,
            summarize_session,
        )

        async with self._semaphore:
            _extract_session_metadata(session)
            session["_trajectory"] = build_session_trajectory(session)
            try:
                session["_summary"] = await summarize_session(engine._llm, session)
            except Exception:  # noqa: BLE001 - keep judging with an empty summary
                logger.debug(
                    "[SessionJudge] summarize failed for %s",
                    session.get("session_id"),
                    exc_info=True,
                )
                session["_summary"] = ""
            if _should_skip_judging(session):
                return None
            return await judge_session(engine._llm, session)

    @staticmethod
    def _judge_payload(scores: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime, timezone

        payload: dict[str, Any] = {
            "overall_score": scores.get("overall_score"),
            "rationale": str(scores.get("rationale") or ""),
            "reasons": scores.get("reasons") or {},
            "judged_at": datetime.now(timezone.utc).isoformat(),
            "judge_source": "post_ingest_async",
        }
        for dim in ("task_completion", "response_quality", "efficiency", "tool_usage"):
            value = scores.get(dim)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[dim] = float(value)
        return payload
