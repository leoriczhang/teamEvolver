"""Per-tenant evolution-engine pool (multi-tenancy plan Phase 2).

One :class:`~teamEvolver.evolve.runtime.orchestrator.EvolveServer` instance per
tenant (tenant ≡ OpenViking account), built lazily from the request-resolved
tenant context and kept in an LRU with a bounded number of resident engines.
Engine state lives in the storage backend keyed by ``pg_tenant_id`` (PostgreSQL
RLS) or per-tenant OpenViking credentials, so evicting an idle engine loses no
state — it is rebuilt on demand.

In ``single`` mode (storage_pg disabled) the pool serves exactly one implicit
``default`` tenant with the pre-multi-tenancy behavior.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Callable

from ..tenants.registry import (
    DEFAULT_TENANT_ID,
    QUOTA_MAX_CONCURRENT_SESSIONS,
    effective_config,
    tenant_quotas,
)

logger = logging.getLogger(__name__)

# LRU bound from the plan ("LRU 上限约 32 个常驻引擎").
DEFAULT_MAX_ENGINES = 32
# Tenant-list cache for the scheduler: avoids a PG list_tenants per tick
# (registry lookups are cached separately; listings are not).
_TENANT_LIST_TTL_SECONDS = 30.0


class EnginePool:
    """Lazy, LRU-bounded registry of per-tenant EvolveServer engines."""

    def __init__(
        self,
        config: Any,
        config_builder: Callable[[Any], Any],
        *,
        registry_provider: Callable[[], Any] | None = None,
        max_engines: int = DEFAULT_MAX_ENGINES,
    ) -> None:
        self._config = config
        self._config_builder = config_builder
        self._registry_provider = registry_provider
        self._max = max(1, int(max_engines))
        self._lock = threading.RLock()
        self._build_locks = [threading.Lock() for _ in range(64)]
        # tenant_id -> EvolveServer, LRU order (oldest first).
        self._engines: OrderedDict[str, Any] = OrderedDict()
        self._build_failed: set[str] = set()
        self._retry_after: dict[str, float] = {}
        self._tenant_ids_cache: tuple[float, list[str] | None] = (0.0, None)

    # -- tenant resolution --------------------------------------------------- #

    def registry(self) -> Any | None:
        if self._registry_provider is None:
            return None
        try:
            return self._registry_provider()
        except Exception:  # noqa: BLE001 - resolution must not break the pool
            logger.warning("[EnginePool] tenant registry provider failed", exc_info=True)
            return None

    def tenant_ids(self) -> list[str]:
        """Active tenant ids known to the registry (cached briefly).

        ``single`` mode yields exactly the implicit default tenant.
        """
        now = time.monotonic()
        with self._lock:
            ts, cached = self._tenant_ids_cache
            if cached is not None and now - ts < _TENANT_LIST_TTL_SECONDS:
                return list(cached)
        registry = self.registry()
        if registry is None or registry.mode != "postgres":
            ids = [DEFAULT_TENANT_ID]
        else:
            try:
                ids = [
                    ctx.tenant_id
                    for ctx in registry.list_tenants()
                    if ctx.status == "active"
                ]
            except Exception:  # noqa: BLE001 - listing failure must not stop cycles
                logger.warning(
                    "[EnginePool] tenant listing failed; scheduling default only",
                    exc_info=True,
                )
                ids = [DEFAULT_TENANT_ID]
            if not ids:
                ids = [DEFAULT_TENANT_ID]
        with self._lock:
            self._tenant_ids_cache = (now, list(ids))
        return list(ids)

    def build_evolve_config(self, tenant_id: str) -> Any:
        """Per-tenant ``EvolveServerConfig``: registry overrides + tenant scope."""
        base = self._config
        registry = self.registry()
        ctx = registry.get(tenant_id) if registry is not None else None
        if registry is not None and registry.mode == "postgres" and (ctx is None or ctx.status != "active"):
            raise ValueError("unknown or disabled tenant")
        effective = effective_config(registry, ctx, base)
        config = self._config_builder(effective)
        # Quota: max_concurrent_sessions caps the engine's parallel session
        # groups so one tenant cannot monopolize the shared LLM pool.
        cap = tenant_quotas(ctx)[QUOTA_MAX_CONCURRENT_SESSIONS]
        if cap > 0:
            current = max(1, int(getattr(config, "max_parallel_groups", 1) or 1))
            if current > cap:
                config = replace(config, max_parallel_groups=cap)
        if str(getattr(config, "pg_tenant_id", "") or "") != tenant_id:
            config = replace(config, pg_tenant_id=tenant_id)
            config.__post_init__()
        return config

    # -- engine lifecycle ----------------------------------------------------- #

    def get(self, tenant_id: str) -> Any | None:
        # Lock striping bounds synchronization memory and serializes cold builds
        # for the same account without holding the global pool lock during I/O.
        with self._build_locks[hash(tenant_id) % len(self._build_locks)]:
            return self._get_or_build(tenant_id)

    def _get_or_build(self, tenant_id: str) -> Any | None:
        """Return the tenant's engine, building it on first use (None on failure)."""
        from ..storage.pg_store import validate_tenant_id

        try:
            tid = validate_tenant_id(tenant_id)
        except ValueError:
            logger.warning("[EnginePool] invalid tenant id %r", tenant_id)
            return None
        with self._lock:
            engine = self._engines.get(tid)
            if engine is not None:
                self._engines.move_to_end(tid)
                return engine
            if tid in self._build_failed and time.monotonic() < self._retry_after.get(tid, 0):
                return None
            if len(self._engines) >= self._max:
                victim = next(
                    ((key, value) for key, value in self._engines.items() if not self._engine_busy(value)),
                    None,
                )
                if victim is None:
                    return None
                key, value = victim
                self._engines.pop(key)
                value.stop()
        try:
            from ..evolve import EvolveServer

            engine = EvolveServer(self.build_evolve_config(tid))
        except Exception:
            with self._lock:
                self._build_failed.add(tid)
                self._retry_after[tid] = time.monotonic() + 30.0
            logger.warning(
                "[EnginePool] engine build failed for tenant %s; retry after 30s",
                tid,
                exc_info=True,
            )
            return None
        with self._lock:
            self._build_failed.discard(tid)
            self._retry_after.pop(tid, None)
            self._engines[tid] = engine
            self._evict_locked()
        logger.info("[EnginePool] engine ready for tenant %s (%d resident)", tid, len(self._engines))
        return engine

    def build_failed(self, tenant_id: str) -> bool:
        with self._lock:
            return tenant_id in self._build_failed

    def drop(self, tenant_id: str, *, reason: str = "") -> None:
        """Drop one engine so the next use rebuilds it with fresh config.

        Used after a tenant-config update: a resident engine keeps its built
        config until evicted. A busy engine is only dropped from the pool (its
        running cycle finishes against the old config; the PG advisory cycle
        lock prevents a new-engine cycle from overlapping it). Also clears any
        cached build failure so a previously broken tenant retries immediately.
        """
        with self._lock:
            engine = self._engines.pop(tenant_id, None)
            self._build_failed.discard(tenant_id)
            self._retry_after.pop(tenant_id, None)
        if engine is None:
            return
        if not self._engine_busy(engine):
            try:
                engine.stop()
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            "[EnginePool] dropped engine for tenant %s%s",
            tenant_id,
            f" ({reason})" if reason else "",
        )

    @staticmethod
    def _engine_busy(engine: Any) -> bool:
        """An engine with an active cycle or pending replay evals is not evictable."""
        run_lock = getattr(engine, "_run_lock", None)
        if run_lock is not None and run_lock.locked():
            return True
        if getattr(engine, "_eval_tasks", None):
            return True
        if getattr(engine, "_eval_jobs", None):
            return True
        return False

    def _evict_locked(self) -> None:
        while len(self._engines) > self._max:
            evicted = False
            for tid, engine in list(self._engines.items()):
                if len(self._engines) <= self._max:
                    break
                if self._engine_busy(engine):
                    continue
                self._engines.pop(tid, None)
                evicted = True
                try:
                    engine.stop()
                except Exception:  # noqa: BLE001
                    pass
                logger.info("[EnginePool] evicted idle engine for tenant %s", tid)
            if not evicted:
                # Every resident engine is busy — allow a temporary overflow
                # rather than disrupting a running cycle.
                return

    def engines(self) -> list[Any]:
        with self._lock:
            return list(self._engines.values())

    def peek_config(self, tenant_id: str) -> Any | None:
        """Config of the resident engine if built, else a freshly built one.

        Config construction is cheap (no engine, no storage); used for
        hot-reload change detection.
        """
        with self._lock:
            engine = self._engines.get(tenant_id)
        if engine is not None:
            return engine.config
        try:
            return self.build_evolve_config(tenant_id)
        except Exception:  # noqa: BLE001
            return None

    def reset(self) -> None:
        """Drop every engine (after graceful stop) — used on config reload."""
        with self._lock:
            self._engines.clear()
            self._build_failed.clear()
            self._retry_after.clear()
            self._tenant_ids_cache = (0.0, None)
