"""Proxy server composition and lifecycle.

``ProxyServer`` composes the FastAPI route and skill synchronization mixins
into the SkillGene service. It owns threading lifecycle (uvicorn in a
background thread) and idle/validation accessors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from starlette.responses import Response

from ..config import SkillGeneConfig
from ..skills.manager import SkillManager
from .routes import RoutesMixin
from .skills_admin import SkillsAdminMixin
from .uploads import UploadsMixin
from .users_admin import UsersAdminMixin

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_RESET = "\033[0m"


class ProxyServer(
    RoutesMixin,
    UploadsMixin,
    SkillsAdminMixin,
    UsersAdminMixin,
):
    """SkillGene service: console, skill sync, user management, and validation.

    Parameters
    ----------
    config:
        SkillGeneConfig instance.
    skill_manager:
        Optional SkillManager for injecting skills into system prompts.
    """

    def __init__(
        self,
        config: SkillGeneConfig,
        sampling_client=None,
        skill_manager: Optional[SkillManager] = None,
        last_request_tracker=None,
    ):
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
        self._embedded_evolve_server = None
        self._embedded_evolve_app = None
        self._embedded_evolve_task: Optional[asyncio.Task] = None
        self._embedded_evolve_init_failed = False

        self.app = self._build_app()

        # Threading lifecycle (set by start())
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._server_stopped_event = threading.Event()

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

    async def _shutdown_cleanup(self) -> None:
        if self._skill_reload_task is not None:
            self._skill_reload_task.cancel()
            await asyncio.gather(self._skill_reload_task, return_exceptions=True)
            self._skill_reload_task = None
        await self._stop_embedded_evolve()
        await self._await_background_tasks(self._shutdown_drain_timeout_seconds)

    # ------------------------------------------------------------------ #
    # Embedded evolve server                                               #
    # ------------------------------------------------------------------ #

    def _embedded_evolve_enabled(self) -> bool:
        raw = os.environ.get("SKILLGENE_EMBEDDED_EVOLVE_ENABLED", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _ensure_skill_evolver_importable(self) -> None:
        configured = os.environ.get("SKILLGENE_EVOLVER_REPO", "").strip()
        candidates = [
            configured,
            "/home/zhangpengkun/team_evolve/team_evolve_agent",
            "/data00/home/zhangpengkun/team_evolve/team_evolve_agent",
        ]
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw).expanduser().resolve()
            if not (path / "skill_evolver").is_dir():
                continue
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
            return

    def _get_embedded_evolve_server(self):
        if not self._embedded_evolve_enabled():
            return None
        if self._embedded_evolve_server is not None:
            return self._embedded_evolve_server
        if self._embedded_evolve_init_failed:
            return None
        try:
            self._ensure_skill_evolver_importable()
            from skill_evolver.kernel.settings import EvolveServerConfig
            from skill_evolver.runtime.orchestrator import EvolveServer

            evolve_config = EvolveServerConfig.from_skillgene_config(self.config)
            evolve_config.http_port = int(getattr(self.config, "proxy_port", 52010) or 52010)
            interval = os.environ.get("SKILLGENE_EMBEDDED_EVOLVE_INTERVAL_S", "").strip()
            if interval:
                evolve_config.interval_seconds = max(1, int(interval))
            evolve_config.__post_init__()
            self._embedded_evolve_server = EvolveServer(evolve_config)
            logger.info(
                "[EvolveServer] embedded in SkillGene on port %s interval=%ss",
                evolve_config.http_port,
                evolve_config.interval_seconds,
            )
        except Exception:
            self._embedded_evolve_init_failed = True
            logger.warning("[EvolveServer] embedded startup disabled; import/config failed", exc_info=True)
            return None
        return self._embedded_evolve_server

    def _get_embedded_evolve_app(self):
        if self._embedded_evolve_app is not None:
            return self._embedded_evolve_app
        server = self._get_embedded_evolve_server()
        if server is None:
            return None
        app = server.create_http_app()
        try:
            from skill_evolver.__main__ import _mount_dreamcycle_route

            _mount_dreamcycle_route(app)
        except Exception:
            logger.debug("[EvolveServer] DreamCycle route not mounted", exc_info=True)
        self._embedded_evolve_app = app
        return app

    def _start_embedded_evolve(self) -> None:
        server = self._get_embedded_evolve_server()
        if server is None:
            return
        if self._embedded_evolve_task is not None and not self._embedded_evolve_task.done():
            return
        self._embedded_evolve_task = asyncio.create_task(server.run_periodic())
        logger.info("[EvolveServer] embedded periodic loop started")

    async def _stop_embedded_evolve(self) -> None:
        server = self._embedded_evolve_server
        if server is not None:
            try:
                server.stop()
            except Exception:
                logger.debug("[EvolveServer] embedded stop failed", exc_info=True)
        task = self._embedded_evolve_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._embedded_evolve_task = None

    async def _dispatch_embedded_evolve_request(self, request) -> Optional[Response]:
        app = self._get_embedded_evolve_app()
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
        async with httpx.AsyncClient(transport=transport, base_url="http://skillgene-embedded-evolve") as client:
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
            log_level="info",
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
        banner = (
            f"\n{'=' * 70}\n"
            f"  SkillGene service ready\n"
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
        return self._ready_event.wait(timeout=timeout_s)

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
