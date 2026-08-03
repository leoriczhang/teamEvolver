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

logger = logging.getLogger(__name__)

_PID_FILE = Path.home() / ".teamEvolver" / "teamEvolver.pid"


class Launcher:
    """Start/stop teamEvolver services based on ConfigStore."""

    def __init__(self, config_store: ConfigStore):
        self.cs = config_store
        self._api_server = None
        self._stop_event = threading.Event()
        self._validation_worker = None
        self._validation_task = None

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def start(self):
        cfg = self.cs.to_config()
        logger.info("[Launcher] Starting teamEvolver …")
        self._write_pid()
        self._setup_signal_handlers()
        await self._run(cfg)

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
        _PID_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Core startup                                                         #
    # ------------------------------------------------------------------ #

    async def _run(self, cfg):
        from .proxy import ProxyServer
        from .skills.manager import SkillManager

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
                from .skills.hub import SkillHub

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
        if callable(wait_until_ready) and wait_until_ready(timeout_s=30.0):
            logger.info("[Launcher] service ready at http://%s:%d", cfg.proxy_host, cfg.proxy_port)
        elif callable(wait_until_ready):
            logger.warning(
                "[Launcher] service did not report ready within timeout on http://%s:%d",
                cfg.proxy_host,
                cfg.proxy_port,
            )
        else:
            logger.info("[Launcher] service does not expose wait_until_ready(); skipping readiness wait")

        if getattr(cfg, "validation_enabled", False):
            try:
                from .validation import ValidationWorker

                self._validation_worker = ValidationWorker(
                    cfg,
                    idle_provider=server,
                )
                self._validation_task = asyncio.create_task(self._validation_worker.run())
                logger.info("[Launcher] background validation worker started")
            except Exception as e:
                logger.warning("[Launcher] failed to start validation worker: %s", e)

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(1.0)
        finally:
            if self._validation_worker is not None:
                self._validation_worker.stop()
            if self._validation_task is not None:
                await asyncio.gather(self._validation_task, return_exceptions=True)
                self._validation_task = None
            self._validation_worker = None

    # ------------------------------------------------------------------ #
    # PID / signals                                                        #
    # ------------------------------------------------------------------ #

    def _write_pid(self):
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()))

    def _setup_signal_handlers(self):
        def _handler(signum, frame):
            logger.info("[Launcher] signal %s received — stopping …", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass
