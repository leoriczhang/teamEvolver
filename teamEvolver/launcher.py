"""teamEvolver service launcher."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Optional

from .config_store import ConfigStore
from . import runtime_state

logger = logging.getLogger(__name__)


class Launcher:
    """Start/stop teamEvolver services based on ConfigStore."""

    def __init__(self, config_store: ConfigStore):
        self.cs = config_store
        self._api_server = None
        self._stop_event = threading.Event()
        self._validation_worker = None
        self._validation_task = None
        self._skill_sync_task = None
        self._skillopt_rollout_task = None

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def start(self):
        cfg = self.cs.to_config()
        from .logging_runtime import register_secrets
        register_secrets(cfg)
        logger.info("[Launcher] Starting teamEvolver …")
        self._write_pid()
        self._setup_signal_handlers()
        try:
            await self._run(cfg)
        finally:
            self.stop()

    def stop(self):
        self._stop_event.set()
        if self._validation_worker is not None:
            try:
                self._validation_worker.stop()
            except Exception:
                pass
        if self._api_server is not None:
            try:
                self._api_server.stop()
            except Exception:
                pass
        runtime_state.clear_pid_if_matches(os.getpid())

    # ------------------------------------------------------------------ #
    # Core startup                                                         #
    # ------------------------------------------------------------------ #

    async def _run(self, cfg):
        from .proxy import ProxyServer
        from team_skills.library.manager import SkillManager

        if getattr(cfg, "validation_agentshub_url", ""):
            os.environ["AGENTSHUB_REPLAY_URL"] = str(cfg.validation_agentshub_url)
        if getattr(cfg, "validation_agentshub_api_key", ""):
            os.environ["AGENTSHUB_REPLAY_API_KEY"] = str(
                cfg.validation_agentshub_api_key
            )
        os.environ["EVOLVE_VALIDATION_REQUIRED_RESULTS"] = str(
            max(1, int(getattr(cfg, "validation_required_results", 3) or 3))
        )
        os.environ["EVOLVE_VALIDATION_REQUIRED_APPROVALS"] = str(
            max(1, int(getattr(cfg, "validation_required_approvals", 2) or 2))
        )
        skill_manager: Optional[SkillManager] = None
        if cfg.use_skills:
            Path(cfg.skills_dir).mkdir(parents=True, exist_ok=True)
            skill_manager = SkillManager(
                skills_dir=cfg.skills_dir,
                public_skill_root=cfg.skills_public_root,
            )
            logger.info("[Launcher] SkillManager loaded: %s skills", skill_manager.get_skill_count())

        # Auto-pull shared skills on startup
        if cfg.sharing_enabled and cfg.sharing_auto_pull_on_start:
            try:
                from team_skills.library.hub import SkillHub

                hub = SkillHub.team_from_config(cfg)
                result = hub.pull_skills(cfg.skills_dir)
                logger.info(
                    "[Launcher] auto-pull: %d downloaded, %d unchanged, %d deleted",
                    result["downloaded"],
                    result["skipped"],
                    result.get("deleted", 0),
                )
                if skill_manager is not None and (
                    result.get("downloaded", 0) > 0
                    or result.get("deleted", 0) > 0
                    or result.get("restored_from_backup", False)
                ):
                    skill_manager.reload()
            except Exception as e:
                logger.warning("[Launcher] auto-pull failed: %s", e)

        server = ProxyServer(
            config=cfg,
            sampling_client=None,
            skill_manager=skill_manager,
        )
        server.start()
        self._api_server = server

        wait_until_ready = getattr(server, "wait_until_ready", None)
        if callable(wait_until_ready) and wait_until_ready(timeout_s=120.0):
            logger.info("[Launcher] service ready at http://%s:%d", cfg.proxy_host, cfg.proxy_port)
        elif callable(wait_until_ready):
            server.stop()
            raise RuntimeError(f"service failed to become ready on {cfg.proxy_host}:{cfg.proxy_port}")
        else:
            logger.info("[Launcher] service does not expose wait_until_ready(); skipping readiness wait")

        if getattr(cfg, "validation_enabled", False):
            try:
                from team_skills.candidates import ValidationWorker

                self._validation_worker = ValidationWorker(
                    cfg,
                    idle_provider=server,
                )
                self._validation_task = asyncio.create_task(self._validation_worker.run())
                logger.info("[Launcher] background validation worker started")
            except Exception as e:
                logger.warning("[Launcher] failed to start validation worker: %s", e)

        if getattr(cfg, "sharing_enabled", False):
            if getattr(cfg, "skills_delivery_mode", "push") == "pull":
                await asyncio.to_thread(self._cancel_skill_sync_outbox, cfg)
                logger.info("[Launcher] Skill pull enabled; pending push deliveries cancelled")
            else:
                self._skill_sync_task = asyncio.create_task(
                    self._run_skill_sync_outbox(cfg)
                )
                logger.info("[Launcher] durable Skill sync outbox started")

        self._skillopt_rollout_task = asyncio.create_task(
            self._run_skillopt_rollout(cfg)
        )
        logger.info("[Launcher] tenant-aware skillopt rollout supervisor started")

        try:
            while not self._stop_event.is_set():
                stopped = getattr(server, "_server_stopped_event", None)
                if stopped is not None and stopped.is_set():
                    raise RuntimeError("service HTTP thread stopped unexpectedly")
                await asyncio.sleep(1.0)
        finally:
            if self._validation_worker is not None:
                self._validation_worker.stop()
            if self._validation_task is not None:
                await asyncio.gather(self._validation_task, return_exceptions=True)
                self._validation_task = None
            self._validation_worker = None
            if self._skill_sync_task is not None:
                self._skill_sync_task.cancel()
                await asyncio.gather(
                    self._skill_sync_task,
                    return_exceptions=True,
                )
                self._skill_sync_task = None
            if self._skillopt_rollout_task is not None:
                self._skillopt_rollout_task.cancel()
                await asyncio.gather(
                    self._skillopt_rollout_task,
                    return_exceptions=True,
                )
                self._skillopt_rollout_task = None

    @staticmethod
    def _cancel_skill_sync_outbox(cfg) -> int:
        from team_skills.library.mutations import SkillMutationService
        from .tenants.registry import (
            TenantRegistry, effective_config, reset_current_tenant, set_current_tenant,
        )

        registry = TenantRegistry(cfg)
        cancelled = 0
        for tenant in registry.list_tenants():
            token = set_current_tenant(tenant)
            try:
                config = effective_config(registry, tenant, cfg)
                service = SkillMutationService.from_config(config, tenant_id=tenant.tenant_id)
                cancelled += service.cancel_pending_for_pull()
            finally:
                reset_current_tenant(token)
        return cancelled

    async def _run_skill_sync_outbox(self, cfg) -> None:
        from team_skills.library.mutations import SkillMutationService
        from .tenants.registry import (
            TenantRegistry, effective_config, reset_current_tenant, set_current_tenant,
        )

        registry = TenantRegistry(cfg)
        # Per-tenant services keep reconcile/outbox caches warm; new tenants
        # get a service on first sight.
        services: dict[str, SkillMutationService] = {}
        reconcile_interval_s = 60.0
        last_reconcile: dict[str, float] = {}
        while not self._stop_event.is_set():
            try:
                for tenant in registry.list_tenants():
                    token = set_current_tenant(tenant)
                    try:
                        tid = tenant.tenant_id
                        config = effective_config(registry, tenant, cfg)
                        service = services.get(tid)
                        if service is None:
                            service = SkillMutationService.from_config(config, tenant_id=tid)
                            services[tid] = service
                        loop_time = asyncio.get_running_loop().time()
                        repaired = 0
                        if loop_time - last_reconcile.get(tid, float("-inf")) >= reconcile_interval_s:
                            last_reconcile[tid] = loop_time
                            repaired = await asyncio.to_thread(service.reconcile)
                        result = await service.drain()
                        if repaired or result["synced"] or result["failed"]:
                            logger.info(
                                "[Launcher] Skill outbox tenant=%s repaired=%d synced=%d failed=%d pending=%d",
                                tid,
                                repaired,
                                result["synced"],
                                result["failed"],
                                result["pending"],
                            )
                    finally:
                        reset_current_tenant(token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Launcher] Skill outbox drain failed: %s", exc)
            await asyncio.sleep(5.0)

    async def _run_skillopt_rollout(self, cfg) -> None:
        from team_skills.library.mutations import SkillMutationService
        from .integrations.skillopt_rollout import (
            rollout_tick_with_lease,
            update_supervisor_status,
        )
        from .tenants.registry import (
            TenantRegistry, effective_config, reset_current_tenant, set_current_tenant,
        )

        registry = TenantRegistry(cfg)
        services: dict[str, SkillMutationService] = {}
        try:
            while not self._stop_event.is_set():
                tenant_states: dict[str, dict] = {}
                try:
                    for tenant in registry.list_tenants():
                        token = set_current_tenant(tenant)
                        try:
                            tid = tenant.tenant_id
                            config = effective_config(registry, tenant, cfg)
                            service = services.get(tid)
                            if service is None:
                                service = SkillMutationService.from_config(config, tenant_id=tid)
                                services[tid] = service
                            summary = await rollout_tick_with_lease(config, service)
                            tenant_states[tid] = dict(summary)
                            if summary["synced"] or summary["failed"]:
                                logger.info(
                                    "[Launcher] skillopt rollout tenant=%s state=%s synced=%d failed=%d",
                                    tid,
                                    summary["state"],
                                    summary["synced"],
                                    summary["failed"],
                                )
                        finally:
                            reset_current_tenant(token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Launcher] skillopt rollout tick failed: %s", exc)
                update_supervisor_status(tenant_states)
                await asyncio.sleep(5.0)
        finally:
            update_supervisor_status({}, running=False)

    # ------------------------------------------------------------------ #
    # PID / signals                                                        #
    # ------------------------------------------------------------------ #

    def _write_pid(self):
        pid_path = runtime_state.pid_file_path()
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))

    def _setup_signal_handlers(self):
        def _handler(signum, frame):
            logger.info("[Launcher] signal %s received — stopping …", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass
