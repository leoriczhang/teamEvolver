"""Proxy server composition and lifecycle.

``ProxyServer`` composes the FastAPI route and skill synchronization mixins
into the teamEvolver service. It owns threading lifecycle (uvicorn in a
background thread) and idle/validation accessors.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from starlette.responses import Response

from team_memory.debug_routes import MemoryDebugMixin
from team_memory.maintenance.runtime import TeamMemorySupervisor as DreamCycleSupervisor
from team_memory.routes import AggregationMixin
from team_miner.bridge import SkillMinerBridgeMixin
from team_skills.library.manager import SkillManager

from ..config import TeamEvolverConfig
from ..observability import configure_langfuse, flush_langfuse
from ..tenants.registry import (
    DEFAULT_TENANT_ID,
    QUOTA_MAX_EVOLVE_PER_DAY,
    TenantRegistry,
    current_tenant_id,
    tenant_quotas,
)
from .agent_context import AgentContextMixin
from .docs import DocsMixin
from .knowledge_mining import KnowledgeMiningMixin
from .openviking_workspace import OpenVikingWorkspaceMixin
from .platform_assets import PlatformAssetsMixin
from .routes import RoutesMixin
from .skill_lab import SkillLabMixin
from .skills_admin import SkillsAdminMixin
from .tenant_routes import get_tenant_registry
from .uploads import UploadsMixin
from .users_admin import UsersAdminMixin, sync_openviking_user

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_RESET = "\033[0m"


class ProxyServer(
    RoutesMixin,
    SkillMinerBridgeMixin,
    SkillLabMixin,
    SkillsAdminMixin,
    UploadsMixin,
    UsersAdminMixin,
    PlatformAssetsMixin,
    OpenVikingWorkspaceMixin,
    KnowledgeMiningMixin,
    MemoryDebugMixin,
    AgentContextMixin,
    AggregationMixin,
    DocsMixin,
):
    """teamEvolver service: console, skill sync, user management, and validation.

    Parameters
    ----------
    config:
        TeamEvolverConfig instance.
    skill_manager:
        Optional SkillManager for injecting skills into system prompts.
    """

    def __init__(
        self,
        config: TeamEvolverConfig,
        sampling_client=None,
        skill_manager: Optional[SkillManager] = None,
        last_request_tracker=None,
    ):
        from ..replay_adapter import ensure_replay_host

        ensure_replay_host()
        self.config = config
        self._sampling_client = sampling_client
        self.skill_manager = skill_manager
        self._last_request_tracker = last_request_tracker
        self._last_request_at = time.time()

        self._background_tasks: set[asyncio.Task] = set()
        self._skill_reload_task: Optional[asyncio.Task] = None
        self._shutdown_drain_timeout_seconds = 15
        self._skill_reload_interval_seconds = max(
            5,
            int(getattr(config, "sharing_skill_reload_interval_seconds", 30) or 30),
        )
        # Default-tenant engine slot — kept for hot-reload change detection and
        # test doubles; the authoritative per-tenant engines live in the pool.
        self._embedded_evolve_server = None
        # Per-tenant engines (multi-tenancy plan Phase 2): LRU pool + per-tenant
        # HTTP apps + per-tenant cycle pacing for the global scheduler.
        self._engine_pool = None
        self._embedded_evolve_apps: dict[str, Any] = {}
        self._embedded_evolve_task: Optional[asyncio.Task] = None
        self._tenant_cycle_tasks: dict[str, asyncio.Task] = {}
        self._evolve_next_due: dict[str, float] = {}
        # Per-tenant daily cycle counters (local day) for the max_evolve_per_day
        # quota; in-memory per replica, so the cap is approximate across
        # replicas — sufficient to stop single-tenant starvation (plan Phase 3).
        self._evolve_daily: dict[str, tuple[str, int]] = {}
        configure_langfuse(config)
        # Team-owned memories/resources use the canonical OpenViking ``team``
        # user. Ensure it exists on every deployment; the sync helper is
        # idempotent and fail-open when OpenViking is unavailable.
        sync_openviking_user(config, "team")
        self._dreamcycle = DreamCycleSupervisor(config, self._aggregation_service())
        # Durable skill-mirror outbox: delivers the local skill library subtree
        # to OpenViking with retry/backoff so remote Agents keep reading team
        # skills even while OpenViking is flaky. Never on the evolution path.
        self._mirror_flusher = None
        self._start_skill_mirror_flusher()

        # Compatibility/backfill queue for historical archived Sessions that
        # predate mandatory ingest-time Session Analyze. New Sessions already
        # carry their complete review before storage.
        from team_skills.evolution.session_judge_queue import AsyncSessionJudgeQueue

        self._session_judge_queue = AsyncSessionJudgeQueue(self)

        self.app = self._build_app()

        # Threading lifecycle (set by start())
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._server_stopped_event = threading.Event()

        # Persist daily evolve quotas across process restarts (plan Phase 3).
        self._load_evolve_daily()

    # ------------------------------------------------------------------ #
    # Idle / validation accessors                                          #
    # ------------------------------------------------------------------ #

    def _mark_request_activity(self) -> None:
        self._last_request_at = time.time()
        if self._last_request_tracker is not None:
            try:
                self._last_request_tracker.touch()
            except Exception:
                pass

    def last_request_age_seconds(self) -> Optional[float]:
        last = getattr(self, "_last_request_at", None)
        if last is None:
            return None
        return max(0.0, time.time() - float(last))

    def active_session_count(self) -> int:
        return 0

    def is_idle_for_validation(self, idle_after_seconds: int) -> bool:
        age = self.last_request_age_seconds()
        if age is None:
            return False
        if self.active_session_count() > 0:
            return False
        return age >= max(0, int(idle_after_seconds))

    def _start_skill_mirror_flusher(self) -> None:
        try:
            from team_skills.library.mirror import MirrorFlusher, VikingSkillMirror

            mirror = VikingSkillMirror.from_config(self.config)
            if mirror is None:
                return
            self._mirror_flusher = MirrorFlusher(mirror)
            self._mirror_flusher.start()
        except Exception:  # noqa: BLE001 - mirroring must never block startup
            logger.debug("[SkillMirror] flusher start skipped", exc_info=True)

    async def _shutdown_cleanup(self) -> None:
        dataset_runner = getattr(self, "_dataset_batch_runner", None)
        if dataset_runner is not None:
            dataset_runner.stop()
        if self._skill_reload_task is not None:
            self._skill_reload_task.cancel()
            await asyncio.gather(self._skill_reload_task, return_exceptions=True)
            self._skill_reload_task = None
        knowledge_watchers = list(getattr(self, "_knowledge_compile_watchers", {}).values())
        for task in knowledge_watchers:
            task.cancel()
        if knowledge_watchers:
            await asyncio.gather(*knowledge_watchers, return_exceptions=True)
        if self._mirror_flusher is not None:
            self._mirror_flusher.stop()
            self._mirror_flusher = None
        datasource_runtime = getattr(self, "_datasource_pull_runtime", None)
        if datasource_runtime is not None:
            try:
                await datasource_runtime.stop()
            except Exception:  # noqa: BLE001 - shutdown must proceed
                logger.debug("[DatasourceSchedule] shutdown failed", exc_info=True)
        self._dreamcycle.stop()
        judge_queue = getattr(self, "_session_judge_queue", None)
        if judge_queue is not None:
            try:
                await asyncio.wait_for(judge_queue.stop(), timeout=5.0)
            except Exception:  # noqa: BLE001 - shutdown must proceed
                logger.debug("[SessionJudge] queue stop failed", exc_info=True)
        await self._stop_embedded_evolve()
        try:
            self._stop_skillminer()
        except Exception:
            logger.debug("[SkillMiner] shutdown stop failed", exc_info=True)
        await self._await_background_tasks(self._shutdown_drain_timeout_seconds)
        try:
            await self.aclose_knowledge_http()
        except Exception:  # noqa: BLE001 - shutdown must proceed
            logger.debug("[Knowledge] HTTP pool close failed", exc_info=True)
        await asyncio.to_thread(flush_langfuse)

    def _configure_langfuse(self, config: TeamEvolverConfig) -> dict:
        """Hot-reload outbound tracing without restarting model runtimes."""
        self.config = config
        return configure_langfuse(config)

    def _start_dreamcycle(self) -> None:
        try:
            self._dreamcycle.start()
        except Exception:
            logger.warning("[DreamCycle] startup failed", exc_info=True)

    def _trigger_dreamcycle(self) -> dict:
        return self._dreamcycle.trigger()

    def _dreamcycle_status(self) -> dict:
        return self._dreamcycle.status()

    def _dreamcycle_dry_run(self) -> dict:
        return self._dreamcycle.dry_run()

    def _dreamcycle_memory_changes(
        self,
        *,
        limit: int = 100,
        config: Any = None,
    ) -> dict:
        return self._dreamcycle.memory_changes(limit=limit, config=config)

    def _run_dreamcycle_memory_replay(
        self,
        *,
        change_id: str,
        query: str,
        checklist: list,
        source_session_id: str = "",
        max_interactions: int = 4,
        timeout_seconds: int = 600,
        config: Any = None,
    ) -> dict:
        return self._dreamcycle.run_memory_replay(
            change_id=change_id,
            query=query,
            checklist=checklist,
            source_session_id=source_session_id,
            max_interactions=max_interactions,
            timeout_seconds=timeout_seconds,
            config=config,
        )

    def _run_dreamcycle_memory_replay_adhoc(
        self,
        *,
        memory_path: str,
        before_content: str,
        after_content: str,
        query: str,
        checklist: list,
        scope: str = "team_memory",
        source_session_id: str = "",
        max_interactions: int = 4,
        timeout_seconds: int = 600,
        config: Any = None,
    ) -> dict:
        return self._dreamcycle.run_memory_replay_adhoc(
            memory_path=memory_path,
            before_content=before_content,
            after_content=after_content,
            query=query,
            checklist=checklist,
            scope=scope,
            source_session_id=source_session_id,
            max_interactions=max_interactions,
            timeout_seconds=timeout_seconds,
            config=config,
        )

    def _dreamcycle_memory_replays(
        self,
        *,
        change_id: str,
        limit: int = 100,
        config: Any = None,
    ) -> dict:
        return self._dreamcycle.memory_replays(
            change_id=change_id,
            limit=limit,
            config=config,
        )

    def _dreamcycle_reset(
        self,
        *,
        remote: bool = False,
        dry_run: bool = True,
    ) -> dict:
        return self._dreamcycle.reset(remote=remote, dry_run=dry_run)

    async def _reload_openviking_integrations(
        self,
        config: TeamEvolverConfig,
    ) -> None:
        """Apply a credential update without restarting the service process."""
        current_evolve_config = self._embedded_evolve_config_snapshot()
        next_evolve_config = self._build_embedded_evolve_config(config)
        evolve_config_changed = current_evolve_config != next_evolve_config

        await asyncio.to_thread(self._dreamcycle.stop)
        self.config = config
        configure_langfuse(config)
        self._dreamcycle = DreamCycleSupervisor(config, self._aggregation_service())
        # Tenant registry binds to storage_pg settings — rebuild on reload so
        # enabling/disabling PG or changing its DSN takes effect at once.
        self._tenant_registry = TenantRegistry(config)

        if evolve_config_changed:
            await self._stop_embedded_evolve(graceful=True)
            self._embedded_evolve_server = None
            self._embedded_evolve_apps = {}
            self._engine_pool = None
            self._evolve_next_due = {}
            self._load_evolve_daily()
            self._start_embedded_evolve()
        else:
            logger.info(
                "[EvolveServer] OpenViking sync did not change evolve config; "
                "keeping the active cycle"
            )
        # DreamCycle is retired; a credential reload no longer restarts it.
        # Team memory is maintained via the ov compile aggregation service.

    # ------------------------------------------------------------------ #
    # Embedded evolve server                                               #
    # ------------------------------------------------------------------ #

    def _embedded_evolve_enabled(self) -> bool:
        if importlib.util.find_spec("team_skills.evolution.runtime") is None:
            return False
        raw = os.environ.get("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _build_embedded_evolve_config(
        self,
        config: TeamEvolverConfig,
    ):
        from team_skills.evolution import EvolveServerConfig

        evolve_config = EvolveServerConfig.from_teamEvolver_config(config)
        evolve_config.http_port = int(getattr(config, "proxy_port", 52010) or 52010)
        interval = os.environ.get("TEAMEVOLVER_EMBEDDED_EVOLVE_INTERVAL_S", "").strip()
        if interval:
            evolve_config.interval_seconds = max(1, int(interval))
        evolve_config.__post_init__()
        return evolve_config

    def _get_engine_pool(self):
        """Per-tenant engine pool (multi-tenancy plan Phase 2)."""
        if not self._embedded_evolve_enabled():
            return None
        if self._engine_pool is None:
            from .engine_pool import EnginePool

            self._engine_pool = EnginePool(
                self.config,
                self._build_embedded_evolve_config,
                registry_provider=lambda: get_tenant_registry(self),
            )
        return self._engine_pool

    def _embedded_evolve_config_snapshot(self):
        """Currently active evolve config for hot-reload change detection.

        Reads the resident default-tenant engine (or a test-injected stand-in)
        without constructing anything; falls back to a cheap config-only build
        from the pool.
        """
        config = getattr(self._embedded_evolve_server, "config", None)
        if config is not None:
            return config
        pool = self._engine_pool
        if pool is not None:
            return pool.peek_config(DEFAULT_TENANT_ID)
        return None

    def _get_embedded_evolve_server(self, tenant_id: Optional[str] = None):
        """Engine for *tenant_id* (request tenant context by default)."""
        pool = self._get_engine_pool()
        if pool is None:
            return None
        tid = str(tenant_id or current_tenant_id() or DEFAULT_TENANT_ID)
        engine = pool.get(tid)
        if tid == DEFAULT_TENANT_ID:
            # Keep the legacy slot in sync for reload detection and tests.
            self._embedded_evolve_server = engine
        return engine

    def _get_embedded_evolve_app(self, tenant_id: Optional[str] = None):
        server = self._get_embedded_evolve_server(tenant_id)
        if server is None:
            return None
        tid = str(getattr(server, "tenant_id", "") or DEFAULT_TENANT_ID)
        resident_engines = getattr(self._get_engine_pool(), "engines", None)
        if callable(resident_engines):
            resident = {engine.tenant_id for engine in resident_engines()}
            for cached_id in list(self._embedded_evolve_apps):
                if cached_id not in resident:
                    self._embedded_evolve_apps.pop(cached_id, None)
        app = self._embedded_evolve_apps.get(tid)
        if app is None or (hasattr(app, "state") and getattr(app.state, "engine", None) is not server):
            app = server.create_http_app()
            if hasattr(app, "state"):
                app.state.engine = server
            self._embedded_evolve_apps[tid] = app
        return app

    def _start_embedded_evolve(self) -> None:
        if self._get_engine_pool() is None:
            return
        if self._embedded_evolve_task is not None and not self._embedded_evolve_task.done():
            return
        self._embedded_evolve_task = asyncio.create_task(self._run_multi_tenant_evolve())
        logger.info("[EvolveServer] multi-tenant scheduler started")

    async def _run_multi_tenant_evolve(self) -> None:
        """Global evolution scheduler (plan §3 Phase 2.3).

        One loop discovers all tenants, while each due tenant owns an
        independent task, engine, Session queue, and LLM dispatcher. An
        operator may set ``TEAMEVOLVER_TENANT_CONCURRENCY`` to add a
        process-wide safety cap; the default 0 leaves tenant APIs independent.
        Cross-replica advisory locks still prevent two replicas from evolving
        the same tenant simultaneously.
        """
        tick = self._evolve_tick_seconds()
        limit = self._tenant_cycle_limit()
        tasks = self._tenant_cycle_tasks
        logger.info(
            "[EvolveServer] multi-tenant scheduler: tick=%ss active_tenant_cap=%s",
            tick,
            limit or "unlimited",
        )

        async def run_tenant(pool, tid):
            from ..tenants.registry import reset_current_tenant, set_current_tenant

            context_token = None
            try:
                registry = pool.registry()
                ctx = await asyncio.to_thread(registry.get, tid) if registry is not None else None
                if ctx is not None:
                    if ctx.status != "active":
                        return
                    context_token = set_current_tenant(ctx)
                engine = await asyncio.to_thread(pool.get, tid)
                if engine is None:
                    self._evolve_next_due[tid] = time.monotonic() + 30
                    return
                drained = await self._run_tenant_cycle(pool, tid, engine)
                if drained > 0:
                    await asyncio.to_thread(self._evolve_note_cycle, tid)
                cap = int(getattr(engine.config, "drain_max_per_cycle", 0) or 0)
                backlog = drained >= cap if cap > 0 else drained > 0
                self._evolve_next_due[tid] = time.monotonic() + (
                    1 if backlog else max(1, int(engine.config.interval_seconds))
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._evolve_next_due[tid] = time.monotonic() + 30
                logger.exception("[EvolveServer] tenant %s cycle failed", tid)
            finally:
                if context_token is not None:
                    reset_current_tenant(context_token)

        try:
            while True:
                for tid, task in list(tasks.items()):
                    if task.done():
                        tasks.pop(tid)
                try:
                    pool = self._get_engine_pool()
                    if pool is not None:
                        for tid in await asyncio.to_thread(self._quota_ordered_tenants, pool):
                            if limit > 0 and len(tasks) >= limit:
                                break
                            if tid not in tasks and time.monotonic() >= self._evolve_next_due.get(tid, 0):
                                tasks[tid] = asyncio.create_task(run_tenant(pool, tid))
                except Exception:
                    logger.exception("[EvolveServer] scheduler pass failed")
                await asyncio.sleep(tick)
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            tasks.clear()

    @staticmethod
    def _evolve_tick_seconds() -> float:
        raw = os.environ.get("TEAMEVOLVER_EVOLVE_TICK_S", "2").strip()
        try:
            return max(0.5, float(raw))
        except ValueError:
            return 2.0

    @staticmethod
    def _tenant_cycle_limit() -> int:
        raw = os.environ.get("TEAMEVOLVER_TENANT_CONCURRENCY", "0").strip()
        try:
            return max(0, min(256, int(raw or 0)))
        except ValueError:
            return 0

    # -- per-tenant quota scheduling (plan Phase 3) --------------------------- #

    @staticmethod
    def _evolve_day_key() -> str:
        return time.strftime("%Y-%m-%d")

    def _evolve_daily_file(self) -> Path:
        return Path.home() / ".teamEvolver" / "evolve_daily_quota.json"

    def _load_evolve_daily(self) -> None:
        """Populate ``self._evolve_daily`` from the on-disk quota file.

        File format: ``{"day": "2024-01-01", "counts": {"tenant_id": 5}}``.
        Stale data (stored day != today) is discarded so a fresh day starts
        every tenant at zero.
        """
        self._evolve_daily = {}
        path = self._evolve_daily_file()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("day") != self._evolve_day_key():
            return
        counts = data.get("counts") or {}
        self._evolve_daily = {
            tid: (self._evolve_day_key(), int(count))
            for tid, count in counts.items()
        }

    def _save_evolve_daily(self) -> None:
        """Atomically persist ``self._evolve_daily`` to the quota file."""
        path = self._evolve_daily_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "[EvolveServer] failed to create evolve quota dir %s",
                path.parent,
                exc_info=True,
            )
            return
        payload = {
            "day": self._evolve_day_key(),
            "counts": {
                tid: count for tid, (_day, count) in self._evolve_daily.items()
            },
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            logger.warning(
                "[EvolveServer] failed to persist evolve quota file %s",
                path,
                exc_info=True,
            )

    def _evolve_cycle_count_today(self, tenant_id: str) -> int:
        day, count = self._evolve_daily.get(tenant_id, ("", 0))
        return count if day == self._evolve_day_key() else 0

    def _evolve_note_cycle(self, tenant_id: str) -> None:
        self._evolve_daily[tenant_id] = (
            self._evolve_day_key(),
            self._evolve_cycle_count_today(tenant_id) + 1,
        )
        self._save_evolve_daily()

    @staticmethod
    def _next_midnight_monotonic() -> float:
        now = time.time()
        lt = time.localtime(now)
        midnight = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)
        )
        return time.monotonic() + max(0.0, midnight - now)

    def _quota_ordered_tenants(self, pool) -> list[str]:
        """Due-order tenants by quota-weighted deficit (plan Phase 3 quotas).

        Weighted round-robin: tenants are served in ascending
        ``cycles_today / weight`` order (weight = max_evolve_per_day, or 1 when
        unlimited), so a tenant with a larger quota earns proportionally more
        cycles per pass. Tenants that exhausted their daily cap are deferred to
        the next local midnight. Counters are per-replica in-memory — the cap
        is approximate across replicas, which is enough to prevent one tenant
        from starving the rest.
        """
        registry = pool.registry()
        entries: list[tuple[float, str]] = []
        for tid in pool.tenant_ids():
            ctx = None
            if registry is not None:
                try:
                    ctx = registry.get(tid)
                except Exception:  # noqa: BLE001 - quota lookup must not stop cycles
                    logger.warning(
                        "[EvolveServer] quota lookup failed for tenant %s", tid,
                        exc_info=True,
                    )
            daily_cap = tenant_quotas(ctx)[QUOTA_MAX_EVOLVE_PER_DAY]
            count = self._evolve_cycle_count_today(tid)
            if daily_cap > 0 and count >= daily_cap:
                self._evolve_next_due[tid] = self._next_midnight_monotonic()
                logger.info(
                    "[EvolveServer] tenant %s reached max_evolve_per_day=%d; "
                    "deferred to midnight",
                    tid,
                    daily_cap,
                )
                continue
            weight = float(daily_cap if daily_cap > 0 else 1)
            entries.append((count / weight, tid))
        entries.sort()
        return [tid for _, tid in entries]

    async def _run_tenant_cycle(self, pool, tenant_id: str, engine) -> int:
        """One evolution cycle for one tenant; returns the drained session count."""
        registry = pool.registry()
        runtime = getattr(registry, "runtime", None) if registry is not None else None
        locked = False
        if runtime is not None and not getattr(engine, "owns_cycle_lock", False):
            # Cross-replica mutex (plan §2.3): skip when another replica is
            # already running this tenant's cycle.
            locked = await asyncio.to_thread(runtime.try_advisory_lock, tenant_id)
            if not locked:
                logger.debug(
                    "[EvolveServer] tenant %s cycle lock held elsewhere; skipping",
                    tenant_id,
                )
                return 0
        try:
            # Share the cycle lock with /trigger so a manual trigger and the
            # scheduler never run overlapping read-modify-write cycles.
            async with engine._get_run_lock():
                logger.info("[EvolveServer] tenant %s cycle start", tenant_id)
                result = await engine.run_once()
            return int(result.get("sessions") or 0)
        finally:
            if locked:
                await asyncio.to_thread(runtime.release_advisory_lock, tenant_id)

    async def _stop_embedded_evolve(self, *, graceful: bool = False) -> None:
        engines = []
        pool = self._engine_pool
        if pool is not None:
            engines.extend(pool.engines())
        legacy = self._embedded_evolve_server
        if legacy is not None and all(legacy is not engine for engine in engines):
            engines.append(legacy)
        if graceful:
            timeout = max(
                1.0,
                float(
                    os.environ.get(
                        "TEAMEVOLVER_EVOLVE_RELOAD_GRACE_S",
                        "900",
                    )
                ),
            )
            for engine in engines:
                run_lock = getattr(engine, "_run_lock", None)
                if run_lock is None or not run_lock.locked():
                    continue
                try:
                    await asyncio.wait_for(run_lock.acquire(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[EvolveServer] active cycle did not finish within %.1fs; "
                        "forcing config reload",
                        timeout,
                    )
                else:
                    run_lock.release()
        for engine in engines:
            try:
                engine.stop()
            except Exception:
                logger.debug("[EvolveServer] embedded stop failed", exc_info=True)
        task = self._embedded_evolve_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._embedded_evolve_task = None

    async def _dispatch_embedded_evolve_request(self, request) -> Optional[Response]:
        tenant_id = getattr(request.state, "tenant_id", None) or current_tenant_id()
        app = await asyncio.to_thread(self._get_embedded_evolve_app, tenant_id)
        if app is None:
            return None

        import httpx

        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in request.scope.get("headers", [])
            if key.lower() not in {b"host", b"content-length", b"connection"}
        }
        body = await request.body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://teamEvolver-embedded-evolve") as client:
            upstream = await client.request(request.method, target, content=body, headers=headers)
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in {"content-length", "connection", "transfer-encoding"}
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready_event.clear()
        self._server_stopped_event.clear()
        cfg = uvicorn.Config(
            self.app,
            host=self.config.proxy_host,
            port=self.config.proxy_port,
            log_level=None,
            log_config=None,
            access_log=False,
            limit_concurrency=max(16, int(os.environ.get("TEAMEVOLVER_HTTP_CONCURRENCY", "256"))),
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        threading.Thread(target=self._print_ready_banner, daemon=True).start()

    def _run_server(self):
        try:
            self._server.run()
        finally:
            self._server_stopped_event.set()
            self._ready_event.clear()

    def _print_ready_banner(self):
        if not self._ready_event.wait(timeout=30):
            return
        if self._server_stopped_event.is_set():
            return
        from ..logging_runtime import event, logging_status
        status = logging_status()
        event(logger, "service.ready", log_path=status.get("path"), log_state=status["file_state"])
        banner = (
            f"\n{'=' * 70}\n"
            f"  teamEvolver service ready\n"
            f"  http://{self.config.proxy_host}:{self.config.proxy_port}\n"
            f"{'=' * 70}\n"
        )
        logger.info(f"{_GREEN}{banner}{_RESET}")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._ready_event.clear()
        self._server_stopped_event.set()

    def wait_until_ready(self, timeout_s: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while not self._server_stopped_event.is_set():
            if self._ready_event.is_set():
                server = getattr(self, "_server", None)
                if server is None or server.started:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._server_stopped_event.wait(min(0.05, remaining))
        return False

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    def _safe_create_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._background_tasks.discard(t)
            self._task_done_cb(t)

        task.add_done_callback(_on_done)
        return task

    @staticmethod
    def _task_done_cb(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("[Proxy] background task failed: %s", exc, exc_info=exc)
