"""Preserved remote task polling, cancellation, deadlines and completion policy."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from .content_io import _content_text
from .ov_compile_mining import ACTIVE_JOB_STATES, utc_now, validate_descendant, validate_resource_uri


class MiningPollingMixin:
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


    async def _reconcile_unconfirmed_ov_skill_job(self, observed: dict) -> None:
        """Read-only reconciliation after local cancellation waiting has ended."""
        repository = self._ov_skill_mining_repository()
        async with self._ov_skill_mining_lock(observed["job_id"]):
            job = await asyncio.to_thread(repository.get_job, observed["job_id"])
            if (not job.get("remote_terminal_unconfirmed") or job.get("status") != "interrupted"
                    or job.get("ov_task_id") != observed.get("ov_task_id")):
                return
            observed_at = utc_now()
            changes = {"ov_last_sync_attempt_at": observed_at,
                       "ov_next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}
            try:
                task = await self._knowledge_request(
                    str(job.get("account") or ""), "GET", f"/api/v1/tasks/{job['ov_task_id']}", timeout=5.0,
                    params={"include_events": "true"},
                )
                if task.get("task_id", job["ov_task_id"]) != job["ov_task_id"]:
                    raise ValueError("远端状态不属于当前任务")
                status = str(task.get("status") or "").lower()
                changes.update(ov_status=status, ov_sync_error="", ov_last_synced_at=observed_at)
                if status in {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}:
                    await self._release_staged_compile_slot(job, job["ov_task_id"])
                    changes.update(remote_terminal_unconfirmed=False, remote_reconciled_at=observed_at,
                                   status="interrupted", error="本地已中断等待；远端终态现已确认：" + status,
                                   remote_state_warning="本地已中断等待；远端终态现已确认：" + status)
            except Exception as exc:
                changes["ov_sync_error"] = str(getattr(exc, "detail", exc))[:1000]
            await asyncio.to_thread(repository.update_job, job["job_id"], **changes)


    async def _ov_enforce_deadline(self, repository, job: dict, status: str) -> dict:
        # Observe completion first: a deadline must never discard a completed result.
        if status not in {"accepted", "pending", "queued", "running", "committing", "cancelling"}:
            return job
        from .native_compiler import is_native
        if is_native(job):
            from .native_recovery import effective_round_budget
            budget_start, timeout = effective_round_budget(job)
        else:
            budget_start = str(job.get("round_started_at") or job.get("started_at") or "")
            timeout = int(job.get("round_timeout_seconds") or job.get("runtime_timeout_seconds") or 3600)
        elapsed = self._elapsed_seconds(budget_start, utc_now())
        expired = elapsed is not None and elapsed >= timeout
        reason = str(job.get("ov_cancel_reason") or "")
        if not reason and expired:
            reason = f"达到本轮运行期限 {timeout} 秒"
            job = await asyncio.to_thread(
                repository.update_job, job["job_id"], ov_cancel_reason=reason,
                ov_cancel_requested_at=utc_now(), stop_reason=reason, status="stopping",
            )
            await asyncio.to_thread(repository.append_log, job["job_id"], reason + "；请求取消并等待远端终态")
        # Cancellation acceptance is not a terminal acknowledgement. Bound the local
        # wait without claiming that remote work stopped or releasing its reservation.
        sent = str(job.get("ov_cancel_sent_at") or "")
        cancel_elapsed = self._elapsed_seconds(sent, utc_now()) if sent else None
        if reason and cancel_elapsed is not None and cancel_elapsed >= 120:
            message = "停止请求已受理超过120秒，远端终态未确认；保留远端原始状态，不视为已取消或已完成"
            job = await asyncio.to_thread(
                repository.update_job, job["job_id"], status="interrupted", finished_at=utc_now(),
                ov_status=status, remote_terminal_unconfirmed=True, remote_state_warning=message,
                ov_next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                stop_reason=reason, error=message,
                phase={"step1": "done", "step2": "interrupted", "step3": "idle"},
            )
            await asyncio.to_thread(repository.append_log, job["job_id"], message)
            return job
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


    async def _materialize_ov_skill_job(self, job: dict) -> dict:
        from .native_compiler import is_native
        if is_native(job):
            return await self._materialize_native_round(job)
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
            if status in {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}:
                await self._release_staged_compile_slot(job, task_id)
            job = await self._ov_enforce_deadline(repository, job, status)
            if job.get("remote_terminal_unconfirmed") and job.get("status") == "interrupted":
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
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
            staged_failure_pending = (
                job.get("staged_protocol") == "te-staged-compile.v1" and public_status == "failed"
            )
            changes = {
                # A remote completed task can contain partial output. Publish success only
                # after the local artifact contract has accepted the complete bundle.
                "status": (
                    "running"
                    if awaiting_materialization or round_completion_pending or staged_failure_pending
                    else public_status
                ),
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
                    and not awaiting_materialization and not round_completion_pending and not staged_failure_pending):
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
            from .mining_completion import completion_only
            from .native_compiler import is_native
            errors = ((task.get("meta") or {}).get("pipeline") or {}).get("errors") or []
            if (is_native(job) and not job.get("ov_cancel_reason")
                    and (public_status == "failed" or (
                        public_status == "succeeded" and errors and not completion_only(job)))):
                if public_status == "succeeded":
                    error = "远端完成状态仍包含未成功的内部任务，不能按完整来源验收"
                diagnosis = error + ("；" + json.dumps(errors, ensure_ascii=False) if errors else "")
                job = await self._repair_native_round(job, diagnosis, task=task)
                if job["status"] == "preparing":
                    self._schedule_ov_skill_round_submission(job)
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
            if (job.get("staged_protocol") == "te-staged-compile.v1"
                    and status in {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}):
                job = await self._advance_staged_round(job, task)
                return await asyncio.to_thread(repository.public_job, job, detail=detail)
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
                    if completion_only(job):
                        job = await asyncio.to_thread(repository.update_job, job["job_id"],
                            materialize_warnings=[*job.get("materialize_warnings", []),
                                                  f"远端挖掘已完成，本地文件同步未完成：{exc}"],
                            materialize_seconds=materialize_seconds)
                        job = await asyncio.to_thread(repository.finalize_materialized, job["job_id"])
                        job = await self._complete_ov_skill_round(job)
                        return await asyncio.to_thread(repository.public_job, job, detail=detail)
                    from .native_round import NativeArtifactError
                    if isinstance(exc, NativeArtifactError):
                        job = await self._repair_native_round(job, str(exc), task=task, local_validation=True)
                        if job["status"] == "preparing":
                            self._schedule_ov_skill_round_submission(job)
                        return await asyncio.to_thread(repository.public_job, job, detail=detail)
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
        if (job.get("status") not in {"succeeded", "stopped", "failed", "waiting_human"}
                and int(job["current_round"]) < int(job["max_rounds"])):
            job = await asyncio.to_thread(repository.prepare_automatic_next_round, job["job_id"])
            self._schedule_ov_skill_round_submission(job)
        return job
