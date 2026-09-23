"""Tenant registry and request-scoped tenant context (multi-tenancy plan Phase 1).

Two operating modes:

- ``postgres``: tenants live in the ``teamevolver.tenants`` table (created by
  the Phase 0 schema bootstrap). Agent tokens (``tevt_...``) hash-lookup to a
  tenant; per-tenant config overrides (``tenants.config`` JSONB, flat
  ``TeamEvolverConfig`` field names) merge over the global config.
- ``single``: storage_pg is not enabled — only the implicit ``default``
  tenant exists and admin mutations are refused, so deployments without PG
  keep their exact single-tenant behavior (zero-change compatibility). A
  single machine credential can be configured (``tenant_machine_token`` /
  ``TEAMEVOLVER_TENANT_TOKEN``) and resolves to that default tenant.

Identity rules (plan §1.2): the tenant is always derived server-side from the
bearer token (or the console operator's explicit switch); a client-supplied
``X-Tenant-Id`` that disagrees with the resolved identity is rejected.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..storage.pg_pool import (
    _DEFAULT_COMMAND_TIMEOUT,
    dsn_from_env,
    get_pg_runtime,
)
from ..storage.pg_store import validate_tenant_id

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "default"
AGENT_TOKEN_PREFIX = "tevt_"
# Prefix of the retired per-Agent access token. Recognised only to answer with
# an actionable 401 during the migration window; see RETIRED note in docs.
RETIRED_AGENT_TOKEN_PREFIX = "tev1_"
_COLS = "tenant_id, display_name, status, config"

# Token lifetime for the registry caches; short enough for rotations/disables
# to take effect quickly, long enough to skip PG on the hot path.
_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class TenantContext:
    """Resolved identity for one request."""

    tenant_id: str = DEFAULT_TENANT_ID
    display_name: str = ""
    status: str = "active"
    # tenants.config JSONB — flat TeamEvolverConfig field-name overrides.
    config_overrides: dict[str, Any] = field(default_factory=dict)

    def is_default(self) -> bool:
        return self.tenant_id == DEFAULT_TENANT_ID


_current_tenant: contextvars.ContextVar[TenantContext | None] = contextvars.ContextVar(
    "te_current_tenant", default=None
)


def get_current_tenant() -> TenantContext | None:
    """Tenant resolved for the running request (None outside request scope)."""
    return _current_tenant.get()


def current_tenant_id() -> str:
    ctx = _current_tenant.get()
    return ctx.tenant_id if ctx is not None else DEFAULT_TENANT_ID


def set_current_tenant(ctx: TenantContext | None):
    """Bind the tenant for the current request task; returns a reset token."""
    return _current_tenant.set(ctx)


def reset_current_tenant(token) -> None:
    _current_tenant.reset(token)


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def tenant_scope_prefix(tenant_id: str, *, is_pg: bool) -> str:
    """Key prefix isolating a non-default tenant in a shared-namespace backend.

    PG isolates rows with RLS (bare keys); local/viking backends share one
    physical key namespace and need ``tenants/<id>/`` on every artifact family.
    The default tenant keeps bare keys for backward compatibility.
    """
    tid = str(tenant_id or "").strip().strip("/")
    if not tid or tid == DEFAULT_TENANT_ID or is_pg:
        return ""
    return f"tenants/{tid}/"


def generate_agent_token() -> str:
    return AGENT_TOKEN_PREFIX + secrets.token_urlsafe(24)


# tenants.config keys consumed by the scheduler/engine pool rather than
# applied as TeamEvolverConfig field overrides (multi-tenancy plan Phase 3
# per-tenant quotas). 0 or absent = unlimited.
QUOTA_MAX_CONCURRENT_SESSIONS = "max_concurrent_sessions"
QUOTA_MAX_EVOLVE_PER_DAY = "max_evolve_per_day"
QUOTA_KEYS = frozenset({QUOTA_MAX_CONCURRENT_SESSIONS, QUOTA_MAX_EVOLVE_PER_DAY})

# Process-wide settings must never be shadowed by a tenant's JSON overrides.
# Outbound Langfuse tracing intentionally sends every tenant to one operator-
# managed project, while inbound Langfuse session sources remain tenant-scoped.
SERVICE_WIDE_CONFIG_KEYS = frozenset(
    {
        "logging",
        "tenant_machine_token",
        "agent_protocol_identity_mode",
        "skills_delivery_mode",
        "sharing_backend",
        "sharing_skill_backend",
        "sharing_session_backend",
        "sharing_local_root",
        "sharing_skill_local_root",
        "replay_adapters_dir",
        "datasource_adapters_dir",
        "langfuse_tracing_enabled",
        "langfuse_tracing_host",
        "langfuse_tracing_public_key",
        "langfuse_tracing_secret_key",
        "langfuse_tracing_environment",
        "langfuse_tracing_release",
        "langfuse_tracing_sample_rate",
        "langfuse_tracing_capture_content",
        "langfuse_tracing_flush_at",
        "langfuse_tracing_flush_interval_seconds",
    }
)


def _quota_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def tenant_quotas(ctx: TenantContext | None) -> dict[str, int]:
    """Per-tenant quotas from ``tenants.config`` (0 = unlimited)."""
    overrides = ctx.config_overrides if ctx is not None else None
    overrides = overrides if isinstance(overrides, dict) else {}
    return {
        QUOTA_MAX_CONCURRENT_SESSIONS: _quota_int(
            overrides.get(QUOTA_MAX_CONCURRENT_SESSIONS)
        ),
        QUOTA_MAX_EVOLVE_PER_DAY: _quota_int(overrides.get(QUOTA_MAX_EVOLVE_PER_DAY)),
    }


def apply_tenant_config_overrides(base: Any, overrides: dict[str, Any] | None) -> Any:
    """Return *base* (TeamEvolverConfig) with the tenant's overrides applied.

    Overrides use flat ``TeamEvolverConfig`` field names; unknown keys are
    ignored with a warning (they may belong to a newer backend version).
    """
    if not overrides:
        return base
    valid = {f.name for f in dataclasses.fields(base)}
    kwargs: dict[str, Any] = {}
    for key, value in overrides.items():
        if key in SERVICE_WIDE_CONFIG_KEYS:
            logger.warning(
                "[Tenants] ignoring service-wide config override %r for tenant scope",
                key,
            )
        elif key in valid:
            kwargs[key] = value
        elif key in QUOTA_KEYS:
            continue  # scheduler-level quota, not a TeamEvolverConfig field
        else:
            logger.warning(
                "[Tenants] ignoring unknown config override %r for tenant scope", key
            )
    if not kwargs:
        return base
    return dataclasses.replace(base, **kwargs)


def effective_config(registry: "TenantRegistry", ctx: TenantContext | None, base: Any) -> Any:
    """Convenience: base config merged with the tenant's overrides."""
    config = base if ctx is None or ctx.is_default() else apply_tenant_config_overrides(base, ctx.config_overrides)
    if bool(getattr(base, "storage_pg_enabled", False)):
        skill_backend = str(
            getattr(base, "sharing_skill_backend", "") or "postgres"
        ).strip()
        config = dataclasses.replace(
            config,
            storage_pg_enabled=True,
            storage_pg_dsn=base.storage_pg_dsn,
            storage_pg_schema=base.storage_pg_schema,
            storage_pg_pool_min=base.storage_pg_pool_min,
            storage_pg_pool_max=base.storage_pg_pool_max,
            storage_pg_ssl=base.storage_pg_ssl,
            sharing_skill_backend=skill_backend,
            sharing_session_backend="postgres",
        )
    return config


class TenantRegistry:
    """Tenant lookup + admin surface. Sync API; PG calls go through PgRuntime."""

    def __init__(
        self,
        config: Any = None,
        *,
        runtime: Any | None = None,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        op_timeout: float = 10.0,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._op_timeout = float(op_timeout)
        self._lock = threading.RLock()
        self._by_id: dict[str, tuple[float, TenantContext | None]] = {}
        self._by_token: dict[str, tuple[float, TenantContext | None]] = {}
        self._runtime = runtime
        if self._runtime is None and config is not None and bool(
            getattr(config, "storage_pg_enabled", False)
        ):
            dsn = str(getattr(config, "storage_pg_dsn", "") or "") or dsn_from_env()
            if not dsn:
                raise ValueError(
                    "storage_pg.enabled requires a DSN or TEAMEVOLVER_PG_HOST"
                )
            if dsn:
                # Control-plane pool: a small dedicated pool so admin queries
                # (/api/tenants, token resolve) are never starved by background
                # work (evolution cycles, session judging) that saturates the
                # main pool.  get_pg_runtime keys by pool params, so a
                # different pool_max yields an independent asyncpg pool.
                control_pool_max = max(
                    1, int(getattr(config, "storage_pg_control_pool_max", 5) or 5)
                )
                self._runtime = get_pg_runtime(
                    dsn=dsn,
                    schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
                    pool_min=1,
                    pool_max=control_pool_max,
                    command_timeout=float(
                        getattr(config, "storage_pg_command_timeout_seconds", _DEFAULT_COMMAND_TIMEOUT) or 30.0
                    ),
                    ssl=str(getattr(config, "storage_pg_ssl", "prefer") or "prefer"),
                )
        self._pg_enabled = self._runtime is not None
        # Single-tenant machine credential: one operator-configured credential
        # unlocking the ``default`` tenant's machine surface. Multi-tenant
        # deployments issue per-tenant credentials instead and leave it empty.
        configured = str(
            getattr(config, "tenant_machine_token", "")
            or os.environ.get("TEAMEVOLVER_TENANT_TOKEN", "")
            or ""
        ).strip()
        if configured and not configured.startswith(AGENT_TOKEN_PREFIX):
            logger.error(
                "[Tenants] tenant_machine_token must start with %r; ignoring the "
                "configured value (it can never be routed to a tenant)",
                AGENT_TOKEN_PREFIX,
            )
            configured = ""
        self._single_token_hash = hash_agent_token(configured) if configured else ""

    # -- mode --------------------------------------------------------------- #

    @property
    def mode(self) -> str:
        return "postgres" if self._pg_enabled else "single"

    @property
    def runtime(self):
        return self._runtime

    def default_context(self) -> TenantContext:
        return TenantContext(tenant_id=DEFAULT_TENANT_ID, display_name="Default")

    @property
    def default_machine_credential_configured(self) -> bool:
        """Whether the single-tenant machine credential is configured (never the secret)."""
        return bool(self._single_token_hash)

    # -- lookup -------------------------------------------------------------- #

    def get(self, tenant_id: str) -> TenantContext | None:
        """Fetch one tenant by id (None when unknown/disabled)."""
        try:
            tid = validate_tenant_id(tenant_id)
        except ValueError:
            return None
        if not self._pg_enabled:
            return self.default_context() if tid == DEFAULT_TENANT_ID else None
        cached = self._cache_get(self._by_id, tid)
        if cached is not None or tid in self._by_id:
            return cached
        row = self._run(self._fetch_tenant(tid))
        ctx = self._row_to_ctx(row)
        with self._lock:
            self._by_id[tid] = (time.time() + self._ttl, ctx)
        return ctx

    def resolve_by_agent_token(self, token: str) -> TenantContext | None:
        """Map a ``tevt_`` machine credential to its tenant (cached, incl. negatives)."""
        token = str(token or "").strip()
        if not token.startswith(AGENT_TOKEN_PREFIX):
            return None
        if not self._pg_enabled:
            # Single-tenant: the one operator-configured credential unlocks the
            # default tenant. Not cached — it is a hash compare, and rotation is
            # a config edit plus a restart.
            if not self._single_token_hash or not hmac.compare_digest(
                hash_agent_token(token), self._single_token_hash
            ):
                return None
            return self.default_context()
        token_hash = hash_agent_token(token)
        cached = self._cache_get(self._by_token, token_hash)
        if cached is not None or token_hash in self._by_token:
            return cached
        row = self._run(self._fetch_by_token(token_hash))
        ctx = self._row_to_ctx(row)
        with self._lock:
            self._by_token[token_hash] = (time.time() + self._ttl, ctx)
            if ctx is not None:
                self._by_id[ctx.tenant_id] = (time.time() + self._ttl, ctx)
        return ctx

    def list_tenants(self) -> list[TenantContext]:
        if not self._pg_enabled:
            return [self.default_context()]
        rows = self._run(self._fetch_all())
        ctxs = [self._row_to_ctx(row) for row in rows]
        ctxs = [ctx for ctx in ctxs if ctx is not None]
        with self._lock:
            now = time.time() + self._ttl
            for ctx in ctxs:
                self._by_id[ctx.tenant_id] = (now, ctx)
        return ctxs

    # -- admin mutations ------------------------------------------------------ #

    def create_tenant(self, display_name: str, tenant_id: str = "") -> tuple[TenantContext, str]:
        """Create a tenant + agent token. Returns (context, plaintext token)."""
        self._require_pg()
        display_name = str(display_name or "").strip() or "Untitled"
        tid = validate_tenant_id(tenant_id) if tenant_id else "t_" + secrets.token_hex(8)
        if tid == DEFAULT_TENANT_ID:
            raise ValueError("default is reserved")
        # Prevent duplicate display_name — the primary cause of accidental
        # double-creates (double-click, refresh-resubmit) when account_id is
        # left empty (each attempt generates a fresh random t_ id).
        with self._lock:
            existing = self._run(self._fetch_by_display_name(display_name))
            if existing is not None:
                raise ValueError(
                    f"display_name already in use: {display_name!r} "
                    f"(tenant_id={existing['tenant_id']})"
                )
            token = generate_agent_token()
            row = self._run(self._insert_tenant(tid, display_name, hash_agent_token(token)))
            if row is None:
                raise ValueError("tenant account already exists")
            ctx = self._row_to_ctx(row)
            self._invalidate()
        return ctx, token

    def rotate_token(self, tenant_id: str) -> str | None:
        """Issue a new agent token; the previous one stops working at once."""
        self._require_pg()
        tid = validate_tenant_id(tenant_id)
        if tid == DEFAULT_TENANT_ID:
            # Mirrors create_tenant: a credential on the default tenant would
            # make ``tenant_source == "token"`` reachable in a multi-tenant
            # deployment and widen the machine-path surface.
            raise ValueError("default is reserved")
        token = generate_agent_token()
        row = self._run(self._update_token(tid, hash_agent_token(token)))
        if row is None:
            return None
        self._invalidate()
        return token

    def set_status(self, tenant_id: str, status: str) -> bool:
        self._require_pg()
        tid = validate_tenant_id(tenant_id)
        status = str(status or "").strip().lower()
        if status not in {"active", "disabled"}:
            raise ValueError(f"invalid tenant status: {status!r}")
        row = self._run(self._update_status(tid, status))
        if row is None:
            return False
        self._invalidate()
        return True

    def update_tenant_config(self, tenant_id: str, overrides: dict[str, Any]) -> TenantContext | None:
        """Merge flat config overrides into tenants.config (same-key values win).

        Returns the updated context, or None if the tenant does not exist.
        """
        self._require_pg()
        tid = validate_tenant_id(tenant_id)
        if not isinstance(overrides, dict) or not overrides:
            raise ValueError("config overrides must be a non-empty dict")
        row = self._run(self._update_config(tid, overrides))
        if row is None:
            return None
        self._invalidate()
        return self._row_to_ctx(row)

    # -- internals ------------------------------------------------------------ #

    def _require_pg(self) -> None:
        if not self._pg_enabled:
            raise RuntimeError(
                "tenant management requires storage_pg to be enabled (single-tenant mode)"
            )

    def _run(self, coro):
        return self._runtime.run(coro, timeout=self._op_timeout)

    @staticmethod
    def _row_to_ctx(row: Any) -> TenantContext | None:
        if row is None:
            return None
        config = row["config"] if "config" in row.keys() else None
        if isinstance(config, str):
            import json

            try:
                config = json.loads(config)
            except (ValueError, TypeError):
                config = {}
        return TenantContext(
            tenant_id=str(row["tenant_id"]),
            display_name=str(row["display_name"] or ""),
            status=str(row["status"] or "active"),
            config_overrides=config if isinstance(config, dict) else {},
        )

    def _cache_get(self, cache: dict, key: str) -> TenantContext | None:
        with self._lock:
            if len(cache) >= 4096:
                cache.clear()
            entry = cache.get(key)
        if entry is None:
            return None  # type: ignore[unreachable]  # distinct from cached None below
        expires, ctx = entry
        if expires < time.time():
            with self._lock:
                cache.pop(key, None)
            return None
        return ctx

    def _invalidate(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_token.clear()

    # -- SQL (schema from PgRuntime; qualified names keep search_path free) ---- #

    @property
    def _schema(self) -> str:
        return getattr(self._runtime, "schema", "teamevolver")

    async def _fetch_tenant(self, tid: str):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"SELECT {_COLS} FROM {schema}.tenants WHERE tenant_id = $1", tid
            )

    async def _fetch_by_token(self, token_hash: str):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"SELECT {_COLS} FROM {schema}.tenants "
                "WHERE agent_token_hash = $1 AND status = 'active'",
                token_hash,
            )

    async def _fetch_all(self):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetch(
                f"SELECT {_COLS} FROM {schema}.tenants ORDER BY created_at, tenant_id"
            )

    async def _fetch_by_display_name(self, display_name: str):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"SELECT {_COLS} FROM {schema}.tenants "
                "WHERE display_name = $1 AND status = 'active' LIMIT 1",
                display_name,
            )

    async def _insert_tenant(self, tid: str, display_name: str, token_hash: str):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"INSERT INTO {schema}.tenants (tenant_id, display_name, agent_token_hash) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (tenant_id) DO NOTHING "
                "RETURNING " + _COLS,
                tid,
                str(display_name or "").strip() or "Untitled",
                token_hash,
            )

    async def _update_token(self, tid: str, token_hash: str):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"UPDATE {schema}.tenants SET agent_token_hash = $2 "
                "WHERE tenant_id = $1 AND status = 'active' "
                "RETURNING " + _COLS,
                tid,
                token_hash,
            )

    async def _update_status(self, tid: str, status: str):
        schema = self._schema
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"UPDATE {schema}.tenants SET status = $2 "
                "WHERE tenant_id = $1 RETURNING " + _COLS,
                tid,
                status,
            )

    async def _update_config(self, tid: str, overrides: dict[str, Any]):
        schema = self._schema
        removed = [key for key, value in overrides.items() if value is None]
        updates = {key: value for key, value in overrides.items() if value is not None}
        async with self._runtime.tenant_conn(DEFAULT_TENANT_ID) as conn:
            return await conn.fetchrow(
                f"UPDATE {schema}.tenants SET config = (config - $2::text[]) || $3::jsonb "
                "WHERE tenant_id = $1 RETURNING " + _COLS,
                tid,
                removed,
                json.dumps(updates),
            )
