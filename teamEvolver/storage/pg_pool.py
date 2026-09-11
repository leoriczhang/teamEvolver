"""PostgreSQL connection runtime for TeamEvolver's local-state storage.

Implements the multi-tenancy plan (``docs/multi-tenancy-plan.md`` §2.4):

- One dedicated IO thread owns one asyncio loop and one lazily-created
  ``asyncpg`` pool per ``(dsn, schema)`` — sync storage callers (the whole
  object-store contract is synchronous) submit coroutines via
  :meth:`PgRuntime.run` instead of bridging per-thread event loops, which
  avoids asyncpg's cross-loop connection pitfalls entirely.
- Auto-DDL on first use: schema, ``tenants`` + ``objects`` tables, and a
  fail-closed RLS policy on ``objects`` (``app.tenant_id`` session variable
  must match the row, or the row is invisible/unwritable).
- Per-acquire context: ``set_config('search_path', ...)`` and
  ``set_config('app.tenant_id', ...)`` are re-applied on every connection
  check-out, so pool resets can never leak one tenant's session context into
  another's queries.

The DSN defaults to the same PostgreSQL instance OpenViking uses
(``OV_PG_*`` environment variables, see ``inc-aiagent-core-openviking``
``scripts/local/start-local.sh``). Passwords containing reserved characters
(e.g. ``#``) are percent-encoded per the OpenViking PostgreSQL guide §4.1.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_DEFAULT_SCHEMA = "teamevolver"
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
    """Assemble a ``postgresql://`` DSN, percent-encoding user and password.

    A password containing ``#`` / ``@`` / ``:`` truncates the DSN unless
    encoded (real case: ``Example#password`` — see the OpenViking PG guide §4.1).
    """
    user_part = quote(str(username or ""), safe="")
    pwd_part = quote(str(password or ""), safe="")
    auth = f"{user_part}:{pwd_part}@" if (user_part or pwd_part) else ""
    return f"postgresql://{auth}{host}:{int(port)}/{database}"


def dsn_from_env() -> str:
    """Derive the DSN from the ``OV_PG_*`` variables shared with OpenViking.

    Returns an empty string when ``OV_PG_HOST`` is unset, letting callers
    fall back to explicit configuration. Mirrors the defaults of
    ``inc-aiagent-core-openviking/scripts/local/start-local.sh``.
    """
    import os

    host = str(os.environ.get("OV_PG_HOST") or "").strip()
    if not host:
        return ""
    return build_pg_dsn(
        host=host,
        port=os.environ.get("OV_PG_PORT") or 5432,
        database=os.environ.get("OV_PG_DATABASE") or "openviking",
        username=os.environ.get("OV_PG_USERNAME") or "openviking",
        password=os.environ.get("OV_PG_PASSWORD") or "",
    )


def validate_schema_name(schema: str) -> str:
    """Reject anything that could break out of a qualified SQL identifier."""
    value = str(schema or "").strip().lower()
    if not _SCHEMA_RE.match(value):
        raise ValueError(f"invalid PostgreSQL schema name: {schema!r}")
    return value


# --------------------------------------------------------------------------- #
# DDL bootstrap                                                                #
# --------------------------------------------------------------------------- #

_TENANTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.tenants (
    tenant_id        TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    agent_token_hash TEXT NOT NULL DEFAULT '',
    viking_endpoint  TEXT NOT NULL DEFAULT '',
    viking_api_key_enc TEXT NOT NULL DEFAULT '',
    config           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_OBJECTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.objects (
    tenant_id  TEXT NOT NULL REFERENCES {schema}.tenants (tenant_id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    content    BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key)
)
"""

_SESSION_INDEX_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.session_index (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    index_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    meta JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, index_key, session_id)
)
"""

# text_pattern_ops keeps prefix scans (starts_with) on the btree index.
_OBJECTS_KEY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS objects_prefix_idx
    ON {schema}.objects (tenant_id, key text_pattern_ops)
"""

_RLS_SQL = (
    "ALTER TABLE {schema}.objects ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE {schema}.objects FORCE ROW LEVEL SECURITY",
)

# Fail closed: an unset/empty app.tenant_id matches no rows at all.
_RLS_POLICY_SQL = """
DO $ddl$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = '{schema}' AND tablename = 'objects'
          AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON {schema}.objects
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$ddl$
"""

_DEFAULT_TENANT_SQL = """
INSERT INTO {schema}.tenants (tenant_id, display_name)
VALUES ('default', 'Default (single-tenant compatibility)')
ON CONFLICT (tenant_id) DO NOTHING
"""


# --------------------------------------------------------------------------- #
# Advisory lock keys (cross-replica cycle mutex, plan §2.3)                    #
# --------------------------------------------------------------------------- #

def _advisory_key(schema: str, tenant_id: str) -> int:
    """Deterministic signed-int64 advisory lock key for ``(schema, tenant)``.

    md5-derived (documented functions only, unlike the internal ``hashtext``);
    the schema prefix keeps keys distinct across schemas on the same database.
    """
    digest = hashlib.md5(f"teamEvolver:{schema}:{tenant_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def init_pg_schema(conn: Any, schema: str = _DEFAULT_SCHEMA) -> None:
    """Create the schema, tables, RLS policy, and the default tenant.

    Idempotent — safe to run on every pool bootstrap. ``conn`` is any object
    exposing ``execute(sql)`` (asyncpg connection or test fake).
    """
    schema = validate_schema_name(schema)
    statements = [
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        _TENANTS_TABLE_SQL.format(schema=schema),
        _OBJECTS_TABLE_SQL.format(schema=schema),
        _SESSION_INDEX_SQL.format(schema=schema),
        f"CREATE INDEX IF NOT EXISTS session_index_recent ON {schema}.session_index "
        "(tenant_id, index_key, updated_at DESC)",
        _OBJECTS_KEY_INDEX_SQL.format(schema=schema),
        *(sql.format(schema=schema) for sql in _RLS_SQL),
        _RLS_POLICY_SQL.format(schema=schema),
        *(sql.format(schema=schema).replace(".objects", ".session_index") for sql in _RLS_SQL),
        _RLS_POLICY_SQL.format(schema=schema).replace(".objects", ".session_index").replace(
            "tablename = 'objects'", "tablename = 'session_index'"
        ),
        _DEFAULT_TENANT_SQL.format(schema=schema),
    ]
    for statement in statements:
        await conn.execute(statement)


# --------------------------------------------------------------------------- #
# Dedicated IO-thread runtime                                                  #
# --------------------------------------------------------------------------- #


class PgRuntime:
    """One IO thread + event loop + lazy asyncpg pool per (dsn, schema)."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = _DEFAULT_SCHEMA,
        pool_min: int = _DEFAULT_POOL_MIN,
        pool_max: int = _DEFAULT_POOL_MAX,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self._dsn = dsn
        self._schema = validate_schema_name(schema)
        self._pool_min = max(1, int(pool_min))
        self._pool_max = max(self._pool_min, int(pool_max))
        self._command_timeout = float(command_timeout)
        self._pool: Any | None = None
        self._init_lock: asyncio.Lock | None = None
        # Tenant rows lazily ensured on first use (objects.tenant_id carries a
        # FK to tenants); ON CONFLICT makes this race-safe across replicas.
        self._ensured_tenants: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._closed = False
        # Cross-replica cycle mutexes (pg_try_advisory_lock), held connections
        # keyed by tenant_id — see ``try_advisory_lock``/``release_advisory_lock``.
        self._advisory_conns: dict[str, Any] = {}
        self._advisory_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._loop_main, name="te-pg-io", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("PgRuntime: IO thread failed to start")

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
            raise RuntimeError("PgRuntime is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    # -- pool bootstrap ---------------------------------------------------- #

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._pool is None:
                try:
                    import asyncpg
                except ImportError as exc:  # pragma: no cover - env guard
                    raise ImportError(
                        "PostgreSQL storage requires asyncpg: "
                        "pip install 'teamEvolver[pg]'"
                    ) from exc
                pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._pool_min,
                    max_size=self._pool_max,
                    timeout=self._command_timeout,
                    command_timeout=self._command_timeout,
                    server_settings={"search_path": self._schema},
                )
                try:
                    async with pool.acquire(timeout=self._command_timeout) as conn:
                        role = await conn.fetchrow(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                        )
                        if role["rolsuper"] or role["rolbypassrls"]:
                            raise ValueError("PostgreSQL runtime role must not be SUPERUSER or BYPASSRLS")
                        async with conn.transaction():
                            await conn.execute(
                                "SELECT pg_advisory_xact_lock($1)",
                                _advisory_key(self._schema, "_schema_init"),
                            )
                            await init_pg_schema(conn, self._schema)
                except BaseException:
                    pool.terminate()
                    raise
                self._pool = pool
        return self._pool

    @asynccontextmanager
    async def tenant_conn(self, tenant_id: str) -> AsyncIterator[Any]:
        """Check out a connection scoped to *tenant_id*.

        Re-applies ``search_path`` and ``app.tenant_id`` on every check-out:
        pool resets may clear session SETs, and the RLS policy fails closed
        when ``app.tenant_id`` is empty or mismatched. The tenant registry row
        is ensured once per process before the connection is used (the
        ``objects`` table carries a FK to ``tenants``).
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._command_timeout) as conn:
            await conn.execute(
                "SELECT set_config('search_path', $1, false)", self._schema
            )
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, false)", str(tenant_id)
            )
            if tenant_id not in self._ensured_tenants:
                await conn.execute(
                    f"INSERT INTO {self._schema}.tenants (tenant_id) VALUES ($1) "
                    "ON CONFLICT (tenant_id) DO NOTHING",
                    str(tenant_id),
                )
                self._ensured_tenants.add(tenant_id)
            yield conn

    async def _close(self) -> None:
        if self._pool is not None:
            with self._advisory_lock:
                conns = list(self._advisory_conns.items())
                self._advisory_conns.clear()
            for tid, conn in conns:
                if conn is not None:
                    await self._release_advisory(conn, tid)
            try:
                await asyncio.wait_for(self._pool.close(), timeout=5.0)
            except asyncio.TimeoutError:
                self._pool.terminate()
            self._pool = None

    # -- cross-replica cycle mutex ------------------------------------------ #

    async def _acquire_advisory(self, tenant_id: str) -> Any | None:
        """Try ``pg_try_advisory_lock`` on a dedicated connection (IO loop).

        Returns the held connection, or ``None`` when another replica holds
        the lock (non-blocking, per the multi-tenancy plan §2.3).
        """
        pool = await self._ensure_pool()
        conn = await pool.acquire(timeout=self._command_timeout)
        try:
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", _advisory_key(self._schema, tenant_id)
            )
        except BaseException:
            await pool.release(conn)
            raise
        if not got:
            await pool.release(conn)
            return None
        return conn

    async def _release_advisory(self, conn: Any, tenant_id: str) -> None:
        try:
            await conn.fetchval(
                "SELECT pg_advisory_unlock($1)", _advisory_key(self._schema, tenant_id)
            )
        finally:
            if self._pool is not None:
                await self._pool.release(conn)

    def try_advisory_lock(self, tenant_id: str, *, timeout: float = 10.0) -> bool:
        """Take the tenant's cross-replica evolution-cycle lock (non-blocking).

        The advisory connection is held until :meth:`release_advisory_lock`.
        Returns ``False`` when the lock is held by another replica — the
        caller must skip that tenant's cycle.
        """
        if self._closed:
            return False
        with self._advisory_lock:
            if tenant_id in self._advisory_conns:
                return False
            self._advisory_conns[tenant_id] = None
        try:
            conn = self.run(self._acquire_advisory(tenant_id), timeout=timeout)
            with self._advisory_lock:
                if conn is None:
                    self._advisory_conns.pop(tenant_id, None)
                else:
                    self._advisory_conns[tenant_id] = conn
            return conn is not None
        except BaseException:
            with self._advisory_lock:
                self._advisory_conns.pop(tenant_id, None)
            raise

    def release_advisory_lock(self, tenant_id: str, *, timeout: float = 10.0) -> None:
        """Release the tenant's cycle lock (no-op when not held here)."""
        with self._advisory_lock:
            conn = self._advisory_conns.pop(str(tenant_id), None)
        if conn is None:
            return
        try:
            self.run(self._release_advisory(conn, tenant_id), timeout=timeout)
        except Exception:  # noqa: BLE001 - unlock failure must not break shutdown
            logger.warning(
                "[PgRuntime] advisory unlock failed for tenant %s", tenant_id,
                exc_info=True,
            )

    # -- health / metrics --------------------------------------------------- #

    async def _pool_probe(self) -> tuple[int, int, float]:
        pool = await self._ensure_pool()
        start = time.monotonic()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        ping_ms = round((time.monotonic() - start) * 1000.0, 1)
        return pool.get_size(), pool.get_idle_size(), ping_ms

    def pool_status(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Health + connection-pool metrics for ``/storage/status``.

        Never raises: an unreachable PG reports ``reachable: False`` with the
        error in ``reason`` so the status endpoint stays up during an outage.
        """
        status: dict[str, Any] = {
            "schema": self._schema,
            "pool_min": self._pool_min,
            "pool_max": self._pool_max,
            "reachable": False,
        }
        if self._closed:
            status["reason"] = "runtime_closed"
            return status
        try:
            size, idle, ping_ms = self.run(self._pool_probe(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - status must never raise
            status["reason"] = type(exc).__name__
            return status
        status.update(
            {"reachable": True, "pool_size": size, "pool_idle": idle, "ping_ms": ping_ms}
        )
        return status

    def close(self, timeout: float = 10.0) -> None:
        """Close the pool and stop the IO thread (used by tests/shutdown)."""
        if self._closed:
            return
        try:
            self.run(self._close(), timeout=timeout)
        except Exception:
            pass
        self._closed = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)


_RUNTIME_LOCK = threading.Lock()
_RUNTIMES: dict[tuple[str, str, int, int, float], "PgRuntime"] = {}


def get_pg_runtime(
    *,
    dsn: str,
    schema: str = _DEFAULT_SCHEMA,
    pool_min: int = _DEFAULT_POOL_MIN,
    pool_max: int = _DEFAULT_POOL_MAX,
    command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
) -> PgRuntime:
    """Process-wide runtime registry keyed by (dsn, schema, pool sizes)."""
    schema = validate_schema_name(schema)
    key = (dsn, schema, int(pool_min), int(pool_max), float(command_timeout))
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None or runtime._closed:
            runtime = PgRuntime(
                dsn=dsn,
                schema=schema,
                pool_min=pool_min,
                pool_max=pool_max,
                command_timeout=command_timeout,
            )
            _RUNTIMES[key] = runtime
        return runtime


def close_pg_runtimes() -> None:
    """Tear down every runtime in this process (tests / shutdown hook)."""
    with _RUNTIME_LOCK:
        runtimes = list(_RUNTIMES.values())
        _RUNTIMES.clear()
    for runtime in runtimes:
        runtime.close()
