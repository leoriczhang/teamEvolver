"""PostgreSQL connection pool for the platform database layer.

A thin, explicit replacement for the legacy ``storage.pg_pool`` runtime:

- asyncpg pools are bound to the event loop that created them, so the pool keeps
 **one lazily-created asyncpg pool per running event loop**. Async callers
 (HTTP handlers on the server loop, worker handlers on the worker loop) use the
 ``tenant_conn`` / ``admin_conn`` contexts directly on their own loop.
- One dedicated IO thread owns one more asyncio loop for synchronous callers,
 which submit coroutines via :meth:`DatabasePool.run`.
- No auto-DDL. The pool never creates schemas or tables; that is the sole job of
 ``teamEvolver schema apply`` (:mod:`teamEvolver.database.schema_runner`).
- The runtime role is verified non-SUPERUSER / non-BYPASSRLS on first connect
 , so RLS can never be silently bypassed.
- ``tenant_conn`` pins ``search_path`` + ``app.tenant_id`` on every check-out
 (:mod:`teamEvolver.database.rls`); ``admin_conn`` opens a context without a
 tenant GUC for platform-level (non-tenant) tables.

This module is platform infrastructure and imports no business module.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

from .rls import apply_connection_context, reset_tenant_guc

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA = "teamevolver"
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_DEFAULT_POOL_MIN = 2
_DEFAULT_POOL_MAX = 20
_DEFAULT_COMMAND_TIMEOUT = 30.0


def build_pg_dsn(
    *,
    host: str,
    port: int | str = 5432,
    database: str,
    username: str = "",
    password: str = "",
) -> str:
    """Assemble a ``postgresql://`` DSN, percent-encoding user and password."""
    user_part = quote(str(username or ""), safe="")
    pwd_part = quote(str(password or ""), safe="")
    auth = f"{user_part}:{pwd_part}@" if (user_part or pwd_part) else ""
    return f"postgresql://{auth}{host}:{int(port)}/{database}"


def dsn_from_env() -> str:
    """Derive the DSN from the ``TEAMEVOLVER_PG_*`` environment variables."""
    import os

    host = str(os.environ.get("TEAMEVOLVER_PG_HOST") or "").strip()
    if not host:
        return ""
    return build_pg_dsn(
        host=host,
        port=os.environ.get("TEAMEVOLVER_PG_PORT") or 5432,
        database=os.environ.get("TEAMEVOLVER_PG_DATABASE") or "teamevolver",
        username=os.environ.get("TEAMEVOLVER_PG_USERNAME") or "teamevolver",
        password=os.environ.get("TEAMEVOLVER_PG_PASSWORD") or "",
    )


def validate_schema_name(schema: str) -> str:
    """Reject anything that could break out of a qualified SQL identifier."""
    value = str(schema or "").strip().lower()
    if not _SCHEMA_RE.match(value):
        raise ValueError(f"invalid PostgreSQL schema name: {schema!r}")
    return value


class DatabasePool:
    """One IO thread + event loop + lazy asyncpg pool per connection key."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = DEFAULT_SCHEMA,
        pool_min: int = _DEFAULT_POOL_MIN,
        pool_max: int = _DEFAULT_POOL_MAX,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        ssl: str = "prefer",
    ) -> None:
        self._dsn = dsn
        self._schema = validate_schema_name(schema)
        self._pool_min = max(1, int(pool_min))
        self._pool_max = max(self._pool_min, int(pool_max))
        self._command_timeout = float(command_timeout)
        self._ssl = ssl or "prefer"
        # One asyncpg pool (and its init lock) per event loop that uses it.
        self._pools: dict[asyncio.AbstractEventLoop, Any] = {}
        self._init_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._registry_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._loop_main, name="te-db-io", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("DatabasePool: IO thread failed to start")

    # -- thread plumbing -------------------------------------------------- #

    def _loop_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    @property
    def schema(self) -> str:
        return self._schema

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Execute *coro* on the IO loop and block the caller for the result."""
        if self._closed or self._loop is None:
            coro.close()
            raise RuntimeError("DatabasePool is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    # -- pool bootstrap --------------------------------------------------- #

    def _prune_closed_loops(self) -> None:
        """Forget pools whose event loop has closed (their sockets are gone)."""
        with self._registry_lock:
            dead = [loop for loop in self._pools if loop.is_closed()]
            for loop in dead:
                pool = self._pools.pop(loop)
                self._init_locks.pop(loop, None)
                try:
                    pool.terminate()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            for loop in [loop for loop in self._init_locks if loop.is_closed()]:
                self._init_locks.pop(loop, None)

    async def _ensure_pool(self) -> Any:
        """Return the asyncpg pool bound to the *running* event loop."""
        if self._closed:
            raise RuntimeError("DatabasePool is closed")
        loop = asyncio.get_running_loop()
        pool = self._pools.get(loop)
        if pool is not None:
            return pool
        self._prune_closed_loops()
        with self._registry_lock:
            lock = self._init_locks.setdefault(loop, asyncio.Lock())
        async with lock:
            pool = self._pools.get(loop)
            if pool is None:
                import asyncpg

                pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._pool_min,
                    max_size=self._pool_max,
                    timeout=self._command_timeout,
                    command_timeout=self._command_timeout,
                    server_settings={"search_path": self._schema},
                    ssl=self._ssl,
                )
                try:
                    async with pool.acquire(timeout=self._command_timeout) as conn:
                        await self._verify_role(conn)
                except BaseException:
                    pool.terminate()
                    raise
                with self._registry_lock:
                    self._pools[loop] = pool
        return pool

    @staticmethod
    async def _verify_role(conn: Any) -> None:
        role = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        if role is not None and (role["rolsuper"] or role["rolbypassrls"]):
            raise ValueError("PostgreSQL runtime role must not be SUPERUSER or BYPASSRLS")

    # -- connection contexts ---------------------------------------------- #

    @asynccontextmanager
    async def tenant_conn(self, tenant_id: str) -> AsyncIterator[Any]:
        """Check out a connection scoped to *tenant_id* (RLS enforced)."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._command_timeout) as conn:
            await apply_connection_context(conn, schema=self._schema, tenant_id=tenant_id)
            try:
                yield conn
            finally:
                await reset_tenant_guc(conn)

    @asynccontextmanager
    async def admin_conn(self) -> AsyncIterator[Any]:
        """Check out a connection without a tenant GUC (platform tables only).

        Used by the schema runner and platform-level reads/writes to tables that
        are not tenant-scoped (``schema_versions``, ``teamevolver_config``).
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._command_timeout) as conn:
            await conn.execute("SELECT set_config('search_path', $1, false)", self._schema)
            yield conn

    # -- health ----------------------------------------------------------- #

    async def _probe(self) -> tuple[int, int, float]:
        pool = await self._ensure_pool()
        start = time.monotonic()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        ping_ms = round((time.monotonic() - start) * 1000.0, 1)
        return pool.get_size(), pool.get_idle_size(), ping_ms

    def pool_status(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Health + pool metrics; never raises (status endpoints stay up)."""
        status: dict[str, Any] = {
            "schema": self._schema,
            "pool_min": self._pool_min,
            "pool_max": self._pool_max,
            "reachable": False,
        }
        if self._closed:
            status["reason"] = "pool_closed"
            return status
        try:
            size, idle, ping_ms = self.run(self._probe(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - status must never raise
            status["reason"] = type(exc).__name__
            return status
        status.update({"reachable": True, "pool_size": size, "pool_idle": idle, "ping_ms": ping_ms})
        return status

    async def _close(self) -> None:
        """Close the IO-loop pool gracefully (runs on the IO loop)."""
        loop = asyncio.get_running_loop()
        with self._registry_lock:
            pool = self._pools.pop(loop, None)
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=5.0)
            except asyncio.TimeoutError:
                pool.terminate()

    async def aclose(self) -> None:
        """Close the pool bound to the *calling* event loop gracefully."""
        loop = asyncio.get_running_loop()
        with self._registry_lock:
            pool = self._pools.pop(loop, None)
            self._init_locks.pop(loop, None)
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=5.0)
            except asyncio.TimeoutError:
                pool.terminate()

    def close(self, timeout: float = 10.0) -> None:
        """Close every per-loop pool and stop the IO thread.

        The IO-loop pool closes gracefully; pools owned by other loops are
        terminated (call :meth:`aclose` on those loops first for a graceful
        drain — the role lifecycle does).
        """
        if self._closed:
            return
        try:
            self.run(self._close(), timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        self._closed = True
        with self._registry_lock:
            remaining = list(self._pools.values())
            self._pools.clear()
            self._init_locks.clear()
        for pool in remaining:
            try:
                pool.terminate()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)


_POOL_LOCK = threading.Lock()
_POOLS: dict[tuple, DatabasePool] = {}


def get_database_pool(
    *,
    dsn: str,
    schema: str = DEFAULT_SCHEMA,
    pool_min: int = _DEFAULT_POOL_MIN,
    pool_max: int = _DEFAULT_POOL_MAX,
    command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
    ssl: str = "prefer",
) -> DatabasePool:
    """Process-wide pool registry keyed by (dsn, schema, sizes, timeout, ssl)."""
    schema = validate_schema_name(schema)
    key = (dsn, schema, int(pool_min), int(pool_max), float(command_timeout), ssl)
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None or pool._closed:
            pool = DatabasePool(
                dsn=dsn,
                schema=schema,
                pool_min=pool_min,
                pool_max=pool_max,
                command_timeout=command_timeout,
                ssl=ssl,
            )
            _POOLS[key] = pool
        return pool


def close_database_pools() -> None:
    """Tear down every pool in this process (tests / shutdown hook)."""
    with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()
