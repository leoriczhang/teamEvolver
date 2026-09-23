"""Compatibility queue for backfilling historical Session analysis.

New Sessions complete merged classification, summary, and scoring before
ingest can persist them. This queue remains for administrative backfills of
older archived Sessions. A worker runs the same mandatory ``analyze_session``
stage and writes the scores back onto the archive and Session index.

One queue per process, keyed by ``(tenant_id, session_id)`` for dedupe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any, Optional

from teamEvolver.tenants.registry import (
    DEFAULT_TENANT_ID,
    TenantContext,
    reset_current_tenant,
    set_current_tenant,
)

logger = logging.getLogger(__name__)

# Bound memory: a burst ingest (e.g. a large Langfuse pull) enqueues at most
# this many pending reviews; overflow is dropped (the admin backfill endpoint
# can sweep the remainder).
_DEFAULT_QUEUE_MAX = int(os.environ.get("TEAMEVOLVER_JUDGE_QUEUE_MAX", "500") or 500)
# Keep historical backfills modest at the worker layer. Their model calls also
# enter the selected tenant's dispatcher, so they cannot consume another
# tenant's API concurrency.
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
            from teamEvolver.proxy.tenant_routes import get_tenant_registry

            registry = get_tenant_registry(self._owner)
            if registry is None or registry.mode != "postgres":
                return registry.default_context() if registry is not None else None
            ctx = registry.get(tenant_id)
            return ctx if ctx is not None and ctx.status == "active" else None
        except Exception:  # noqa: BLE001
            return None

    async def _review_one(self, tenant_id: str, session_id: str) -> None:
        from teamEvolver.session_store import SessionStore

        ctx = await asyncio.to_thread(self._tenant_context, tenant_id)
        if ctx is None and getattr(self._owner.config, "storage_pg_enabled", False):
            return
        token = set_current_tenant(ctx) if ctx is not None else None
        try:
            config = self._owner.config
            if ctx is not None and not ctx.is_default():
                from teamEvolver.tenants.registry import effective_config

                config = effective_config(
                    self._tenant_registry(), ctx, self._owner.config
                )
            store = await asyncio.to_thread(
                SessionStore.from_config, config, tenant_id
            )
            session = await asyncio.to_thread(store.load_archived, session_id)
            if not isinstance(session, dict):
                return
            turns = session.get("turns")
            if not isinstance(turns, list) or not turns:
                # Nothing judgeable (metadata-only / pre-archive sessions).
                return

            engine = await asyncio.to_thread(self._owner._get_embedded_evolve_server, tenant_id)
            if engine is None:
                from team_skills.evolution.session_filter import SessionValueClassifier

                # Session analysis is also available before any Skill or
                # evolution runtime has been initialized.
                classifier = await asyncio.to_thread(SessionValueClassifier.from_config, config)
                engine = SimpleNamespace(_llm=classifier.client)

            scores = await self._judge(engine, session)
            judge_payload = self._judge_payload(scores)
            saved = await asyncio.to_thread(
                store.save_session_judge, session_id, judge_payload
            )
            if saved:
                # Drop the cached ledger so the next poll shows the new score
                # immediately instead of waiting for the 15s TTL. We're already
                # inside this tenant's context, so the scoped cache key matches.
                try:
                    from teamEvolver.proxy.routes import _invalidate_dashboard_cache

                    _invalidate_dashboard_cache(
                        f"conversations:{id(self._owner.config)}",
                        f"skill-experiences:{id(self._owner.config)}",
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
        from teamEvolver.proxy.tenant_routes import get_tenant_registry

        return get_tenant_registry(self._owner)

    async def _judge(self, engine: Any, session: dict[str, Any]) -> dict[str, Any]:
        from team_skills.evolution.stages.analyze import (
            analyze_session,
            session_has_merged_outputs,
        )

        async with self._semaphore:
            if not session_has_merged_outputs(session):
                # The standalone summarize+judge stages were merged into
                # ``analyze_session``; historical archived Sessions missing
                # either output must complete it before publishing a review.
                await analyze_session(engine._llm, session)
            return session["_judge_scores"]

    @staticmethod
    def _judge_payload(scores: dict[str, Any]) -> dict[str, Any]:
        from session_ingestion.service import _public_judge_from_scores

        payload = _public_judge_from_scores(scores)
        payload["judge_source"] = "post_ingest_async"
        return payload
