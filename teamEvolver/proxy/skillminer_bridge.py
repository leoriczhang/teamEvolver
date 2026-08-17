"""SkillMiner bridge: run the vendored SkillMiner web console as a local
subprocess and reverse-proxy ``/api/mining/*`` (including its SSE stream)
to it.

SkillMiner (``teamEvolver/skillminer``) ships its own zero-dependency console
built on the stdlib ``http.server`` with a hand-rolled SSE event bus, and it
drives the mining pipeline by importing ``run_pipeline`` in-process while
shelling out to the ``hermes`` CLI. Rather than re-implement any of that, we
launch it verbatim on an internal loopback port and expose it through the
teamEvolver FastAPI app under a ``/api/mining`` prefix so the unified console can
talk to a single origin.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config_store import ConfigStore
from ..mining_settings import reset_prompt, settings_payload, update_settings

logger = logging.getLogger(__name__)

# Path to the vendored SkillMiner project root inside the package.
_SKILLMINER_ROOT = Path(__file__).resolve().parent.parent / "skillminer"
_CONSOLE_SERVER = _SKILLMINER_ROOT / "web_console" / "server.py"

# Internal prefix the unified console uses; stripped before proxying upstream.
_MINING_PREFIX = "/api/mining"


def _is_mining_path(path: str) -> bool:
    return path == _MINING_PREFIX or path.startswith(_MINING_PREFIX + "/")


def _pick_free_port(preferred: int) -> int:
    """Return ``preferred`` if free, otherwise an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SkillMinerBridgeMixin:
    """Mixin adding SkillMiner subprocess lifecycle + reverse-proxy routes.

    Expected to be composed into :class:`ProxyServer`, which provides
    ``self.config``. Enabled unless ``TEAMEVOLVER_SKILLMINER_ENABLED`` is falsy.
    """

    _skillminer_proc: Optional[subprocess.Popen] = None
    _skillminer_port: Optional[int] = None
    _skillminer_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Enablement / lifecycle                                             #
    # ------------------------------------------------------------------ #

    def _skillminer_enabled(self) -> bool:
        raw = os.environ.get("TEAMEVOLVER_SKILLMINER_ENABLED", "1").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        return _CONSOLE_SERVER.is_file()

    def _start_skillminer(self) -> None:
        if not self._skillminer_enabled():
            return
        with self._skillminer_lock:
            if self._skillminer_proc is not None and self._skillminer_proc.poll() is None:
                return
            port = _pick_free_port(int(os.environ.get("TEAMEVOLVER_SKILLMINER_PORT", "8765") or 8765))
            env = dict(os.environ)
            env["PORT"] = str(port)
            config_file = str(
                getattr(self.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            mining = (
                data.get("mining")
                if isinstance(data.get("mining"), dict)
                else {}
            )
            model = (
                mining.get("model")
                if isinstance(mining.get("model"), dict)
                else {}
            )
            llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
            effective = settings_payload(data)
            effective_model = effective["model"]
            effective_pipeline = effective["pipeline"]
            env["TEAMEVOLVER_CONFIG_FILE"] = str(store.config_file)
            env["SKILLMINER_MODEL_PROVIDER"] = str(
                effective_model.get("provider") or "custom"
            )
            env["SKILLMINER_MODEL_ID"] = str(
                effective_model.get("model") or ""
            )
            env["SKILLMINER_MODEL_BASE_URL"] = str(
                effective_model.get("base_url") or ""
            )
            env["SKILLMINER_MODEL_MAX_TOKENS"] = str(
                effective_model.get("max_tokens") or 100000
            )
            env["SKILLMINER_MODEL_CONTEXT_LENGTH"] = str(
                effective_model.get("context_length") or 240000
            )
            api_key = str(
                model.get("api_key")
                or llm.get("api_key")
                or getattr(self.config, "llm_api_key", "")
                or ""
            ).strip()
            if api_key:
                env["ARK_API_KEY"] = api_key
                env["SKILLMINER_MODEL_API_KEY"] = api_key
            env["SKILLMINER_MAX_ROUNDS"] = str(
                effective_pipeline["max_rounds"]
            )
            env["SKILLMINER_HERMES_RETRIES"] = str(
                effective_pipeline["max_retries"]
            )
            env["SKILLMINER_HERMES_BACKOFF"] = str(
                effective_pipeline["retry_backoff_seconds"]
            )
            env["SKILLMINER_ONESHOT_TIMEOUT"] = str(
                effective_pipeline["oneshot_timeout_seconds"]
            )
            env["SKILLMINER_STEP1_VALIDATION_RETRIES"] = str(
                effective_pipeline["step1_validation_retries"]
            )
            env["SKILLMINER_STRICT_STEP1"] = (
                "1" if effective_pipeline["strict_step1"] else "0"
            )
            env["SKILLMINER_BENCHMARK_TARGET_TOTAL"] = str(
                effective_pipeline["benchmark_target_total"]
            )
            env["SKILLMINER_BENCHMARK_DIFFICULTY_DIST"] = str(
                effective_pipeline["benchmark_difficulty_dist"]
            )
            env["SKILLMINER_BENCHMARK_MAX_TURNS"] = str(
                effective_pipeline["benchmark_max_turns"]
            )
            try:
                self._skillminer_proc = subprocess.Popen(
                    [sys.executable, str(_CONSOLE_SERVER)],
                    cwd=str(_SKILLMINER_ROOT),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
                self._skillminer_port = port
                logger.info("[SkillMiner] console subprocess started on 127.0.0.1:%s", port)
            except Exception:
                self._skillminer_proc = None
                self._skillminer_port = None
                logger.warning("[SkillMiner] failed to start console subprocess", exc_info=True)

    def _stop_skillminer(self) -> None:
        with self._skillminer_lock:
            proc = self._skillminer_proc
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                logger.debug("[SkillMiner] stop failed", exc_info=True)
            finally:
                self._skillminer_proc = None
                self._skillminer_port = None
                logger.info("[SkillMiner] console subprocess stopped")

    def _skillminer_base_url(self) -> Optional[str]:
        if self._skillminer_port is None:
            return None
        return f"http://127.0.0.1:{self._skillminer_port}"

    def _wait_skillminer_ready(self, timeout_s: float = 8.0) -> bool:
        base = self._skillminer_base_url()
        if base is None:
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            proc = self._skillminer_proc
            if proc is not None and proc.poll() is not None:
                return False  # subprocess died
            try:
                with httpx.Client(timeout=1.0) as client:
                    resp = client.get(f"{base}/api/config")
                if resp.status_code < 500:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    async def _await_skillminer_ready(self, timeout_s: float = 8.0) -> bool:
        """Async readiness probe so we never block the uvicorn event loop."""
        import asyncio

        base = self._skillminer_base_url()
        if base is None:
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            proc = self._skillminer_proc
            if proc is not None and proc.poll() is not None:
                return False  # subprocess died
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    resp = await client.get(f"{base}/api/config")
                if resp.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    # ------------------------------------------------------------------ #
    # Reverse proxy                                                       #
    # ------------------------------------------------------------------ #

    async def _dispatch_skillminer_request(self, request: Request) -> Optional[Response]:
        """Proxy a ``/api/mining/*`` request to the SkillMiner subprocess.

        Streams the response so the SSE endpoint (``/api/mining/events``)
        forwards events as they arrive.
        """
        # Lazily (re)start the subprocess on first use.
        if self._skillminer_proc is None or self._skillminer_proc.poll() is not None:
            self._start_skillminer()
            if not await self._await_skillminer_ready():
                logger.warning("[SkillMiner] console did not become ready within the startup timeout")
                return JSONResponse(
                    status_code=503,
                    content={"detail": "SkillMiner console failed to start or did not become ready"},
                )

        base = self._skillminer_base_url()
        if base is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "SkillMiner console is not available"},
            )

        # Map the unified-console prefix onto SkillMiner's own API surface:
        #   /api/mining/config  -> /api/config
        #   /api/mining/events  -> /api/events   (SSE)
        #   /api/mining         -> /api
        rest = request.url.path[len(_MINING_PREFIX):]  # e.g. "/config" or ""
        upstream_path = "/api" + rest if rest else "/api"
        target = upstream_path
        if request.url.query:
            target = f"{upstream_path}?{request.url.query}"

        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in request.scope.get("headers", [])
            if key.lower() not in {b"host", b"content-length", b"connection"}
        }
        console_user = getattr(request.state, "console_user", None)
        if isinstance(console_user, dict):
            reviewer = str(
                console_user.get("username")
                or console_user.get("name")
                or console_user.get("id")
                or "authenticated-user"
            ).strip()
            reviewer = "".join(ch for ch in reviewer if ch.isprintable() and ch not in "\r\n")
            headers["X-teamEvolver-Reviewer"] = reviewer[:120]
        body = await request.body()

        is_sse = upstream_path.rstrip("/").endswith("/events")
        client = httpx.AsyncClient(base_url=base, timeout=None if is_sse else 120.0)
        try:
            req = client.build_request(request.method, target, content=body, headers=headers)
            upstream = await client.send(req, stream=True)
        except Exception:
            await client.aclose()
            logger.warning("[SkillMiner] proxy request failed", exc_info=True)
            return JSONResponse(status_code=502, content={"detail": "SkillMiner upstream error"})

        resp_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in {"content-length", "connection", "transfer-encoding", "content-encoding"}
        }

        async def _body_iter():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _body_iter(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )

    def _register_skillminer_routes(self, app: FastAPI) -> None:
        """Register a catch-all route for ``/api/mining/*``."""
        owner = self

        def _store() -> ConfigStore:
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            return (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )

        def _require_admin(request: Request) -> Optional[JSONResponse]:
            user = getattr(request.state, "console_user", None)
            if not isinstance(user, dict) or str(user.get("role") or "") != "admin":
                return JSONResponse(
                    status_code=403,
                    content={"detail": "only admin users can edit mining settings"},
                )
            return None

        @app.get("/api/mining/settings")
        async def mining_settings_get():
            store = _store()
            return JSONResponse(content=settings_payload(store.load()))

        @app.post("/api/mining/settings")
        async def mining_settings_save(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            try:
                body = await request.json()
                store = _store()
                data = store.load()
                payload = update_settings(data, body)
                store.save(data)
            except (ValueError, TypeError) as exc:
                return JSONResponse(
                    status_code=400,
                    content={"detail": str(exc)},
                )
            owner._stop_skillminer()
            return JSONResponse(content=payload)

        @app.post("/api/mining/prompts/{stage_id}/reset")
        async def mining_prompt_reset(stage_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            try:
                store = _store()
                data = store.load()
                payload = reset_prompt(data, stage_id)
                store.save(data)
            except KeyError as exc:
                return JSONResponse(
                    status_code=404,
                    content={"detail": str(exc)},
                )
            owner._stop_skillminer()
            return JSONResponse(content=payload)

        async def _mining_proxy(request: Request):
            response = await owner._dispatch_skillminer_request(request)
            if response is None:
                return JSONResponse(status_code=502, content={"detail": "SkillMiner upstream error"})
            return response

        app.add_api_route(
            "/api/mining/{path:path}",
            _mining_proxy,
            methods=["GET", "POST", "DELETE"],
        )
