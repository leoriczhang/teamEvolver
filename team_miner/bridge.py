"""Native Skill mining routes and durable OpenViking Compile orchestration."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from teamEvolver.config_store import CONFIG_DIR, ConfigStore

from .artifacts import artifact_job_id, read_artifact, save_artifact
from .document_converter import convert_knowledge_upload
from .ov_compile_mining import (
    ACTIVE_JOB_STATES,
    RUNS_ROOT,
    SKILL_URI,
    SOURCE_TAG_SKILL,
    OvSkillMiningRepository,
    normalize_source_tags,
    source_has_tag,
    utc_now,
    validate_descendant,
    validate_resource_uri,
)
from .ov_round_orchestration import OvRoundOrchestrationMixin, _compiler_package
from .routes import _content_editable, _content_text
from .settings import (
    LEGACY_COMPILER_WARNING,
    compiler_contract_version,
    compiler_prompt,
    pipeline_settings,
    reset_prompt,
    settings_payload,
    update_settings,
)

logger = logging.getLogger(__name__)

# Path to the SkillMiner project root.
_SKILLMINER_ROOT = Path(__file__).resolve().parent


class SkillMinerBridgeMixin(OvRoundOrchestrationMixin):
    """Expose Skill mining inside the main service without a local execution subprocess."""

    def _mining_store(self) -> ConfigStore:
        config_file = str(getattr(getattr(self, "config", None), "_config_file", "") or "").strip()
        return ConfigStore(config_file=Path(config_file)) if config_file else ConfigStore()

    def _ov_skill_compiler_body(self) -> str:
        return compiler_prompt(self._mining_store().load())

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

    @staticmethod
    def _ov_skill_refresh_due(job: dict) -> bool:
        raw = str(job.get("ov_next_retry_at") or "").strip()
        if not raw:
            return True
        try:
            retry_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return retry_at <= datetime.now(timezone.utc)

    def _schedule_ov_skill_job_refresh(self, job: dict, *, force: bool = False) -> None:
        """Start at most one remote refresh per job without delaying API reads."""
        if (job.get("engine") == "ov_compile" and job.get("orchestration_version") == 2
                and job.get("status") in {"preparing", "stopping"} and not job.get("ov_task_id")
                and (force or self._ov_skill_refresh_due(job))):
            self._schedule_ov_skill_round_submission(job)
            return
        if (
            job.get("engine") != "ov_compile"
            or job.get("status") not in ACTIVE_JOB_STATES
            or not job.get("ov_task_id")
            or (not force and not self._ov_skill_refresh_due(job))
        ):
            return
        tasks = getattr(self, "_ov_skill_refresh_tasks", None)
        if tasks is None:
            tasks = {}
            self._ov_skill_refresh_tasks = tasks
        existing = tasks.get(job["job_id"])
        if existing is not None and not existing.done():
            return

        async def refresh() -> None:
            try:
                await self._refresh_ov_skill_job(job)
            except Exception:
                logger.exception("unexpected OV Skill mining refresh failure for %s", job["job_id"])

        create_task = getattr(self, "_safe_create_task", None)
        task = create_task(refresh()) if callable(create_task) else asyncio.create_task(refresh())
        tasks[job["job_id"]] = task

        def forget(completed: asyncio.Task) -> None:
            if tasks.get(job["job_id"]) is completed:
                tasks.pop(job["job_id"], None)

        task.add_done_callback(forget)

    def _start_ov_skill_monitor(self) -> None:
        """Track deadlines and cancellation even when no browser is polling."""
        existing = getattr(self, "_ov_skill_monitor_task", None)
        if existing is not None and not existing.done():
            return

        async def monitor() -> None:
            while True:
                try:
                    jobs = await asyncio.to_thread(self._ov_skill_mining_repository().list_jobs)
                    for job in jobs:
                        self._schedule_ov_skill_job_refresh(job)
                except Exception:
                    logger.exception("OV Skill mining monitor failed")
                await asyncio.sleep(5)

        self._ov_skill_monitor_task = asyncio.create_task(monitor())

    async def _stop_ov_skill_monitor(self) -> None:
        tasks = [getattr(self, "_ov_skill_monitor_task", None)]
        tasks.extend(list(getattr(self, "_ov_skill_refresh_tasks", {}).values()))
        tasks.extend(list(getattr(self, "_ov_skill_submission_tasks", {}).values()))
        tasks = [task for task in tasks if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ov_enforce_deadline(self, repository, job: dict, status: str) -> dict:
        # Observe completion first: a deadline must never discard a completed result.
        if status not in {"accepted", "pending", "queued", "running", "committing", "cancelling"}:
            return job
        elapsed = self._elapsed_seconds(str(job.get("round_started_at") or job.get("started_at") or ""), utc_now())
        expired = elapsed is not None and elapsed >= int(job.get("runtime_timeout_seconds") or 3600)
        reason = str(job.get("ov_cancel_reason") or "")
        if not reason and expired:
            reason = f"达到运行期限 {int(job.get('runtime_timeout_seconds') or 3600)} 秒"
            job = await asyncio.to_thread(
                repository.update_job, job["job_id"], ov_cancel_reason=reason,
                ov_cancel_requested_at=utc_now(), stop_reason=reason, status="stopping",
            )
            await asyncio.to_thread(repository.append_log, job["job_id"], reason + "；请求取消并等待远端终态")
        if not reason or status == "cancelling" or job.get("ov_cancel_sent_at"):
            return job
        try:
            await self._knowledge_request(
                str(job.get("account") or ""), "POST",
                f"/api/v1/tasks/{job['ov_task_id']}/cancel", timeout=10.0,
            )
        except Exception as exc:
            return await asyncio.to_thread(
                repository.update_job, job["job_id"], ov_cancel_error=str(exc)[:1000], status="stopping",
            )
        return await asyncio.to_thread(
            repository.update_job, job["job_id"], ov_cancel_sent_at=utc_now(), ov_cancel_error="", status="stopping",
        )

    @staticmethod
    def _ov_compile_warnings(task: dict) -> list[str]:
        """Keep native partial-output warnings even when every required file exists."""
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
        pipeline = meta.get("pipeline") if isinstance(meta.get("pipeline"), dict) else {}
        warnings = []
        for values in (result.get("warnings"), pipeline.get("errors")):
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            for value in values[:20]:
                if isinstance(value, dict):
                    value = value.get("message") or value.get("error") or json.dumps(value, ensure_ascii=False)
                if isinstance(value, str) and value.strip():
                    warnings.append(value.strip()[:4000])
        return list(dict.fromkeys(warnings))[:20]

    @staticmethod
    def _ov_task_observability(job: dict, task: dict, stage: str, observed_at: str) -> dict:
        """Build a bounded, payload-free timeline from OV task observations."""
        history = [dict(item) for item in job.get("ov_stage_history") or [] if isinstance(item, dict)]
        if stage:
            if history and history[-1].get("stage") == stage:
                history[-1]["last_seen_at"] = observed_at
                history[-1]["observations"] = int(history[-1].get("observations") or 1) + 1
            else:
                history.append(
                    {
                        "stage": stage,
                        "first_seen_at": observed_at,
                        "last_seen_at": observed_at,
                        "observations": 1,
                    }
                )
        history = history[-64:]

        timeline = [dict(item) for item in job.get("ov_event_timeline") or [] if isinstance(item, dict)]
        raw_events = task.get("events") or task.get("execution_events") or []
        if isinstance(raw_events, dict):
            raw_events = raw_events.get("items") or list(raw_events.values())
        for raw in raw_events if isinstance(raw_events, list) else []:
            if not isinstance(raw, dict):
                continue
            label = str(
                raw.get("stage")
                or raw.get("event")
                or raw.get("type")
                or raw.get("name")
                or raw.get("status")
                or ""
            ).strip()
            at = str(
                raw.get("timestamp")
                or raw.get("recorded_at")
                or raw.get("created_at")
                or raw.get("updated_at")
                or raw.get("at")
                or ""
            ).strip()
            status = str(raw.get("status") or "").strip()
            if not label:
                continue
            event = {"label": label, "at": at, "status": status}
            if event not in timeline:
                timeline.append(event)
        return {
            "ov_stage_history": history,
            "ov_event_timeline": timeline[-200:],
            "ov_task_created_at": str(
                task.get("created_at_iso") or task.get("created_at") or job.get("ov_task_created_at") or ""
            ),
            "ov_task_started_at": str(task.get("started_at") or job.get("ov_task_started_at") or ""),
            "ov_task_updated_at": str(
                task.get("updated_at_iso") or task.get("updated_at") or job.get("ov_task_updated_at") or ""
            ),
        }

    @staticmethod
    def _elapsed_seconds(started_at: str, finished_at: str) -> float | None:
        try:
            start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            finish = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if finish.tzinfo is None:
                finish = finish.replace(tzinfo=timezone.utc)
            return round(max(0.0, (finish - start).total_seconds()), 3)
        except (TypeError, ValueError):
            return None

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
            "tags": normalize_source_tags(record.get("tags"), default=(SOURCE_TAG_SKILL,)),
            "default": bool(record.get("default")),
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
        observed_round = job.get("current_round")
        async with self._ov_skill_mining_lock(job["job_id"]):
            job = await asyncio.to_thread(repository.get_job, job["job_id"])
            if (job.get("status") not in ACTIVE_JOB_STATES or job.get("ov_task_id") != task_id
                    or job.get("current_round") != observed_round):
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
            attempt_at = utc_now()
            job = await asyncio.to_thread(
                repository.update_job,
                job["job_id"],
                ov_last_sync_attempt_at=attempt_at,
            )
            previous_error = str(job.get("ov_sync_error") or "")
            try:
                task = await self._knowledge_request(
                    str(job.get("account") or ""),
                    "GET",
                    f"/api/v1/tasks/{task_id}",
                    timeout=5.0,
                    params={"include_events": "true"},
                )
            except Exception as exc:
                failures = int(job.get("ov_sync_failures") or 0) + 1
                delay = min(300, 5 * (2 ** min(failures - 1, 6)))
                detail_text = getattr(exc, "detail", None)
                message = str(detail_text if detail_text is not None else exc)[:1000]
                job = await asyncio.to_thread(
                    repository.update_job,
                    job["job_id"],
                    ov_sync_error=message,
                    ov_sync_failures=failures,
                    ov_next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
                )
                if message != previous_error or failures == 1 or failures & (failures - 1) == 0:
                    repository.append_log(
                        job["job_id"],
                        f"远端状态暂不可用，将在 {delay} 秒后重试（本地任务记录已保留）：{message}",
                    )
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
            status = str((task or {}).get("status") or "").lower() if isinstance(task, dict) else ""
            stage = str((task or {}).get("stage") or status) if isinstance(task, dict) else status
            job = await self._ov_enforce_deadline(repository, job, status)
            observed_at = utc_now()
            previous_stage = str(job.get("ov_stage") or "")
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
            if job.get("ov_cancel_reason") and public_status in {"queued", "running"}:
                public_status = "stopping"
            error = ""
            if public_status == "failed":
                raw_error = (task or {}).get("error") if isinstance(task, dict) else None
                error = raw_error if isinstance(raw_error, str) else json.dumps(raw_error or {}, ensure_ascii=False)
            awaiting_materialization = public_status == "succeeded" and not job.get("materialized")
            round_completion_pending = public_status == "succeeded" and job.get("orchestration_version") == 2
            changes = {
                # A remote completed task can contain partial output. Publish success only
                # after the local artifact contract has accepted the complete bundle.
                "status": "running" if awaiting_materialization or round_completion_pending else public_status,
                "phase": phase,
                "ov_status": status,
                "ov_processing_seconds": (task or {}).get("processing_seconds"),
                "ov_pipeline": (task or {}).get("meta", {}).get("pipeline", {}),
                "remote_warnings": self._ov_compile_warnings(task if isinstance(task, dict) else {}),
                "ov_stage": stage,
                "error": error,
                "ov_last_synced_at": utc_now(),
                "ov_sync_error": "",
                "ov_sync_failures": 0,
                "ov_next_retry_at": "",
                **self._ov_task_observability(job, task if isinstance(task, dict) else {}, stage, observed_at),
            }
            elapsed = self._elapsed_seconds(
                str(job.get("round_started_at") or job.get("started_at") or ""), observed_at,
            )
            if elapsed is not None:
                changes["compile_observed_seconds"] = elapsed
            if (public_status in {"succeeded", "failed", "stopped"}
                    and not awaiting_materialization and not round_completion_pending):
                changes["finished_at"] = str(
                    (task or {}).get("updated_at_iso") or (task or {}).get("updated_at") or utc_now()
                )
            job = await asyncio.to_thread(repository.update_job, job["job_id"], **changes)
            if stage and stage != previous_stage:
                repository.append_log(
                    job["job_id"],
                    f"OpenViking 阶段切换：{previous_stage or '(未上报)'} -> {stage}（累计 {elapsed or 0:.3f} 秒）",
                )
            if job.get("ov_last_sync_attempt_at") and previous_error:
                repository.append_log(job["job_id"], "OpenViking 连接已恢复，任务状态同步成功")
            if round_completion_pending and job.get("materialized"):
                job = await self._complete_ov_skill_round(job)
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
            if public_status == "succeeded" and not job.get("materialized"):
                materialize_started = time.perf_counter()
                materialize_started_at = utc_now()
                job = await asyncio.to_thread(
                    repository.update_job,
                    job["job_id"],
                    materialize_started_at=materialize_started_at,
                    ov_stage="materializing",
                )
                try:
                    job = await self._materialize_ov_skill_job(job)
                    materialize_seconds = round(time.perf_counter() - materialize_started, 3)
                    job = await asyncio.to_thread(
                        repository.update_job,
                        job["job_id"],
                        status="running" if job.get("orchestration_version") == 2 else "succeeded",
                        phase={"step1": "done", "step2": "done", "step3": "done"},
                        finished_at=job.get("finished_at") or utc_now(),
                        materialize_finished_at=utc_now(),
                        materialize_seconds=materialize_seconds,
                    )
                    if job.get("orchestration_version") == 2:
                        job = await self._complete_ov_skill_round(job)
                    repository.append_log(job["job_id"], f"候选产物物化耗时 {materialize_seconds:.3f} 秒")
                except Exception as exc:
                    materialize_seconds = round(time.perf_counter() - materialize_started, 3)
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
                            materialize_seconds=materialize_seconds,
                        )
                        return await asyncio.to_thread(repository.public_job, job, detail=detail)
                    job = await asyncio.to_thread(
                        repository.update_job,
                        job["job_id"],
                        status="failed",
                        error=f"候选产物物化失败：{exc}",
                        finished_at=utc_now(),
                        materialize_attempts=attempts,
                        materialize_seconds=materialize_seconds,
                    )
            return await asyncio.to_thread(repository.public_job, job, detail=detail)

    async def _complete_ov_skill_round(self, job: dict) -> dict:
        repository = self._ov_skill_mining_repository()
        job = await asyncio.to_thread(repository.finish_materialized_round, job["job_id"])
        if (not job.get("human_checkpoints_effective") and job.get("status") not in {"succeeded", "stopped", "failed"}
                and int(job["current_round"]) < int(job["max_rounds"])):
            job = await asyncio.to_thread(repository.prepare_automatic_next_round, job["job_id"])
            self._schedule_ov_skill_round_submission(job)
        return job

    # ------------------------------------------------------------------ #
    # Enablement / lifecycle                                             #
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    # Reverse proxy                                                       #
    # ------------------------------------------------------------------ #


    def _register_skillminer_routes(self, app: FastAPI) -> None:
        """Register the current OV mining API and its local artifact workspace."""
        owner = self

        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def mining_lifespan(application):
            async with original_lifespan(application) as state:
                owner._start_ov_skill_monitor()
                try:
                    yield state
                finally:
                    await owner._stop_ov_skill_monitor()

        app.router.lifespan_context = mining_lifespan

        def _store() -> ConfigStore:
            return owner._mining_store()

        def _require_admin(request: Request) -> Optional[JSONResponse]:
            user = getattr(request.state, "console_user", None)
            if not isinstance(user, dict) or str(user.get("role") or "") != "admin":
                return JSONResponse(
                    status_code=403,
                    content={"detail": "only admin users can edit mining settings"},
                )
            return None

        @app.get("/api/mining/config")
        async def mining_config_get():
            settings = pipeline_settings(_store().load())
            return JSONResponse({
                "engine": "ov_compile",
                "max_rounds_default": settings["max_rounds"], "max_rounds_range": [1, 5],
                "runtime_timeout_seconds_default": settings["runtime_timeout_seconds"],
                "runtime_timeout_seconds_range": [60, 7200],
                "rounds_mode": "orchestrated", "human_checkpoints_supported": True,
                "compile_model": {"configuration_source": "openviking_server", "editable_here": False},
            })

        @app.get("/api/mining/artifacts/content")
        async def mining_artifact_get(request: Request, path: str = Query(...)):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            try:
                result = await asyncio.to_thread(read_artifact, owner._ov_skill_mining_repository().project_root, path)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(result)

        @app.post("/api/mining/artifacts/content")
        async def mining_artifact_save(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            if len(await request.body()) > 3 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="产物编辑请求超过 3 MB")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="产物编辑请求必须为对象")
            path = str(body.get("path") or "")
            try:
                job_id = artifact_job_id(path)
                async with owner._ov_skill_mining_lock(job_id):
                    repository = owner._ov_skill_mining_repository()
                    result = await asyncio.to_thread(save_artifact, repository.project_root, path, body.get("content"))
                    await asyncio.to_thread(repository.refresh_edited_quality, job_id, path)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(result)

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
            return JSONResponse(content=payload)

        @app.get("/api/mining/ov-sources")
        async def mining_ov_sources(request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            repository = owner._ov_skill_mining_repository()
            accounts = await asyncio.to_thread(owner._knowledge_accounts)
            account_names = [str(item) for item in accounts.get("accounts") or [] if str(item)]
            current_account = str(accounts.get("current") or "")
            if current_account and current_account not in account_names:
                account_names.append(current_account)
            await asyncio.gather(
                *(asyncio.to_thread(repository.ensure_default_knowledge_source, item) for item in account_names)
            )
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
                        "tags": normalize_source_tags(
                            record.get("tags"),
                            default=(SOURCE_TAG_SKILL,),
                        ),
                        "default": bool(record.get("default")),
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
                    "accounts": account_names,
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
                tags=body.get("tags"),
                replace_tags=True,
            )
            source = await owner._ov_source_detail(record)
            return JSONResponse(status_code=201, content={"ok": True, "source": source})

        @app.patch("/api/mining/ov-sources/{source_id}")
        async def mining_ov_source_update(source_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            body = await request.json()
            repository = owner._ov_skill_mining_repository()
            try:
                record = await asyncio.to_thread(
                    repository.update_source,
                    source_id,
                    tags=body.get("tags"),
                    name=body.get("name"),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="知识源引用不存在") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            source = await owner._ov_source_detail(record)
            return JSONResponse({"ok": True, "source": source})

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
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
            for job in jobs:
                owner._schedule_ov_skill_job_refresh(job)
            public_jobs = [repository.public_job(job) for job in jobs]
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
            if not source_has_tag(record, SOURCE_TAG_SKILL):
                raise HTTPException(status_code=409, detail="所选目录没有“Skill 知识源”标签")
            source = await owner._ov_source_detail(record)
            if source.get("status") == "missing":
                raise HTTPException(status_code=409, detail="知识源目录已被删除，请重新选择")
            if not source.get("ready"):
                raise HTTPException(status_code=409, detail="知识源目录中没有可挖掘文件")
            name = str(body.get("name") or f"{source.get('name')} 挖掘任务").strip()
            if len(name) > 120:
                raise HTTPException(status_code=400, detail="任务名称不能超过 120 个字符")
            try:
                defaults = pipeline_settings(_store().load())
                rounds_hint = int(body.get("max_rounds", defaults["max_rounds"]))
                if not 1 <= rounds_hint <= 5:
                    raise ValueError("最大挖掘轮数必须在 1 到 5 之间")
                timeout_seconds = int(body.get("runtime_timeout_seconds", defaults["runtime_timeout_seconds"]))
                if not 60 <= timeout_seconds <= 7200:
                    raise ValueError("运行期限必须在 60 到 7200 秒之间")
                compiler_body = await asyncio.to_thread(owner._ov_skill_compiler_body)
                await asyncio.to_thread(_compiler_package, compiler_body)
                contract_version = compiler_contract_version(compiler_body)
                job = await asyncio.to_thread(
                    repository.create_job,
                    name=name,
                    account=str(record.get("account") or ""),
                    source_uri=source_uri,
                    source_snapshot_uri=f"{RUNS_ROOT}/pending/source",
                    output_uri=f"{RUNS_ROOT}/pending/output",
                    document_count=int(source.get("document_count") or 0),
                    max_rounds=rounds_hint,
                    runtime_timeout_seconds=timeout_seconds,
                    human_checkpoints=bool(body.get("human_checkpoints", True)),
                    candidate_contract_version=contract_version,
                    compiler_body=compiler_body,
                )
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            run_root = f"{RUNS_ROOT}/{job['job_id']}"
            snapshot_uri = f"{run_root}/rounds/round-1/source"
            job = await asyncio.to_thread(
                repository.update_job, job["job_id"], source_snapshot=snapshot_uri,
                original_source_snapshot=snapshot_uri, output_uri=f"{run_root}/rounds/round-1/output",
                options_warnings=[LEGACY_COMPILER_WARNING] if contract_version == 1 else [],
            )
            owner._start_ov_skill_monitor()
            owner._schedule_ov_skill_round_submission(job)
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
            owner._schedule_ov_skill_job_refresh(job)
            public_job = await asyncio.to_thread(repository.public_job, job, detail=True)
            return JSONResponse({"ok": True, "job": public_job})

        @app.post("/api/mining/jobs/{job_id}/supplements")
        async def mining_round_supplements(job_id: str, request: Request):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            if len(await request.body()) > 30 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="补充文件请求超过 30 MB")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="补证请求必须为对象")
            repository = owner._ov_skill_mining_repository()
            try:
                async with owner._ov_skill_mining_lock(job_id):
                    job = await asyncio.to_thread(repository.get_job, job_id)
                    job = await owner._stage_round_supplements(
                        job, str(body.get("checkpoint_id") or ""), body.get("files"),
                    )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="挖掘任务不存在") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            public = await asyncio.to_thread(repository.public_job, job, detail=True)
            return JSONResponse({"ok": True, "job": public,
                                 "saved": (public.get("pending_checkpoint") or {}).get("attachments") or []})

        async def mining_round_decision(job_id: str, request: Request, *, finish: bool):
            denied = _require_admin(request)
            if denied is not None:
                return denied
            if len(await request.body()) > 1024 * 1024:
                raise HTTPException(status_code=413, detail="补充 Evidence 文本请求超过 1 MB")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="补证请求必须为对象")
            repository = owner._ov_skill_mining_repository()
            user = request.state.console_user
            submitted_by = str(user.get("username") or user.get("user_id") or user.get("id") or "admin")
            fields = {
                "answers": body.get("answers", {}), "unresolved": body.get("unresolved", []),
                "references": body.get("references", {}), "feedback": body.get("feedback", ""),
                "request_id": body.get("request_id", ""), "submitted_by": submitted_by,
            }
            try:
                async with owner._ov_skill_mining_lock(job_id):
                    job = await asyncio.to_thread(repository.get_job, job_id)
                    if job.get("status") == "waiting_human":
                        validated = await asyncio.to_thread(
                            repository._validate_submission, job, str(body.get("checkpoint_id") or ""),
                            fields["answers"], fields["unresolved"], fields["references"], fields["feedback"],
                            fields["request_id"], submitted_by, None,
                        )
                        job = await owner._stage_round_references(job, validated["references"])
                        await asyncio.to_thread(repository.finalize_materialized, job_id)
                    operation = repository.finish_job if finish else repository.prepare_next_round
                    job = await asyncio.to_thread(operation, job_id, str(body.get("checkpoint_id") or ""), **fields)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="挖掘任务不存在") from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not finish and job.get("status") == "preparing":
                owner._schedule_ov_skill_round_submission(job)
            public = await asyncio.to_thread(repository.public_job, job, detail=True)
            return JSONResponse({"ok": True, "job": public})

        @app.post("/api/mining/jobs/{job_id}/continue")
        async def mining_round_continue(job_id: str, request: Request):
            return await mining_round_decision(job_id, request, finish=False)

        @app.post("/api/mining/jobs/{job_id}/finish")
        async def mining_round_finish(job_id: str, request: Request):
            return await mining_round_decision(job_id, request, finish=True)

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
                raise HTTPException(status_code=400, detail="任务不是 OpenViking 挖掘任务")
            async with owner._ov_skill_mining_lock(job_id):
                job = await asyncio.to_thread(repository.get_job, job_id)
                if job.get("status") == "waiting_human":
                    job = await asyncio.to_thread(repository.stop_waiting, job_id)
                    return JSONResponse({"ok": True, "job": repository.public_job(job, detail=True)})
                if job.get("status") == "preparing" and not job.get("ov_task_id"):
                    if not job.get("compile_request"):
                        job = await asyncio.to_thread(repository.update_job, job_id, status="stopped",
                                                      finished_at=utc_now(), stop_reason="用户手动停止")
                    else:
                        job = await asyncio.to_thread(repository.update_job, job_id, status="stopping",
                                                      stop_reason="用户手动停止；正在确认远端受理结果",
                                                      ov_cancel_reason="用户手动停止", ov_cancel_requested_at=utc_now())
                        owner._schedule_ov_skill_round_submission(job)
                    return JSONResponse({"ok": True, "job": repository.public_job(job, detail=True)})
            if job.get("status") not in ACTIVE_JOB_STATES:
                raise HTTPException(status_code=409, detail="该挖掘任务当前不可停止")
            job = await asyncio.to_thread(
                repository.update_job, job_id, status="stopping",
                stop_reason=job.get("stop_reason") or "用户手动停止",
                ov_cancel_reason=job.get("ov_cancel_reason") or "用户手动停止",
                ov_cancel_requested_at=job.get("ov_cancel_requested_at") or utc_now(),
            )
            owner._start_ov_skill_monitor()
            owner._schedule_ov_skill_job_refresh(job, force=True)
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
                raise HTTPException(status_code=400, detail="任务不是 OpenViking 挖掘任务")
            try:
                result = await asyncio.to_thread(repository.delete_job, job_id)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return JSONResponse(result)
