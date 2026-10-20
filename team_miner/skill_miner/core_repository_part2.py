# ruff: noqa: F401
from __future__ import annotations

from .mining_contracts import (
    ACTIVE_JOB_STATES,
    DEFAULT_KNOWLEDGE_SOURCE_URI,
    LEGACY_SKILL_BODY,
    MAX_CANDIDATES,
    MAX_PARALLEL_JOBS,
    MAX_REVIEW_QUESTIONS,
    MAX_SKILL_FRONTMATTER_CHARACTERS,
    MAX_SUPPLEMENT_CHARACTERS,
    OUTPUT_LANGUAGE_GUIDANCE,
    PLAN_SCHEMA_VERSION,
    RECOVERABLE_IDENTITY_ERROR,
    REVIEW_SCHEMA_VERSION,
    RUNS_ROOT,
    SKILL_BODY,
    SKILL_NAME,
    SKILL_URI,
    SOURCE_TAG_KNOWLEDGE,
    SOURCE_TAG_SKILL,
    SOURCE_TAGS,
    TERMINAL_JOB_STATES,
    Any,
    Iterable,
    Path,
    PurePosixPath,
    _atomic_json,
    _file_sha256,
    _safe_slug,
    _skill_metadata,
    benchmark_format,
    compiler_execution_guidance,
    dataset_markdown,
    datetime,
    document_skill_ids,
    hashlib,
    json,
    mining_policy_block,
    multi_compiler_execution_guidance,
    normalize_source_tags,
    os,
    parse_skill_md_text,
    re,
    register_owned_compiler_policy,
    shutil,
    source_has_tag,
    source_id,
    threading,
    timezone,
    utc_now,
    uuid,
    validate_descendant,
    validate_resource_uri,
)


class RepositoryPart2Mixin:
    def _artifact_rows(self, workspace: Path) -> list[dict[str, Any]]:
        kinds = {
            "SKILL.md": "skill",
            "EVALUATION.md": "evaluation",
            "BENCHMARK.md": "benchmark",
            "benchmark.json": "benchmark",
        }
        result: list[dict[str, Any]] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace)
            parts = relative.parts
            is_reference = len(parts) >= 4 and parts[0] == "compiled_skill" and parts[2] == "references"
            if is_reference:
                if (
                    path.suffix.lower() not in {".md", ".json"}
                    or any(part.startswith(".") for part in parts)
                    or path.is_symlink()
                    or any((workspace / parent).is_symlink() for parent in relative.parents)
                ):
                    continue
                kind = "resource"
            else:
                kind = kinds.get(path.name)
            if kind is None and "semantic_reports" in path.parts:
                kind = "semantic"
            if kind is not None:
                display_title = ""
                if kind == "skill" and not path.is_symlink() and path.resolve().is_relative_to(workspace.resolve()):
                    from team_skills.library.frontmatter import split_skill_md

                    with path.open(encoding="utf-8", errors="replace") as stream:
                        content = stream.read(16384)
                    split = split_skill_md(content)
                    body = split[1] if split else content
                    first_line = body.strip().splitlines()[0] if body.strip() else ""
                    if first_line.startswith("# "):
                        display_title = first_line[2:].strip().rstrip("#").strip()[:200]
                result.append(
                    {
                        "name": path.name,
                        "kind": kind,
                        "path": path.relative_to(self.project_root).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "skill_name": parts[1] if len(parts) >= 3 and parts[0] == "compiled_skill" else "",
                        "display_title": display_title,
                    }
                )
        return result


    def public_job(self, job: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        hidden = {
            "workspace", "input_manifest", "compile_request", "workspace_reset_pending", "compiler_path",
            "staged_state_path", "native_allowed_source_uris",
            "compiler_body_snapshot", "config_snapshot", "creation_request_sha256",
        }
        result = {key: value for key, value in job.items() if key not in hidden and not key.startswith("_")}
        from .mining_completion import revision_checkpoint
        result["optional_revision"] = revision_checkpoint(job)
        from .artifact_recovery import ERROR_PREFIX, prepare_recovery

        result["can_resume"] = job.get("status") == "failed" and (
            job.get("error") == RECOVERABLE_IDENTITY_ERROR
        )
        result["can_retry_native"] = False
        result["native_retry_context"] = None
        from .native_compiler import is_native
        if is_native(job) and job.get("status") == "failed":
            from .native_recovery import prepare_recovery as prepare_native_recovery
            try:
                prepare_native_recovery(self._job_dir(job["job_id"]), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            from .native_recovery import prepare_explicit_retry
            try:
                result["native_retry_context"] = prepare_explicit_retry(self._job_dir(job["job_id"]), job)
                result["can_retry_native"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if job.get("status") == "failed" and str(job.get("error", "")).startswith(ERROR_PREFIX):
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        from .extract_recovery import ERROR_PREFIX as EXTRACT_ERROR_PREFIX
        from .extract_recovery import prepare_recovery as prepare_extract_recovery
        from .intent_recovery import ERROR as INTENT_ERROR
        from .intent_recovery import prepare_recovery as prepare_intent_recovery
        from .routing_recovery import ERROR as ROUTING_ERROR
        from .routing_recovery import prepare_recovery as prepare_routing_recovery

        if job.get("status") == "failed" and str(job.get("error", "")).startswith(EXTRACT_ERROR_PREFIX):
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_extract_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if job.get("status") == "failed" and job.get("error") == ROUTING_ERROR:
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_routing_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if job.get("status") == "failed" and job.get("error") == INTENT_ERROR:
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_intent_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        for key in ("pending_attachments", "pending_reference_snapshots"):
            result[key] = [self._public_attachment(row) for row in job.get(key) or []]
        for key in ("checkpoint_history", "supplements"):
            result[key] = [self._public_submission(row) for row in job.get(key) or []]
        result["last_checkpoint_submission"] = self._public_submission(job.get("last_checkpoint_submission") or {})
        result["pending_checkpoint"] = self._public_checkpoint(job)
        result["remote_state_stale"] = bool(
            job.get("engine") == "ov_compile" and job.get("status") in ACTIVE_JOB_STATES and job.get("ov_sync_error")
        )
        if not detail:
            return result
        workspace = self.workspace(job)
        artifacts = self._artifact_rows(workspace)
        log_path = self._job_dir(job["job_id"]) / "run.log"
        logs = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] if log_path.is_file() else []
        skills = []
        for skill_file in sorted((workspace / "compiled_skill").glob("*/SKILL.md")):
            skill_dir = skill_file.parent
            document, _ = benchmark_format.read_document(skill_dir / "benchmark.json")
            skills.append(
                {
                    "name": skill_dir.name,
                    "has_skill": True,
                    "has_evaluation": (skill_dir / "EVALUATION.md").is_file(),
                    "has_benchmark": (skill_dir / "benchmark.json").is_file(),
                    "question_count": len((document or {}).get("cases") or []),
                }
            )
        result.update(
            {
                "artifacts": artifacts,
                "rounds": self._public_rounds(job),
                "logs": logs,
                "skills": skills,
                "pending_checkpoint": self._public_checkpoint(job),
                "knowledge_gaps": self._public_gaps(job),
                "knowledge_supplements": [self._public_submission(row) for row in job.get("checkpoint_history") or []],
            }
        )
        return result


    def resume_native_failure(self, job_id: str, remote_task: dict, *,
                              authorization: dict | None = None) -> dict[str, Any]:
        """Apply a bounded request-contract upgrade; accepted requests and retry counts stay immutable."""
        from .native_recovery import explicit_retry_matches, prepare_explicit_retry, prepare_recovery

        with self._lock:
            job = self.get_job(job_id)
            if authorization is not None and explicit_retry_matches(job, authorization):
                return job
            if (authorization is None and (job.get("native_resource_recovery") or job.get("native_contract_recoveries"))
                    and job.get("status") in ACTIVE_JOB_STATES):
                return job
            active = sum(row.get("status") in ACTIVE_JOB_STATES for row in self._iter_jobs_unlocked())
            if active >= MAX_PARALLEL_JOBS:
                raise RuntimeError("运行并发已满，请稍后恢复")
            journal = (prepare_recovery(self._job_dir(job_id), job, apply=True, remote_task=remote_task)
                       if authorization is None else prepare_explicit_retry(self._job_dir(job_id), job,
                           apply=True, remote_task=remote_task, authorization=authorization))
            now = utc_now()
            upgrades = list(job.get("native_contract_recoveries") or [])
            if authorization is None:
                upgrades.append({key: journal[key] for key in ("protocol", "submission_key", "round", "task_id",
                                                              "guidance_sha256", "upgrade_index")})
            meta = journal if authorization is None else journal["retry"]
            reason = (journal["reason"] if authorization is None
                      else "管理员明确授权一次格式修复实测，追加独立尝试预算")
            history = list(job.get("recovery_history") or [])
            history.append({"round": job["current_round"], "error": job["error"],
                            "failed_at": job["finished_at"], "resumed_at": now,
                            "reason": reason, "recovery_protocol": journal["protocol"],
                            "guidance_sha256": meta["guidance_sha256"],
                            **({"explicit_retry": meta} if authorization is not None else {}),
                            "previous_round_started_at": job.get("round_started_at"),
                            "previous_task_id": job["ov_task_id"]})
            recovered = self.update_job(job_id, status="preparing", error="", finished_at="",
                native_contract_recoveries=upgrades, recovery_history=history, native_repair_instruction="",
                **({"native_explicit_retry": meta} if authorization is not None else {}),
                compile_request={}, ov_task_id="", ov_status="", ov_stage="", ov_pipeline={}, materialized=False,
                submission_idempotency_key=journal["submission_key"], submission_started_at="",
                ov_task_created_at="", ov_task_started_at="", ov_task_updated_at="", ov_processing_seconds=None,
                ov_sync_error="", ov_sync_failures=0, ov_next_retry_at="", materialize_attempts=0,
                round_started_at=(job.get("round_started_at") if authorization is not None
                                  else journal["budget_started_at"]))
            self.append_log(job_id, "已核验远端终态、冻结来源及历史产物；"
                            + ("管理员授权本次人工重试，原预算保留，追加独立尝试预算" if authorization is not None
                               else "保留原编译 Skill、旧修订次数和已有恢复预算，以新版请求契约恢复"))
            return recovered


    def resume_identity_failure(self, job_id: str) -> dict[str, Any]:
        """Recover the identified host error at its durable boundary, preserving frozen requests."""
        with self._lock:
            job = self.get_job(job_id)
            if job.get("status") == "preparing" and (job.get("recovery_history") or []):
                return job
            from .artifact_recovery import ERROR_PREFIX, prepare_recovery
            from .extract_recovery import ERROR_PREFIX as EXTRACT_ERROR_PREFIX
            from .extract_recovery import prepare_recovery as prepare_extract_recovery
            from .intent_recovery import ERROR as INTENT_ERROR
            from .intent_recovery import prepare_recovery as prepare_intent_recovery
            from .routing_recovery import ERROR as ROUTING_ERROR
            from .routing_recovery import prepare_recovery as prepare_routing_recovery

            capacity = str(job.get("error", "")).startswith(ERROR_PREFIX)
            extraction = str(job.get("error", "")).startswith(EXTRACT_ERROR_PREFIX)
            routing = job.get("error") == ROUTING_ERROR
            intent = job.get("error") == INTENT_ERROR
            if job.get("status") != "failed" or (job.get("error") != RECOVERABLE_IDENTITY_ERROR
                                                 and not capacity and not extraction and not routing and not intent):
                raise ValueError("此任务不属于已修复的可恢复失败")
            if job.get("ov_cancel_reason"):
                raise ValueError("已请求停止的任务不能自动恢复")
            path = self._job_dir(job_id) / "round_inputs" / f"round-{job['current_round']}" / "staged/state.json"
            if path.is_symlink():
                raise ValueError("恢复状态必须来自当前任务的普通冻结文件")
            state = json.loads(path.read_text(encoding="utf-8"))
            if not capacity and not extraction and not routing and not intent and (
                    state.get("round") != job["current_round"]
                    or state.get("phase") != "identity_group"
                    or state.get("current") is not None or state.get("complete")):
                raise ValueError("任务不在可恢复的候选身份复核边界")
            active = sum(row.get("status") in ACTIVE_JOB_STATES for row in self._iter_jobs_unlocked())
            if active >= MAX_PARALLEL_JOBS:
                raise RuntimeError("运行并发已满，请稍后恢复")
            journal = prepare_recovery(path.parent, state, job, apply=True) if capacity else None
            if extraction:
                journal = prepare_extract_recovery(path.parent, state, job, apply=True)
            if routing:
                journal = prepare_routing_recovery(path.parent, state, job, apply=True)
            if intent:
                journal = prepare_intent_recovery(path.parent, state, job, apply=True)
            history = list(job.get("recovery_history") or [])
            history.append({"round": job["current_round"], "error": job["error"], "failed_at": job["finished_at"],
                            "resumed_at": utc_now(),
                            "reason": journal["reason"] if journal else "候选名称冲突已修复，从身份复核断点继续",
                            **({"recovery_protocol": journal["protocol"]} if journal else {})})
            recovered = self.update_job(job_id, status="preparing", error="", finished_at="", ov_task_id="",
                                        ov_status="", recovery_history=history)
            self.append_log(job_id, "从已修复失败断点恢复：保留原始来源、已受理请求、产物及未解决问题，从当前轮继续")
            return recovered


    def summary(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total": len(jobs),
            "running": sum(job.get("status") in {"running", "stopping"} for job in jobs),
            "queued": sum(job.get("status") in {"preparing", "queued"} for job in jobs),
            "completed": sum(job.get("status") == "succeeded" for job in jobs),
            "failed": sum(job.get("status") in {"failed", "interrupted", "stopped"} for job in jobs),
            "waiting": sum(job.get("status") == "waiting_human" for job in jobs),
            "max_parallel": MAX_PARALLEL_JOBS,
        }
