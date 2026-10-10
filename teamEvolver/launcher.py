"""teamEvolver service launcher."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading

from . import runtime_state
from .config_store import ConfigStore

logger = logging.getLogger(__name__)


class Launcher:
    """Start/stop teamEvolver services based on ConfigStore."""

    def __init__(self, config_store: ConfigStore):
        self.cs = config_store
        self._api_server = None
        self._stop_event = threading.Event()
        self._validation_workers = {}
        self._validation_tasks = {}
        self._validation_task = None
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
        for worker in list(self._validation_workers.values()):
            try:
                worker.stop()
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

        server = ProxyServer(
            config=cfg,
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
            self._validation_task = asyncio.create_task(
                self._run_validation_workers(cfg, server)
            )
            logger.info("[Launcher] tenant-aware validation supervisor started")

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
            if self._validation_task is not None:
                self._validation_task.cancel()
                await asyncio.gather(self._validation_task, return_exceptions=True)
                self._validation_task = None
            if self._skillopt_rollout_task is not None:
                self._skillopt_rollout_task.cancel()
                await asyncio.gather(
                    self._skillopt_rollout_task,
                    return_exceptions=True,
                )
                self._skillopt_rollout_task = None

    async def _run_validation_workers(self, cfg, server) -> None:
        from team_skills.candidates import ValidationWorker

        from .tenants.registry import (
            TenantRegistry,
            effective_config,
            reset_current_tenant,
            set_current_tenant,
        )

        registry = TenantRegistry(cfg)
        try:
            while not self._stop_event.is_set():
                try:
                    tenants = await asyncio.to_thread(registry.list_tenants)
                    enabled = {}
                    for tenant in tenants:
                        if tenant.status != "active":
                            continue
                        config = effective_config(registry, tenant, cfg)
                        if bool(getattr(config, "validation_enabled", False)):
                            enabled[tenant.tenant_id] = (tenant, config)

                    for tenant_id in set(self._validation_workers) - set(enabled):
                        worker = self._validation_workers.pop(tenant_id)
                        task = self._validation_tasks.pop(tenant_id)
                        worker.stop()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                    for tenant_id, (tenant, config) in enabled.items():
                        current = self._validation_tasks.get(tenant_id)
                        if current is not None and not current.done():
                            continue
                        token = set_current_tenant(tenant)
                        try:
                            worker = ValidationWorker(
                                config,
                                idle_provider=server,
                                tenant_id=tenant_id,
                                tenant_context=tenant,
                            )
                            self._validation_workers[tenant_id] = worker
                            self._validation_tasks[tenant_id] = asyncio.create_task(
                                worker.run()
                            )
                        finally:
                            reset_current_tenant(token)
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Launcher] validation supervisor failed: %s", exc)
                    await asyncio.sleep(5.0)
        finally:
            workers = list(self._validation_workers.values())
            tasks = list(self._validation_tasks.values())
            for worker in workers:
                try:
                    worker.stop()
                except Exception:
                    pass
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._validation_workers.clear()
            self._validation_tasks.clear()

    async def _run_skillopt_rollout(self, cfg) -> None:
        from team_skills.library.mutations import SkillMutationService

        from .integrations.skillopt_rollout import (
            rollout_tick_with_lease,
            update_supervisor_status,
        )
        from .tenants.registry import (
            TenantRegistry,
            effective_config,
            reset_current_tenant,
            set_current_tenant,
        )

        registry = TenantRegistry(cfg)
        services: dict[str, SkillMutationService] = {}
        last_reconcile: dict[str, float] = {}
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
                            loop_time = asyncio.get_running_loop().time()
                            if loop_time - last_reconcile.get(tid, float("-inf")) >= 60.0:
                                await asyncio.to_thread(service.reconcile)
                                last_reconcile[tid] = loop_time
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
        def _handler(signum, _frame):
            logger.info("[Launcher] signal %s received — stopping …", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass
