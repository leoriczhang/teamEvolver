"""SkillMiner bridge: run the vendored SkillMiner web console as a local
subprocess and reverse-proxy ``/api/mining/*`` (including its SSE stream)
to it.

SkillMiner (``team_miner``) ships its own zero-dependency console
built on the stdlib ``http.server`` with a hand-rolled SSE event bus, and it
drives the mining pipeline by importing ``run_pipeline`` in-process while
shelling out to the ``hermes`` CLI. Rather than re-implement any of that, we
launch it verbatim on an internal loopback port and expose it through the
teamEvolver FastAPI app under a ``/api/mining`` prefix so the unified console can
talk to a single origin.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from team_memory.compile_client import CompileClient
from teamEvolver.config_store import CONFIG_DIR, ConfigStore
from teamEvolver.proxy.knowledge_document_converter import convert_knowledge_upload
from teamEvolver.proxy.knowledge_mining import _content_editable, _content_text

from .ov_compile_mining import (
    ACTIVE_JOB_STATES,
    RUNS_ROOT,
    SKILL_BODY,
    SKILL_NAME,
    SKILL_URI,
    OvSkillMiningRepository,
    utc_now,
    validate_descendant,
    validate_resource_uri,
)
from .settings import (
    reset_prompt,
    settings_payload,
    update_settings,
)

logger = logging.getLogger(__name__)

# Path to the SkillMiner project root.
_SKILLMINER_ROOT = Path(__file__).resolve().parent
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
    _skillminer_ready: bool = False
    _skillminer_lock = threading.Lock()

    def _ov_skill_mining_repository(self) -> OvSkillMiningRepository:
        repository = getattr(self, "_ov_skill_mining_repo", None)
        if repository is None:
            repository = OvSkillMiningRepository(
                CONFIG_DIR / "skill_mining",
                _SKILLMINER_ROOT,
            )
            self._ov_skill_mining_repo = repository
        return repository

    def _ov_skill_mining_lock(self, job_id: str) -> asyncio.Lock:
        locks = getattr(self, "_ov_skill_mining_locks", None)
        if locks is None:
            locks = {}
            self._ov_skill_mining_locks = locks
        return locks.setdefault(job_id, asyncio.Lock())

    async def _ov_source_detail(self, record: dict) -> dict:
        account = await asyncio.to_thread(self._knowledge_account, record.get("account"))
        uri = validate_resource_uri(record.get("uri"))
        tree = await self._knowledge_tree(account, uri)
        entries = tree.get("entries") or []
        files = [entry for entry in entries if not entry.get("is_dir")]
        exists = bool(tree.get("exists"))
        status = "ready" if exists and files else "empty" if exists else "missing"
        return {
            "source_id": record["source_id"],
            "account": account,
            "path": uri,
            "uri": uri,
            "name": str(record.get("name") or uri.rsplit("/", 1)[-1]),
            "document_count": len(files),
            "total_bytes": sum(int(entry.get("size") or 0) for entry in files),
            "ready": status == "ready",
            "status": status,
            "created_at": record.get("created_at") or "",
            "ingestion": {
                "schema_version": 1,
                "source_path": uri,
                "batch_id": "",
                "status": "ready" if status == "ready" else "empty" if status == "empty" else "failed",
                "stage": "complete" if exists else "failed",
                "progress": 100,
                "processed_files": len(files),
                "total_files": len(files),
                "current_file": "",
                "error": "" if exists else "OpenViking 中的目录已被删除，请重新选择知识源目录",
                "started_at": "",
                "updated_at": utc_now(),
                "finished_at": utc_now(),
            },
            "tree": tree,
        }

    async def _materialize_ov_skill_job(self, job: dict) -> dict:
        repository = self._ov_skill_mining_repository()
        output_uri = validate_resource_uri(job.get("output_uri"))
        tree = await self._knowledge_tree(str(job.get("account") or ""), output_uri)
        if not tree.get("exists"):
            raise ValueError("OV Compile 已结束，但输出目录不存在")
        files = [entry for entry in tree.get("entries") or [] if not entry.get("is_dir")]
        if len(files) > 128:
            raise ValueError("OV Compile 产物超过 128 个文件，拒绝物化")
        total_bytes = 0
        written = 0
        for entry in files:
            uri = validate_descendant(output_uri, entry.get("uri"), allow_root=False)
            relative = uri[len(output_uri) + 1 :]
            if PurePosixPath(relative).name.startswith("."):
                continue
            result = await self._knowledge_request(
                str(job.get("account") or ""),
                "GET",
                "/api/v1/content/read",
                params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"},
            )
            content = _content_text(result)
            total_bytes += len(content.encode("utf-8"))
            if total_bytes > 16 * 1024 * 1024:
                raise ValueError("OV Compile 产物超过 16 MB，拒绝物化")
            await asyncio.to_thread(repository.write_output, job["job_id"], relative, content)
            written += 1
        repository.append_log(job["job_id"], f"已从 OpenViking 物化 {written} 个候选产物")
        return await asyncio.to_thread(repository.finalize_materialized, job["job_id"])

    async def _refresh_ov_skill_job(self, job: dict, *, detail: bool = False) -> dict:
        repository = self._ov_skill_mining_repository()
        if job.get("engine") != "ov_compile":
            return await asyncio.to_thread(repository.public_job, job, detail=detail)
        if job.get("status") not in ACTIVE_JOB_STATES:
            return await asyncio.to_thread(repository.public_job, job, detail=detail)
        task_id = str(job.get("ov_task_id") or "")
        if not task_id:
            return await asyncio.to_thread(repository.public_job, job, detail=detail)
        async with self._ov_skill_mining_lock(job["job_id"]):
            job = await asyncio.to_thread(repository.get_job, job["job_id"])
            if job.get("status") not in ACTIVE_JOB_STATES:
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
            task = await self._knowledge_request(
                str(job.get("account") or ""),
                "GET",
                f"/api/v1/tasks/{task_id}",
                params={"include_events": "true"},
            )
            status = str((task or {}).get("status") or "").lower() if isinstance(task, dict) else ""
            stage = str((task or {}).get("stage") or status) if isinstance(task, dict) else status
            if status in {"accepted", "pending", "queued"}:
                public_status = "queued"
                phase = {"step1": "active", "step2": "idle", "step3": "idle"}
            elif status in {"running", "committing", "cancelling"}:
                public_status = "stopping" if status == "cancelling" else "running"
                phase = {"step1": "done", "step2": "active", "step3": "idle"}
            elif status in {"completed", "succeeded", "success"}:
                public_status = "succeeded"
                phase = {"step1": "done", "step2": "done", "step3": "active"}
            elif status in {"cancelled", "canceled"}:
                public_status = "stopped"
                phase = {"step1": "done", "step2": "done", "step3": "idle"}
            elif status == "failed":
                public_status = "failed"
                phase = {"step1": "done", "step2": "done", "step3": "idle"}
            else:
                public_status = str(job.get("status") or "running")
                phase = job.get("phase") or {"step1": "done", "step2": "active", "step3": "idle"}
            error = ""
            if public_status == "failed":
                raw_error = (task or {}).get("error") if isinstance(task, dict) else None
                error = raw_error if isinstance(raw_error, str) else json.dumps(raw_error or {}, ensure_ascii=False)
            changes = {
                "status": public_status,
                "phase": phase,
                "ov_status": status,
                "ov_stage": stage,
                "error": error,
            }
            if public_status in {"succeeded", "failed", "stopped"}:
                changes["finished_at"] = str((task or {}).get("updated_at") or utc_now())
            job = await asyncio.to_thread(repository.update_job, job["job_id"], **changes)
            if public_status == "succeeded" and not job.get("materialized"):
                try:
                    job = await self._materialize_ov_skill_job(job)
                    job = await asyncio.to_thread(
                        repository.update_job,
                        job["job_id"],
                        status="succeeded",
                        phase={"step1": "done", "step2": "done", "step3": "done"},
                        finished_at=job.get("finished_at") or utc_now(),
                    )
                except Exception as exc:
                    repository.append_log(job["job_id"], f"候选产物物化失败：{exc}")
                    attempts = int(job.get("materialize_attempts") or 0) + 1
                    if attempts < 5:
                        job = await asyncio.to_thread(
                            repository.update_job,
                            job["job_id"],
                            status="running",
                            error="",
                            finished_at="",
                            materialize_attempts=attempts,
                            ov_stage="materializing",
                        )
                        return await asyncio.to_thread(repository.public_job, job, detail=detail)
                    job = await asyncio.to_thread(
                        repository.update_job,
                        job["job_id"],
                        status="failed",
                        error=f"候选产物物化失败：{exc}",
                        finished_at=utc_now(),
                        materialize_attempts=attempts,
                    )
            return await asyncio.to_thread(repository.public_job, job, detail=detail)

    # ------------------------------------------------------------------ #
    # Enablement / lifecycle                                             #
    # ------------------------------------------------------------------ #

    def _skillminer_enabled(self) -> bool:
        raw = os.environ.get("TEAMEVOLVER_SKILLMINER_ENABLED", "1").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        return _CONSOLE_SERVER.is_file()

    def _skillminer_runtime_root(self) -> Path:
        if os.environ.get("TEAMEVOLVER_CUSTOMER_MODE") != "1" or not getattr(self.config, "storage_pg_enabled", False):
            return _SKILLMINER_ROOT
        # Refresh implementation files while retaining uploaded inputs/jobs on
        # the persistent volume. Never copy one deployment's generated data.
        root = Path.home() / ".teamEvolver" / "skillminer"
        shutil.copytree(_SKILLMINER_ROOT, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".hermes_home", ".env", "data",
            ".knowledge_originals", ".knowledge_locks", "mining_jobs",
            "sample_packages", "semantic_reports", "compiled_skill", "benchmark_results",
            "benchmark_sessions", "trajectory_benchmarks", "coverage_reports",
            "reflection_rounds", "run_history", "lift_datasets", "skill_test_results",
        ))
        (root / "data" / "input").mkdir(parents=True, exist_ok=True)
        return root

    def _start_skillminer(self) -> None:
        if not self._skillminer_enabled():
            return
        with self._skillminer_lock:
            if self._skillminer_proc is not None and self._skillminer_proc.poll() is None:
                return
            self._skillminer_ready = False
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
            llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
            effective = settings_payload(data)
            effective_model = effective["model"]
            effective_pipeline = effective["pipeline"]
            env["TEAMEVOLVER_CONFIG_FILE"] = str(store.config_file)
            env["SKILLMINER_USE_UNIFIED_MODEL_CONFIG"] = "1"
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
                llm.get("api_key")
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
                runtime_root = self._skillminer_runtime_root()
                env["PYTHONUNBUFFERED"] = "1"
                self._skillminer_proc = subprocess.Popen(
                    [sys.executable, str(runtime_root / "web_console" / "server.py")],
                    cwd=str(runtime_root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                from teamEvolver.logging_subprocess import capture_subprocess
                self._skillminer_log_thread = capture_subprocess(self._skillminer_proc, env)
                self._skillminer_port = port
                pid = getattr(self._skillminer_proc, "pid", None)
                if pid:
                    (Path(tempfile.gettempdir()) / f"te_skillminer_{pid}.pid").write_text(str(pid))
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
                        proc.wait(timeout=5)
            except Exception:
                logger.debug("[SkillMiner] stop failed", exc_info=True)
            finally:
                reader = getattr(self, "_skillminer_log_thread", None)
                if reader:
                    reader.join(timeout=1)
                self._skillminer_ready = False
                pid = getattr(proc, "pid", None)
                if pid:
                    (Path(tempfile.gettempdir()) / f"te_skillminer_{pid}.pid").unlink(missing_ok=True)
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
            await asyncio.to_thread(self._start_skillminer)
        if not self._skillminer_ready:
            if not await self._await_skillminer_ready():
                logger.warning("[SkillMiner] console did not become ready within the startup timeout")
                return JSONResponse(
                    status_code=503,
                    content={"detail": "SkillMiner console failed to start or did not become ready"},
                )
            self._skillminer_ready = True

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

        @app.get("/api/mining/model")
        async def mining_model_get():
            return JSONResponse(
                status_code=410,
                content={"detail": "挖掘模型已复用全局模型，请在“全局模型”中查看和配置"},
            )

        @app.post("/api/mining/model")
        async def mining_model_save(request: Request):
            del request
            return JSONResponse(
                status_code=410,
                content={"detail": "挖掘模型已复用全局模型，请在“全局模型”中统一配置"},
            )

        @app.post("/api/mining/model/test")
        async def mining_model_test(request: Request):
            del request
            return JSONResponse(
                status_code=410,
                content={"detail": "请在“全局模型”中测试并保存模型配置"},
            )

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

        @app.get("/api/mining/ov-sources")
        async def mining_ov_sources(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            accounts = await asyncio.to_thread(owner._knowledge_accounts)
            records = await asyncio.to_thread(repository.list_source_records)
            source_probe_limit = asyncio.Semaphore(8)

            async def describe(record: dict) -> dict:
                try:
                    async with source_probe_limit:
                        return await owner._ov_source_detail(record)
                except Exception as exc:  # Keep a deleted/unavailable reference visible for repair.
                    uri = str(record.get("uri") or "")
                    return {
                        "source_id": record.get("source_id"),
                        "account": record.get("account"),
                        "path": uri,
                        "uri": uri,
                        "name": record.get("name") or uri.rsplit("/", 1)[-1],
                        "document_count": 0,
                        "total_bytes": 0,
                        "ready": False,
                        "status": "unavailable",
                        "created_at": record.get("created_at") or "",
                        "error": str(exc),
                        "ingestion": {
                            "schema_version": 1,
                            "source_path": uri,
                            "batch_id": "",
                            "status": "failed",
                            "stage": "failed",
                            "progress": 100,
                            "processed_files": 0,
                            "total_files": 0,
                            "current_file": "",
                            "error": "暂时无法检查 OpenViking 目录，请稍后重试",
                            "started_at": "",
                            "updated_at": utc_now(),
                            "finished_at": utc_now(),
                        },
                    }

            sources = await asyncio.gather(*(describe(record) for record in records)) if records else []
            return JSONResponse(
                {
                    "accounts": accounts.get("accounts") or [],
                    "current": accounts.get("current") or "",
                    "root_uri": "viking://resources",
                    "sources": sources,
                    "skill_uri": SKILL_URI,
                    "error": accounts.get("error") or "",
                }
            )

        @app.get("/api/mining/ov-tree")
        async def mining_ov_tree(
            request: Request,
            account: str = Query(...),
            uri: str = Query("viking://resources"),
        ):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            root = validate_resource_uri(uri, allow_root=True)
            tree = await owner._knowledge_tree(selected, root)
            return JSONResponse({"account": selected, **tree})

        @app.post("/api/mining/ov-sources", status_code=201)
        async def mining_ov_source_create(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            body = await request.json()
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            uri = validate_resource_uri(body.get("uri"))
            tree = await owner._knowledge_tree(selected, uri)
            if not tree.get("exists"):
                raise HTTPException(status_code=404, detail="所选 OpenViking 目录不存在")
            repository = owner._ov_skill_mining_repository()
            record = await asyncio.to_thread(
                repository.save_source,
                selected,
                uri,
                name=str(body.get("name") or ""),
            )
            source = await owner._ov_source_detail(record)
            return JSONResponse(status_code=201, content={"ok": True, "source": source})

        @app.delete("/api/mining/ov-sources/{source_id}")
        async def mining_ov_source_remove(source_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(repository.remove_source, source_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            # Removing a source unregisters only the reference.  The OV folder
            # is deliberately preserved; entries are deleted via /ov-entry.
            return JSONResponse({"ok": True, "source_id": source_id, "uri": record.get("uri")})

        @app.get("/api/mining/ov-source-tree")
        async def mining_ov_source_tree(request: Request, source_id: str = Query(...)):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(repository.get_source, source_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            source = await owner._ov_source_detail(record)
            return JSONResponse(
                {
                    "source": {key: value for key, value in source.items() if key != "tree"},
                    **source["tree"],
                }
            )

        @app.get("/api/mining/ov-source-content")
        async def mining_ov_source_content(
            request: Request,
            source_id: str = Query(...),
            uri: str = Query(...),
        ):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(repository.get_source, source_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            target = validate_descendant(str(record.get("uri") or ""), uri, allow_root=False)
            result = await owner._knowledge_request(
                str(record.get("account") or ""),
                "GET",
                "/api/v1/content/read",
                params={"uri": target, "offset": 0, "limit": -1, "raw": "true"},
            )
            return JSONResponse(
                {
                    "source_id": source_id,
                    "uri": target,
                    "name": target.rsplit("/", 1)[-1],
                    "content": _content_text(result),
                    "editable": _content_editable(target),
                }
            )

        @app.post("/api/mining/ov-source-content")
        async def mining_ov_source_content_save(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            body = await request.json()
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(repository.get_source, str(body.get("source_id") or ""))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            target = validate_descendant(str(record.get("uri") or ""), body.get("uri"), allow_root=False)
            if not _content_editable(target):
                raise HTTPException(status_code=400, detail="该文件类型不支持在线编辑")
            content = body.get("content")
            original = body.get("original_content")
            if not isinstance(content, str) or not isinstance(original, str):
                raise HTTPException(status_code=400, detail="文件内容必须是文本")
            if len(content.encode("utf-8")) > 2 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="知识源文件不能超过 2 MB")
            current_result = await owner._knowledge_request(
                str(record.get("account") or ""),
                "GET",
                "/api/v1/content/read",
                params={"uri": target, "offset": 0, "limit": -1, "raw": "true"},
            )
            current = _content_text(current_result)
            if current not in {original, content}:
                raise HTTPException(status_code=409, detail="文件已被其他操作更新，请重新打开")
            result = await owner._knowledge_request(
                str(record.get("account") or ""),
                "POST",
                "/api/v1/content/write",
                json={"uri": target, "content": content, "mode": "replace", "wait": False},
            )
            return JSONResponse({"saved": True, "uri": target, "content": content, "result": result})

        @app.post("/api/mining/ov-source-upload", status_code=202)
        async def mining_ov_source_upload(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            body = await request.json()
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(repository.get_source, str(body.get("source_id") or ""))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            root = str(record.get("uri") or "")
            parent = validate_descendant(root, body.get("parent_uri") or root)
            files = body.get("files")
            if not isinstance(files, list) or not files or len(files) > 50:
                raise HTTPException(status_code=400, detail="单次需上传 1 到 50 个文件")
            prepared: list[tuple[str, str]] = []
            total = 0
            seen: set[str] = set()
            for item in files:
                if not isinstance(item, dict):
                    raise HTTPException(status_code=400, detail="上传文件格式无效")
                relative = str(item.get("relative_path") or item.get("name") or "").replace("\\", "/").lstrip("/")
                parts = [part for part in relative.split("/") if part]
                if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
                    raise HTTPException(status_code=400, detail=f"上传路径无效：{relative}")
                encoded = item.get("content_b64")
                try:
                    raw = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
                except (binascii.Error, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=f"文件编码无效：{relative}") from exc
                total += len(raw)
                if len(raw) > 20 * 1024 * 1024 or total > 100 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="上传文件超过大小限制")
                converted = await asyncio.to_thread(convert_knowledge_upload, PurePosixPath(relative).name, raw)
                markdown_relative = str(PurePosixPath(relative).with_suffix(".md"))
                if markdown_relative in seen:
                    raise HTTPException(status_code=400, detail=f"多个文件转换为同一路径：{markdown_relative}")
                seen.add(markdown_relative)
                prepared.append((markdown_relative, converted.markdown))
            directories = {parent}
            destinations: list[tuple[str, str]] = []
            current_tree = await owner._knowledge_tree(str(record.get("account") or ""), root)
            occupied = {str(entry.get("uri") or "") for entry in current_tree.get("entries") or []}
            for relative, content in prepared:
                target = validate_descendant(root, f"{parent}/{relative}", allow_root=False)
                if target in occupied:
                    candidate = PurePosixPath(relative)
                    for suffix_index in range(2, 10_000):
                        renamed = candidate.with_name(f"{candidate.stem}-{suffix_index}{candidate.suffix}")
                        target = validate_descendant(root, f"{parent}/{renamed}", allow_root=False)
                        if target not in occupied:
                            break
                    else:
                        raise HTTPException(status_code=409, detail=f"无法为重名文件生成新名称：{relative}")
                occupied.add(target)
                destinations.append((target, content))
                cursor = target.rsplit("/", 1)[0]
                while cursor == parent or cursor.startswith(f"{parent}/"):
                    directories.add(cursor)
                    if cursor == parent:
                        break
                    cursor = cursor.rsplit("/", 1)[0]
            for directory in sorted(directories, key=lambda item: (item.count("/"), item)):
                try:
                    await owner._knowledge_request(
                        str(record.get("account") or ""),
                        "POST",
                        "/api/v1/fs/mkdir",
                        json={"uri": directory},
                    )
                except HTTPException as exc:
                    if exc.status_code != 409 and "ALREADY_EXISTS" not in str(exc.detail).upper():
                        raise
            written = []
            for target, content in destinations:
                try:
                    result = await owner._knowledge_request(
                        str(record.get("account") or ""),
                        "POST",
                        "/api/v1/content/write",
                        timeout=180.0,
                        json={"uri": target, "content": content, "mode": "create", "wait": False},
                    )
                except HTTPException as exc:
                    if exc.status_code == 409 or "ALREADY_EXISTS" in str(exc.detail).upper():
                        raise HTTPException(
                            status_code=409,
                            detail=f"文件上传时发生重名竞争，请刷新后重试：{target}",
                        ) from exc
                    raise
                written.append({"uri": target, "result": result})
            return JSONResponse(status_code=202, content={"ok": True, "written": written, "file_count": len(written)})

        @app.delete("/api/mining/ov-entry")
        async def mining_ov_entry_delete(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            body = await request.json()
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(repository.get_source, str(body.get("source_id") or ""))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            target = validate_descendant(str(record.get("uri") or ""), body.get("uri"), allow_root=False)
            result = await owner._knowledge_request(
                str(record.get("account") or ""),
                "DELETE",
                "/api/v1/fs",
                timeout=180.0,
                params={"uri": target, "recursive": "true", "wait": "false"},
            )
            return JSONResponse({"deleted": True, "uri": target, "result": result})

        @app.get("/api/mining/jobs")
        async def mining_jobs_list(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            jobs = await asyncio.to_thread(repository.list_jobs)
            refreshed = await asyncio.gather(
                *(owner._refresh_ov_skill_job(job) for job in jobs),
                return_exceptions=True,
            )
            public_jobs = []
            for original, result in zip(jobs, refreshed):
                if isinstance(result, Exception):
                    repository.append_log(original["job_id"], f"刷新任务状态失败：{result}")
                    public_jobs.append(await asyncio.to_thread(repository.public_job, original))
                else:
                    public_jobs.append(result)
            return JSONResponse({"jobs": public_jobs, "summary": repository.summary(public_jobs)})

        @app.post("/api/mining/jobs", status_code=201)
        async def mining_jobs_create(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            body = await request.json()
            source_uri = validate_resource_uri(body.get("input_dir"))
            repository = owner._ov_skill_mining_repository()
            requested_source_id = str(body.get("source_id") or "")
            records = await asyncio.to_thread(repository.list_source_records)
            record = next(
                (
                    item
                    for item in records
                    if (
                        item.get("source_id") == requested_source_id
                        if requested_source_id
                        else item.get("uri") == source_uri
                    )
                ),
                None,
            )
            if record is None:
                raise HTTPException(status_code=400, detail="请先将所选 OpenViking 目录登记为知识源")
            source = await owner._ov_source_detail(record)
            if source.get("status") == "missing":
                raise HTTPException(status_code=409, detail="知识源目录已被删除，请重新选择")
            if not source.get("ready"):
                raise HTTPException(status_code=409, detail="知识源目录中没有可挖掘文件")
            name = str(body.get("name") or f"{source.get('name')} 挖掘任务").strip()
            if len(name) > 120:
                raise HTTPException(status_code=400, detail="任务名称不能超过 120 个字符")
            try:
                job = await asyncio.to_thread(
                    repository.create_job,
                    name=name,
                    account=str(record.get("account") or ""),
                    source_uri=source_uri,
                    source_snapshot_uri=f"{RUNS_ROOT}/pending/source",
                    output_uri=f"{RUNS_ROOT}/pending/output",
                    document_count=int(source.get("document_count") or 0),
                    max_rounds=int(body.get("max_rounds") or 3),
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            run_root = f"{RUNS_ROOT}/{job['job_id']}"
            snapshot_uri = f"{run_root}/source"
            output_uri = f"{run_root}/output"
            job = await asyncio.to_thread(
                repository.update_job,
                job["job_id"],
                source_snapshot=snapshot_uri,
                output_uri=output_uri,
            )
            client = CompileClient(
                endpoint=owner._knowledge_endpoint(),
                account_id=str(record.get("account") or ""),
                user_id=owner._knowledge_team_user(),
                api_key=owner._knowledge_api_key(),
                agent_id="team-skill-evolver",
            )
            try:
                provisioned = await client.publish_shared_skill(
                    skill_name=SKILL_NAME,
                    skill_body=SKILL_BODY,
                    version_message="Provision OV Compile SkillMiner v1",
                )
                if not provisioned.get("ok"):
                    raise ValueError(str(provisioned.get("stderr") or "无法发布 SkillMiner Compile Skill"))
                await client.mkdir(run_root)
                copied = await client.copy_tree(source_uri=source_uri, target_uri=snapshot_uri)
                if not copied.get("ok"):
                    raise ValueError(str(copied.get("stderr") or "无法冻结知识源快照"))
                accepted = await owner._knowledge_request(
                    str(record.get("account") or ""),
                    "POST",
                    "/api/v1/compile",
                    timeout=60.0,
                    extra_headers={"Idempotency-Key": f"te-sm-{job['job_id']}"},
                    json={
                        "from": [snapshot_uri],
                        "to": output_uri,
                        "skill": SKILL_URI,
                        "instruction": (
                            f"任务名称：{name}。最多进行 {job['max_rounds']} 轮内部证据复核；"
                            "只输出契约文件，不发布 Skill。"
                        ),
                    },
                )
                task_id = str((accepted or {}).get("task_id") or "") if isinstance(accepted, dict) else ""
                if not task_id:
                    raise ValueError("OpenViking Compile 未返回 task_id")
                revision = ""
                provisioned_result = provisioned.get("result") if isinstance(provisioned, dict) else None
                if isinstance(provisioned_result, dict):
                    revision = str(provisioned_result.get("revision") or provisioned_result.get("content_sha256") or "")
                job = await asyncio.to_thread(
                    repository.update_job,
                    job["job_id"],
                    status="queued",
                    started_at=utc_now(),
                    ov_task_id=task_id,
                    skill_revision=revision,
                )
                repository.append_log(job["job_id"], f"知识源快照已冻结：{snapshot_uri}")
                repository.append_log(job["job_id"], f"OV Compile 已受理：{task_id}")
            except Exception as exc:
                repository.append_log(job["job_id"], f"启动失败：{exc}")
                await asyncio.to_thread(
                    repository.update_job,
                    job["job_id"],
                    status="failed",
                    error=str(exc),
                    finished_at=utc_now(),
                )
                raise HTTPException(status_code=502, detail=f"启动 OV Compile 挖掘失败：{exc}") from exc
            return JSONResponse(
                status_code=201,
                content={"ok": True, "jobs": [repository.public_job(job)], "summary": repository.summary([job])},
            )

        @app.get("/api/mining/jobs/{job_id}")
        async def mining_job_detail(job_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            try:
                job = await asyncio.to_thread(repository.get_job, job_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="挖掘任务不存在") from exc
            refreshed = await owner._refresh_ov_skill_job(job, detail=True)
            return JSONResponse({"ok": True, "job": refreshed})

        @app.post("/api/mining/jobs/{job_id}/stop")
        async def mining_job_stop(job_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            try:
                job = await asyncio.to_thread(repository.get_job, job_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="挖掘任务不存在") from exc
            if job.get("engine") != "ov_compile":
                return await owner._dispatch_skillminer_request(request)
            if job.get("status") not in ACTIVE_JOB_STATES:
                raise HTTPException(status_code=409, detail="该挖掘任务当前不可停止")
            task_id = str(job.get("ov_task_id") or "")
            if task_id:
                await owner._knowledge_request(
                    str(job.get("account") or ""),
                    "POST",
                    f"/api/v1/tasks/{task_id}/cancel",
                )
            job = await asyncio.to_thread(repository.update_job, job_id, status="stopping", stop_reason="用户手动停止")
            return JSONResponse({"ok": True, "job": repository.public_job(job, detail=True)})

        @app.delete("/api/mining/jobs/{job_id}")
        async def mining_job_delete(job_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            try:
                job = await asyncio.to_thread(repository.get_job, job_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="挖掘任务不存在") from exc
            if job.get("engine") != "ov_compile":
                return await owner._dispatch_skillminer_request(request)
            try:
                result = await asyncio.to_thread(repository.delete_job, job_id)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return JSONResponse(result)

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
