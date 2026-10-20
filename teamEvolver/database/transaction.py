"""Unified transaction boundary for the platform database layer.

Repositories receive a connection that is already tenant-scoped (RLS GUC set);
the :class:`TransactionManager` wraps the pool's connection contexts so callers
express intent (``tenant`` vs ``admin``) and get a single transaction per block.

The manager is deliberately small: it does not hide the connection, so a
repository can issue multiple statements inside one atomic block.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .pool import DatabasePool


class TransactionManager:
    """Open atomic, tenant-scoped (or admin) transactions over a pool."""

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    @property
    def pool(self) -> DatabasePool:
        return self._pool

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: str) -> AsyncIterator[Any]:
        """Atomic block on a connection scoped to *tenant_id* (RLS enforced)."""
        async with self._pool.tenant_conn(tenant_id) as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def admin_tx(self) -> AsyncIterator[Any]:
        """Atomic block on a platform (non-tenant) connection."""
        async with self._pool.admin_conn() as conn:
            async with conn.transaction():
                yield conn

    def run_tenant_tx(self, tenant_id: str, body, *, timeout: float | None = None) -> Any:
        """Synchronously run ``await body(conn)`` inside a tenant transaction."""

        async def _runner() -> Any:
            async with self.tenant_tx(tenant_id) as conn:
                return await body(conn)

        return self._pool.run(_runner(), timeout=timeout)

    def run_admin_tx(self, body, *, timeout: float | None = None) -> Any:
        """Synchronously run ``await body(conn)`` inside an admin transaction."""

        async def _runner() -> Any:
            async with self.admin_tx() as conn:
                return await body(conn)

        return self._pool.run(_runner(), timeout=timeout)
