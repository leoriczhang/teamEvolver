"""Run the API, Worker or local combined role.

The application root assembles domain dependencies and routers. API serves the
console and business HTTP surface without running handlers. Worker runs the task
loop and a separate health server. The combined role is for local development."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Any

from ..database import get_database_pool
from ..database.pool import dsn_from_env
from ..database.transaction import TransactionManager
from .http_runtime import build_health_app
from .lifecycle import build_lifecycle
from .services import VALID_ROLES
from .worker import run_worker

logger = logging.getLogger(__name__)

DEFAULT_API_PORT = 52010
DEFAULT_WORKER_HEALTH_PORT = 52011


def _worker_owner_id() -> str:
    """A stable-per-process worker identity for lease/claim ownership."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _resolve_pg(config: Any) -> tuple[str, str, str]:
    dsn = str(getattr(config, "storage_pg_dsn", "") or "").strip() or dsn_from_env()
    schema = str(getattr(config, "storage_pg_schema", "") or "teamevolver").strip()
    ssl = str(getattr(config, "storage_pg_ssl", "") or "prefer").strip()
    return dsn, schema, ssl




async def _serve_uvicorn(app, *, host: str, port: int, lifecycle) -> None:
    """Run uvicorn with the runtime lifecycle driving startup/graceful drain."""
    import uvicorn

    @asynccontextmanager
    async def lifespan(_app):
        await lifecycle.startup()
        try:
            yield
        finally:
            await lifecycle.shutdown()
    app.router.lifespan_context = lifespan
    config = uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    await server.serve()


def run_role(
    *,
    role: str,
    config: Any,
    host: str | None = None,
    api_port: int | None = None,
    worker_health_port: int | None = None,
    handlers: dict | None = None,
) -> None:
    """Build and run the given *role* until the process is signalled to stop.

    ``api`` and ``all`` must only be combined locally; a production deployment
    runs a single ``api`` or ``worker`` role.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")

    dsn, schema, ssl = _resolve_pg(config)
    if not dsn:
        raise RuntimeError(
            "the role runtime requires PostgreSQL (set storage_pg_dsn or TEAMEVOLVER_PG_* env)"
        )
    pool = get_database_pool(dsn=dsn, schema=schema, ssl=ssl)
    tx = TransactionManager(pool)

    from ..app import build_role_services
    services = build_role_services(role=role, tx=tx, config=config, handlers=handlers)

    host = host or str(getattr(config, "proxy_host", "0.0.0.0") or "0.0.0.0")
    api_port = api_port or int(getattr(config, "proxy_port", DEFAULT_API_PORT) or DEFAULT_API_PORT)
    worker_health_port = worker_health_port or DEFAULT_WORKER_HEALTH_PORT

    owner_id = _worker_owner_id()

    async def _active_tenants():
        async with pool.admin_conn() as conn:
            return tuple(r["tenant_id"] for r in await conn.fetch(
                f"SELECT tenant_id FROM {schema}.tenants WHERE status='active' ORDER BY tenant_id"))

    lifecycle_ref: list = []

    def _on_health(healthy: bool) -> None:
        lifecycle_ref[0].set_healthy(healthy)

    async def _worker_main(stop_event: asyncio.Event):
        tasks = [asyncio.create_task(run_worker(
            services, owner_id=owner_id, stop_event=stop_event, tenant_source=_active_tenants,
            on_health=_on_health))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _on_close() -> None:
        # Gracefully drain the pool bound to the serving loop, then stop the
        # pool's own IO thread (any remaining per-loop pools are terminated).
        await pool.aclose()
        pool.close()

    if role == "worker":
        lifecycle = build_lifecycle(services, worker_main=_worker_main, on_close=_on_close)
        lifecycle_ref.append(lifecycle)
        app = build_health_app(lifecycle, role=role)
        asyncio.run(_serve_uvicorn(app, host=host, port=worker_health_port, lifecycle=lifecycle))
        return

    # The application root must assemble successfully before serving.
    worker_main = _worker_main if role == "all" else None
    lifecycle = build_lifecycle(services, worker_main=worker_main, on_close=_on_close)
    lifecycle_ref.append(lifecycle)
    from ..app import create_app
    app = create_app(services=services, lifecycle=lifecycle)
    asyncio.run(_serve_uvicorn(app, host=host, port=api_port, lifecycle=lifecycle))
