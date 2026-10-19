"""Readiness, Worker startup and graceful process shutdown.

Shutdown revokes readiness, stops claiming work, drains active tasks and closes
shared dependencies. Readiness also reflects Worker health and loop liveness."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .services import RuntimeServices

logger = logging.getLogger(__name__)


class RuntimeLifecycle:
    """Readiness + ordered startup/drain/shutdown for one runtime role."""

    def __init__(
        self,
        services: RuntimeServices,
        *,
        worker_main: Callable[[asyncio.Event], Awaitable] | None = None,
        drain_timeout: float = 30.0,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._services = services
        self._worker_main = worker_main
        self._drain_timeout = float(drain_timeout)
        self._on_close = on_close
        self._ready = False
        self._healthy = True
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None

    @property
    def ready(self) -> bool:
        """Readiness the ``/readyz`` probe reads.

        Revoked first on drain, while the worker loop reports its database
        unavailable, and if the worker loop has ended outside shutdown.
        """
        worker_alive = self._worker_task is None or not self._worker_task.done()
        return self._ready and self._healthy and worker_alive

    def set_healthy(self, healthy: bool) -> None:
        """Worker-loop dependency health (PostgreSQL reachable)."""
        if healthy != self._healthy:
            logger.warning("runtime.health_changed", extra={"role": self._services.role, "healthy": healthy})
        self._healthy = bool(healthy)

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    async def startup(self) -> None:
        """Start the worker loop (worker roles) and flip readiness on."""
        if self._services.runs_worker and self._worker_main is not None:
            self._worker_task = asyncio.create_task(self._worker_main(self._stop_event), name="te-worker")
        self._ready = True
        logger.info("runtime.started", extra={"role": self._services.role})

    async def shutdown(self) -> None:
        """Drain in the fixed order; never raises (shutdown must complete)."""
        # 1. revoke readiness — new requests stop arriving.
        self._ready = False
        # 2. stop claiming — the worker loop exits its next iteration.
        self._stop_event.set()
        # 3. wait for the in-flight task to drain, bounded by drain_timeout.
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=self._drain_timeout)
            except asyncio.TimeoutError:
                logger.warning("runtime.drain_timeout", extra={"role": self._services.role})
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                logger.exception("runtime.worker_task_error")
            self._worker_task = None
        # 4. close external clients and the pool.
        if self._on_close is not None:
            try:
                await self._on_close()
            except Exception:  # noqa: BLE001
                logger.exception("runtime.on_close_error")
        logger.info("runtime.stopped", extra={"role": self._services.role})

    async def __aenter__(self) -> "RuntimeLifecycle":
        await self.startup()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.shutdown()


def build_lifecycle(
    services: RuntimeServices,
    *,
    worker_main: Callable[[asyncio.Event], Awaitable] | None = None,
    drain_timeout: float = 30.0,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> RuntimeLifecycle:
    """Return the lifecycle manager for *services* (ASGI lifespan or worker main)."""
    return RuntimeLifecycle(
        services,
        worker_main=worker_main,
        drain_timeout=drain_timeout,
        on_close=on_close,
    )
