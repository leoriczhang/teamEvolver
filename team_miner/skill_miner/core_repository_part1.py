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


class RepositoryPart1Mixin:
    def _read_review(self, job: dict[str, Any]) -> dict[str, Any]:
        path = self.workspace(job) / "semantic_reports" / "review.json"
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("OV Compile 未生成合法的 analysis/review.json") from exc
        if not isinstance(review, dict) or review.get("schema_version") != REVIEW_SCHEMA_VERSION:
            raise ValueError("analysis/review.json schema_version 必须为 skill-miner.review.v1")
        questions = review.get("questions")
        if not isinstance(questions, list) or len(questions) > MAX_REVIEW_QUESTIONS:
            raise ValueError("analysis/review.json questions 必须是最多 50 项的数组")
        seen: set[str] = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError("复核问题格式无效")
            qid = question.get("qid")
            if not isinstance(qid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", qid) or qid in seen:
                raise ValueError("复核问题 qid 必须是唯一的有效标识")
            seen.add(qid)
            for field, limit in (("question", 4000), ("reason", 4000)):
                value = question.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError(f"复核问题 {field} 必须是非空文本且不超过 {limit} 字符")
            if not isinstance(question.get("required"), bool):
                raise ValueError("复核问题 required 必须为布尔值")
            refs = question.get("source_refs")
            if not isinstance(refs, list) or not refs or len(refs) > 50:
                raise ValueError("复核问题 source_refs 必须包含可追溯的知识源 Viking URI")
            for uri in refs:
                if not isinstance(uri, str):
                    raise ValueError("复核问题 source_refs 必须是 Viking URI")
                normalized = validate_resource_uri(uri)
                if not any(
                    normalized == root or normalized.startswith(root + "/") for root in self._citation_prefixes(job)
                ):
                    raise ValueError("复核问题引用不属于本任务知识源")
        return review


    @staticmethod
    def _question_row(question: dict[str, Any]) -> dict[str, Any]:
        refs = list(question.get("source_refs") or [])
        return {
            **question,
            "dimension": "evidence",
            "severity": "required" if question.get("required") else "optional",
            "context": str(question.get("reason") or ""),
            "source": "\n".join(refs),
            "evidence_hint": "\n".join(refs),
            "suggested_type": "long_text",
            "answer_type": "long_text",
        }


    def _archive_round(self, job: dict[str, Any]) -> dict[str, Any]:
        round_number = int(job.get("current_round") or 1)
        destination = self._job_dir(job["job_id"]) / "rounds" / f"round-{round_number}"
        if not destination.is_dir():
            temporary = destination.with_name(destination.name + ".tmp")
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir(parents=True)
            shutil.copytree(self.workspace(job), temporary / "output")
            input_manifest = Path(str(job.get("input_manifest") or ""))
            if not input_manifest.is_absolute():
                input_manifest = self.project_root / input_manifest
            job_dir = self._job_dir(job["job_id"])
            if input_manifest.is_file() and job_dir in input_manifest.resolve().parents:
                # The bridge freezes text files alongside the input manifest.
                # Copy that round's full inputs so its archive is self-contained.
                shutil.copytree(input_manifest.parent, temporary / "input")
            manifest = {
                "round": round_number,
                "source_uri": job.get("source_uri"),
                "source_snapshot": job.get("source_snapshot"),
                "original_source_snapshot": job.get("original_source_snapshot"),
                "round_context_uri": job.get("round_context_uri"),
                "input_manifest": job.get("input_manifest"),
                "compile_request": job.get("compile_request"),
                "output_language_policy": job.get("output_language_policy") or {},
                "supplements": list(job.get("supplements") or []),
                "previous_round": job.get("previous_round"),
                "previous_candidate_is_evidence": False,
            }
            _atomic_json(temporary / "input.json", manifest)
            metadata = {
                "round": round_number,
                "ov_task_id": job.get("ov_task_id"),
                "source_snapshot": job.get("source_snapshot"),
                "output_uri": job.get("output_uri"),
                "round_context_uri": job.get("round_context_uri"),
                "submission_idempotency_key": job.get("submission_idempotency_key"),
                "started_at": job.get("started_at"),
                "completed_at": utc_now(),
                "processing_seconds": job.get("ov_processing_seconds"),
                "remote_warnings": list(job.get("remote_warnings") or []),
                "compiler_sha256": str(job.get("compiler_sha256") or ""),
                "compiler_source_sha256": str(job.get("compiler_source_sha256") or ""),
                "compiler_uri": str(job.get("compiler_uri") or ""),
                "skill_uri": str(job.get("skill_uri") or ""),
                "output_language_policy": job.get("output_language_policy") or {},
                "artifact_quality": job.get("artifact_quality"),
                "output_sha256": {
                    path.relative_to(temporary / "output").as_posix(): _file_sha256(path)
                    for path in sorted((temporary / "output").rglob("*"))
                    if path.is_file()
                },
            }
            _atomic_json(temporary / "meta.json", metadata)
            os.replace(temporary, destination)
        metadata = json.loads((destination / "meta.json").read_text(encoding="utf-8"))
        metadata["path"] = destination.relative_to(self.project_root).as_posix()
        return metadata


    def _archive_reviewed_output(self, job: dict[str, Any], submission: dict[str, Any] | None) -> None:
        destination = self._job_dir(job["job_id"]) / "rounds" / f"round-{int(job['current_round'])}"
        reviewed = destination / "reviewed_output"
        if not reviewed.exists():
            temporary = destination / "reviewed_output.tmp"
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(self.workspace(job), temporary)
            os.replace(temporary, reviewed)
        if submission is None:
            return
        _atomic_json(destination / "checkpoint_submission.json", submission)
        _atomic_json(
            self._job_dir(job["job_id"]) / "checkpoints" / "history" / f"round-{int(job['current_round'])}.json",
            submission,
        )


    def finish_materialized_round(self, job_id: str) -> dict[str, Any]:
        """Freeze Compile output, then wait for review or permit automatic continuation."""
        with self._lock:
            job = self.get_job(job_id)
            if job.get("status") in TERMINAL_JOB_STATES | {"waiting_human"}:
                return job
            from .mining_completion import complete, completion_only
            if completion_only(job):
                return complete(self, job)
            if not job.get("materialized"):
                raise ValueError("本轮产物尚未通过结构校验")
            if int(job.get("orchestration_version") or 0) < 2:
                return self.update_job(job_id, status="succeeded", finished_at=utc_now())
            metadata = self._archive_round(job)
            history = [row for row in job.get("rounds_history") or [] if row.get("round") != metadata["round"]]
            history.append(metadata)
            if job.get("human_checkpoints_effective"):
                review = self._read_review(job)
                if job.get("empty_review_policy") == "te-empty-review-skip.v1" and not review["questions"]:
                    # A declared empty review is not human approval or new Evidence.
                    # Keep a separate receipt; never fabricate a checkpoint submission.
                    record = {
                        "round": job["current_round"], "checkpoint": "after_compile",
                        "policy": "te-empty-review-skip.v1", "reason": "本轮无补证问题，自动跳过人工补证",
                        "question_count": 0, "human_approved": False, "new_evidence": False,
                        "review_sha256": _file_sha256(self.workspace(job) / "semantic_reports/review.json"),
                        "recorded_at": metadata["completed_at"],
                    }
                    if (record in (job.get("skipped_checkpoints") or [])
                            and job.get("round_completed_at") == metadata["completed_at"]):
                        return job
                    skipped = [row for row in job.get("skipped_checkpoints") or []
                               if row["round"] != job["current_round"]] + [record]
                    job = self.update_job(job_id, skipped_checkpoints=skipped, pending_checkpoint=None)
                else:
                    return self._wait_for_round_review(job, metadata, history, review)
            from .native_compiler import is_native
            no_native_work = is_native(job) and (
                not job.get("human_checkpoints_effective") or not self._read_review(job)["questions"]
            )
            if int(job["current_round"]) >= int(job["max_rounds"]) or no_native_work:
                quality = dict(job.get("artifact_quality") or {})
                quality["can_submit"] = bool(quality.get("has_skill"))
                return self.update_job(
                    job_id,
                    rounds_history=history,
                    status="succeeded",
                    finished_at=utc_now(),
                    round_completed_at=metadata["completed_at"],
                    artifact_quality=quality,
                    phase={"step1": "done", "step2": "done", "step3": "done"},
                )
            return self.update_job(job_id, rounds_history=history, round_completed_at=metadata["completed_at"])


    def _wait_for_round_review(self, job: dict, metadata: dict, history: list, review: dict) -> dict:
        job_id = job["job_id"]
        pending = {
            "id": f"{job_id}-round-{job['current_round']}-review",
            "round": job["current_round"],
            "checkpoint": "after_compile",
            "title": f"第 {job['current_round']} 轮 Evidence 复核与补充",
            "intro": "检查本轮 Skill Candidate 和 Test Dataset；补充 Evidence 或明确标记暂时无法确认。",
            "questions": [self._question_row(question) for question in review["questions"]],
            "allow_stop": True,
            "can_continue": int(job["current_round"]) < int(job["max_rounds"]),
        }
        quality = dict(job.get("artifact_quality") or {})
        quality["can_submit"] = False
        return self.update_job(
            job_id,
            rounds_history=history,
            status="waiting_human",
            pending_checkpoint=pending,
            round_completed_at=metadata["completed_at"],
            finished_at="",
            artifact_quality=quality,
            phase={"step1": "done", "step2": "done", "step3": "active"},
        )


    def _validate_submission(
        self,
        job: dict[str, Any],
        checkpoint_id: str,
        answers: Any,
        unresolved: Any,
        references: Any,
        feedback: Any,
        request_id: Any,
        submitted_by: Any,
        attachments: Any,
    ) -> dict[str, Any]:
        pending = job.get("pending_checkpoint") or {}
        if job.get("status") != "waiting_human" or not pending:
            raise ValueError("该挖掘任务当前没有待填写的 Evidence 补充")
        if checkpoint_id != pending.get("id"):
            raise ValueError("复核问题已过期，请刷新任务详情")
        questions = {question["qid"]: question for question in pending.get("questions") or []}
        if not isinstance(answers, dict) or not isinstance(unresolved, list) or not isinstance(references, dict):
            raise ValueError("答案、未解决问题或来源引用格式无效")
        if any(
            not isinstance(key, str) or key not in questions or not isinstance(value, str)
            for key, value in answers.items()
        ):
            raise ValueError("答案必须由本轮问题 ID 和文本组成")
        if any(not isinstance(key, str) or key not in questions for key in unresolved):
            raise ValueError("未解决问题必须引用本轮问题 ID")
        normalized_answers = {key: value.strip() for key, value in answers.items() if value.strip()}
        normalized_unresolved = list(dict.fromkeys(unresolved))
        if set(normalized_answers) & set(normalized_unresolved):
            raise ValueError("同一问题不能同时回答并标记为未解决")
        for key, question in questions.items():
            if question.get("required") and key not in normalized_answers and key not in normalized_unresolved:
                raise ValueError(f"请回答必填问题或明确标记暂时无法确认：{key}")
        normalized_refs: dict[str, list[str]] = {}
        forbidden = [str(job.get("output_uri") or ""), str(job.get("round_context_uri") or "")]
        forbidden.extend(str(row.get("output_uri") or "") for row in job.get("rounds_history") or [])
        forbidden.extend(str(row.get("round_context_uri") or "") for row in job.get("rounds_history") or [])
        for key, values in references.items():
            if key not in questions or not isinstance(values, list) or len(values) > 20:
                raise ValueError("来源引用必须由本轮问题 ID 和最多 20 个 Viking URI 组成")
            if any(not isinstance(value, str) for value in values):
                raise ValueError("来源引用必须是 Viking URI 文本")
            normalized_refs[key] = list(dict.fromkeys(validate_resource_uri(value) for value in values))
            if any(
                root and (uri == root or uri.startswith(root + "/"))
                for uri in normalized_refs[key]
                for root in forbidden
            ):
                raise ValueError("上一轮产物和复核意见不能作为 Evidence 来源")
        if not isinstance(feedback, str) or len(feedback) > 20_000:
            raise ValueError("复核意见必须是最多 20000 字符的文本")
        if sum(len(value) for value in normalized_answers.values()) + len(feedback) > MAX_SUPPLEMENT_CHARACTERS:
            raise ValueError("本轮补充 Evidence 文本超过 100000 字符")
        if not isinstance(request_id, str) or len(request_id) > 200 or not isinstance(submitted_by, str):
            raise ValueError("提交标识或提交人格式无效")
        pending_attachments = list(job.get("pending_attachments") or [])
        selected = pending_attachments if attachments is None else attachments
        if (
            not isinstance(selected, list)
            or len(selected) > 20
            or any(not isinstance(row, dict) or row not in pending_attachments for row in selected)
        ):
            raise ValueError("补充文档必须是本轮已上传的附件")
        content = {
            "checkpoint_id": checkpoint_id,
            "answers": normalized_answers,
            "unresolved": normalized_unresolved,
            "references": normalized_refs,
            "feedback": feedback.strip(),
            "attachments": selected,
            "submitted_by": submitted_by,
            "round": job["current_round"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            **content,
            "request_id": request_id,
            "fingerprint": fingerprint,
            "submitted_at": utc_now(),
            "checkpoint": pending["checkpoint"],
            "title": pending["title"],
            "intro": pending["intro"],
            "questions": pending["questions"],
            "source": "checkpoint",
            "reference_snapshots": list(job.get("pending_reference_snapshots") or []),
        }


    def _duplicate_submission(
        self,
        job: dict[str, Any],
        checkpoint_id: str,
        request_id: str,
        action: str,
        answers: Any,
        unresolved: Any,
        references: Any,
        feedback: Any,
        submitted_by: Any,
        attachments: Any,
    ) -> bool:
        last = job.get("last_checkpoint_submission") or {}
        if last.get("checkpoint_id") != checkpoint_id:
            if request_id and request_id == last.get("request_id"):
                raise ValueError("提交标识已经用于另一轮复核")
            return False
        if request_id and request_id != last.get("request_id"):
            return False
        if last.get("action") != action:
            raise ValueError("该轮复核已提交，请刷新任务详情")
        previous_job = {
            **job,
            "status": "waiting_human",
            "current_round": last["round"],
            "pending_checkpoint": {"id": checkpoint_id, **last},
            "pending_attachments": last.get("attachments") or [],
        }
        candidate = self._validate_submission(
            previous_job,
            checkpoint_id,
            answers,
            unresolved,
            references,
            feedback,
            request_id,
            submitted_by,
            attachments,
        )
        if (candidate["fingerprint"] != last.get("fingerprint")
                and not self._archived_submission_fingerprint(job, candidate, last)):
            raise ValueError("该轮复核已提交，重复请求内容不一致")
        return True


    def _archived_submission_fingerprint(self, job: dict, candidate: dict, last: dict) -> bool:
        """Recognize an exact replay whose host-only attachment locator moved."""
        archive = self._job_dir(job["job_id"]) / "rounds" / f"round-{last['round']}" / "checkpoint_submission.json"
        if archive.is_symlink() or not archive.is_file():
            return False
        archived = json.loads(archive.read_text(encoding="utf-8"))
        if archived.get("fingerprint") != last.get("fingerprint"):
            return False
        originals, current = archived.get("attachments", []), candidate.get("attachments", [])
        if len(originals) != len(current):
            return False
        marker = f"/mining_jobs/{job['job_id']}/"
        rebound = []
        for old, new in zip(originals, current, strict=True):
            old_path, new_path = old.get("local_path"), new.get("local_path")
            if ({key: value for key, value in old.items() if key != "local_path"}
                    != {key: value for key, value in new.items() if key != "local_path"}):
                return False
            if old_path != new_path and (
                not isinstance(old_path, str) or not isinstance(new_path, str)
                or marker not in old_path or marker not in new_path
                or old_path.split(marker, 1)[1] != new_path.split(marker, 1)[1]
            ):
                return False
            rebound.append(old)
        fields = ("checkpoint_id", "answers", "unresolved", "references", "feedback",
                  "attachments", "submitted_by", "round")
        content = {key: candidate[key] for key in fields}
        content["attachments"] = rebound
        return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == (
            last["fingerprint"])


    def _prepare_next_round(self, job: dict[str, Any], submission: dict[str, Any] | None = None) -> dict[str, Any]:
        round_number = int(job["current_round"]) + 1
        if round_number > int(job["max_rounds"]):
            raise ValueError("已达到挖掘轮数上限，请结束挖掘并保留最终产物")
        active = sum(
            row.get("engine") == "ov_compile"
            and row.get("status") in ACTIVE_JOB_STATES
            and row.get("job_id") != job["job_id"]
            for row in self._iter_jobs_unlocked()
        )
        if active >= MAX_PARALLEL_JOBS:
            raise RuntimeError(f"同时最多运行 {MAX_PARALLEL_JOBS} 个 OV Compile 挖掘任务")
        self._archive_round(job)
        if submission is not None:
            submission = {**submission, "action": "continue"}
        self._archive_reviewed_output(job, submission)
        run_root = f"{RUNS_ROOT}/{job['job_id']}"
        history = list(job.get("checkpoint_history") or [])
        supplements = list(job.get("supplements") or [])
        if submission is not None:
            history.append(submission)
            supplements.append(submission)
        round_record = next(row for row in job["rounds_history"] if row["round"] == job["current_round"])
        reset = {
            "status": "preparing",
            "current_round": round_number,
            "previous_round": round_record,
            "source_snapshot": f"{run_root}/rounds/round-{round_number}/source",
            "output_uri": f"{run_root}/rounds/round-{round_number}/output",
            "round_context_uri": f"{run_root}/rounds/round-{round_number}/context",
            "checkpoint_history": history,
            "supplements": supplements,
            "pending_checkpoint": None,
            "pending_attachments": [],
            "pending_reference_snapshots": [],
            "last_checkpoint_submission": submission or {},
            "submission_idempotency_key": f"te-sm-{job['job_id']}-round-{round_number}",
            "ov_task_id": "",
            "ov_status": "",
            "ov_stage": "",
            "started_at": "",
            "round_started_at": "",
            "finished_at": "",
            "error": "",
            "stop_reason": "",
            "materialized": False,
            "artifact_quality": None,
            "round_completed_at": "",
            "compile_request": {},
            "staged_protocol": "",
            "staged_state_path": "",
            "staged_progress": {},
            "input_manifest": "",
            "setup_timings": {},
            "submission_retry_count": 0,
            "submission_attempts": 0,
            "submission_next_retry_at": "",
            "submission_started_at": "",
            "submission_error": "",
            "submission_finished_at": "",
            "submission_seconds": None,
            "materialize_attempts": 0,
            "materialize_seconds": None,
            "materialize_started_at": "",
            "materialize_finished_at": "",
            "ov_cancel_requested_at": "",
            "ov_cancel_reason": "",
            "ov_cancel_sent_at": "",
            "ov_cancel_error": "",
            "ov_last_synced_at": "",
            "ov_last_sync_attempt_at": "",
            "ov_sync_error": "",
            "ov_sync_failures": 0,
            "ov_next_retry_at": "",
            "ov_stage_history": [],
            "ov_event_timeline": [],
            "ov_task_created_at": "",
            "ov_task_started_at": "",
            "ov_task_updated_at": "",
            "ov_processing_seconds": None,
            "ov_pipeline": {},
            "remote_warnings": [],
            "compiler_sha256": "",
            "compiler_source_sha256": "",
            "compiler_uri": "",
            "skill_uri": SKILL_URI,
            "compiler_path": "",
            "compile_observed_seconds": None,
            "phase": {"step1": "active", "step2": "idle", "step3": "idle"},
            "capability_plan": None,
            "candidate_count": 0,
            "candidate_quality": {},
            "valid_candidate_count": 0,
            "workspace_reset_pending": True,
        }
        from .native_compiler import is_native
        if is_native(job):
            reset["output_uri"] = job["stable_output_uri"]
            reset["workspace_reset_pending"] = False
            reset["native_repair_instruction"] = ""
        self.update_job(job["job_id"], **reset)
        return self.get_job(job["job_id"])


    def prepare_next_round(
        self,
        job_id: str,
        checkpoint_id: str,
        answers: dict[str, str],
        unresolved: list[str],
        references: dict[str, list[str]],
        feedback: str = "",
        request_id: str = "",
        *,
        submitted_by: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if self._duplicate_submission(
                job,
                checkpoint_id,
                request_id,
                "continue",
                answers,
                unresolved,
                references,
                feedback,
                submitted_by,
                attachments,
            ):
                return job
            from .mining_completion import completion_only, revision_submission_job
            submission_job = (revision_submission_job(job, checkpoint_id, feedback)
                              if completion_only(job) else job)
            submission = self._validate_submission(
                submission_job,
                checkpoint_id,
                answers,
                unresolved,
                references,
                feedback,
                request_id,
                submitted_by,
                attachments,
            )
            return self._prepare_next_round(job, submission)


    def prepare_automatic_next_round(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if (
                not job.get("materialized")
                or job.get("status") in TERMINAL_JOB_STATES | {"waiting_human"}
            ):
                raise ValueError("该任务当前不能自动开始下一轮")
            if job.get("human_checkpoints_effective"):
                records = job.get("skipped_checkpoints") or []
                receipt = next((row for row in records if row.get("round") == job["current_round"]), {})
                review_path = self.workspace(job) / "semantic_reports/review.json"
                if (job.get("empty_review_policy") != "te-empty-review-skip.v1"
                        or receipt.get("policy") != "te-empty-review-skip.v1"
                        or receipt.get("question_count") != 0
                        or receipt.get("human_approved") is not False
                        or receipt.get("review_sha256") != _file_sha256(review_path)
                        or self._read_review(job)["questions"]):
                    raise ValueError("有补证问题或空检查点记录已变化，不能自动开始下一轮")
            return self._prepare_next_round(job)


    def finish_job(
        self,
        job_id: str,
        checkpoint_id: str,
        *,
        answers: dict[str, str] | None = None,
        unresolved: list[str] | None = None,
        references: dict[str, list[str]] | None = None,
        feedback: str = "",
        request_id: str = "",
        submitted_by: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            answers = {} if answers is None else answers
            unresolved = [] if unresolved is None else unresolved
            references = {} if references is None else references
            if self._duplicate_submission(
                job,
                checkpoint_id,
                request_id,
                "finish",
                answers,
                unresolved,
                references,
                feedback,
                submitted_by,
                attachments,
            ):
                return job
            submission = self._validate_submission(
                job,
                checkpoint_id,
                answers,
                unresolved,
                references,
                feedback,
                request_id,
                submitted_by,
                attachments,
            )
            submission["action"] = "finish"
            self._archive_reviewed_output(job, submission)
            unresolved_qids = list(
                dict.fromkeys(
                    [
                        *submission["unresolved"],
                        *(
                            question["qid"]
                            for question in submission["questions"]
                            if question["qid"] not in submission["answers"]
                        ),
                    ]
                )
            )
            quality = dict(job.get("artifact_quality") or {})
            warnings = list(quality.get("warnings") or [])
            if unresolved_qids:
                warnings.append("结束挖掘时仍有未解决的 Evidence 缺口：" + "、".join(unresolved_qids))
            if (
                submission["answers"]
                or submission["attachments"]
                or submission["feedback"]
                or any(submission["references"].values())
                or submission["reference_snapshots"]
            ):
                warnings.append("最终复核新增的 Evidence 或意见尚未经过下一轮 Compile；请在 Candidate Review 中确认。")
            if warnings:
                quality.update(level="caution", label="需复核", warnings=list(dict.fromkeys(warnings)))
            quality["can_submit"] = bool(quality.get("has_skill"))
            return self.update_job(
                job_id,
                status="succeeded",
                finished_at=utc_now(),
                pending_checkpoint=None,
                pending_attachments=[],
                pending_reference_snapshots=[],
                checkpoint_history=[*(job.get("checkpoint_history") or []), submission],
                supplements=[*(job.get("supplements") or []), submission],
                last_checkpoint_submission=submission,
                unresolved_questions=unresolved_qids,
                artifact_quality=quality,
                phase={"step1": "done", "step2": "done", "step3": "done"},
            )


    def stop_waiting(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if job.get("status") != "waiting_human":
                raise ValueError("该任务当前没有等待人工复核")
            return self.update_job(
                job_id,
                status="stopped",
                pending_checkpoint=None,
                finished_at=utc_now(),
                stop_reason="用户停止了等待人工复核的挖掘任务",
            )


    def _public_checkpoint(self, job: dict[str, Any]) -> dict[str, Any] | None:
        pending = job.get("pending_checkpoint")
        if not isinstance(pending, dict):
            return None
        return {
            **pending,
            "attachments": [self._public_attachment(row) for row in job.get("pending_attachments") or []],
        }


    @staticmethod
    def _public_attachment(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "local_path"}


    def _public_submission(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "attachments": [self._public_attachment(item) for item in row.get("attachments") or []],
            "reference_snapshots": [self._public_attachment(item) for item in row.get("reference_snapshots") or []],
        }


    def _public_gaps(self, job: dict[str, Any]) -> dict[str, Any] | None:
        if not job.get("materialized"):
            return None
        try:
            questions = [self._question_row(row) for row in self._read_review(job)["questions"]]
        except ValueError:
            return None
        return {"total": len(questions), "questions": questions}


    def _public_rounds(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in job.get("rounds_history") or []:
            root = self._job_dir(job["job_id"]) / "rounds" / f"round-{int(row['round'])}"
            result.append(
                {
                    **row,
                    "artifacts": self._artifact_rows(root / "output"),
                    "reviewed_artifacts": self._artifact_rows(root / "reviewed_output"),
                }
            )
        return result
