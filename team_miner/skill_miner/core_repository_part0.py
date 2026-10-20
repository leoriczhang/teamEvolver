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


class RepositoryCoreMixin:
    """Thread-safe local registry for OV source references and mining jobs."""


    def __init__(self, state_root: Path | str, project_root: Path | str, *, backend) -> None:
        if backend is None or any(not callable(getattr(backend, name, None)) for name in (
            "load_job", "save_job", "list_jobs", "load_sources", "save_sources",
        )):
            raise TypeError("Skill mining requires an authoritative metadata backend")
        self.backend = backend
        self.state_root = Path(state_root).resolve()
        self.project_root = Path(project_root).resolve()
        self.jobs_root = self.project_root / "mining_jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()


    def _load_sources(self) -> list[dict[str, Any]]:
        rows = self.backend.load_sources()
        if not isinstance(rows, list):
            raise ValueError("Skill mining backend returned invalid source records")
        normalized: list[dict[str, Any]] = []
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["tags"] = normalize_source_tags(
                row.get("tags"),
                default=(SOURCE_TAG_SKILL,),
            )
            row["default"] = bool(row.get("default"))
            normalized.append(row)
        return normalized


    def _save_sources(self, rows: list[dict[str, Any]]) -> None:
        self.backend.save_sources(rows)


    def list_source_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._load_sources(), key=lambda row: str(row.get("created_at") or ""))


    def get_source(self, identity: str) -> dict[str, Any]:
        with self._lock:
            match = next((row for row in self._load_sources() if row.get("source_id") == identity), None)
        if match is None:
            raise KeyError(identity)
        return match


    def save_source(
        self,
        account: str,
        uri: str,
        *,
        name: str = "",
        tags: Any = None,
        replace_tags: bool = False,
        default: bool = False,
    ) -> dict[str, Any]:
        normalized = validate_resource_uri(uri)
        identity = source_id(account, normalized)
        requested_tags = normalize_source_tags(tags, default=(SOURCE_TAG_SKILL,))
        if default and SOURCE_TAG_KNOWLEDGE not in requested_tags:
            requested_tags.append(SOURCE_TAG_KNOWLEDGE)
        with self._lock:
            rows = self._load_sources()
            existing = next((row for row in rows if row.get("source_id") == identity), None)
            if existing:
                current_tags = normalize_source_tags(existing.get("tags"), default=(SOURCE_TAG_SKILL,))
                next_tags = requested_tags if replace_tags else list(dict.fromkeys([*current_tags, *requested_tags]))
                if existing.get("default") or default:
                    next_tags = list(dict.fromkeys([*next_tags, SOURCE_TAG_KNOWLEDGE]))
                next_tags = normalize_source_tags(next_tags)
                changed = next_tags != current_tags or (default and not existing.get("default"))
                if name and str(name).strip() != str(existing.get("name") or ""):
                    existing["name"] = str(name).strip()
                    changed = True
                if changed:
                    existing["tags"] = next_tags
                    existing["default"] = bool(existing.get("default") or default)
                    existing["updated_at"] = utc_now()
                    self._save_sources(rows)
                return dict(existing)
            now = utc_now()
            record = {
                "source_id": identity,
                "account": str(account),
                "uri": normalized,
                "name": str(name or normalized.rsplit("/", 1)[-1]),
                "tags": normalize_source_tags(requested_tags),
                "default": bool(default),
                "created_at": now,
                "updated_at": now,
            }
            rows.append(record)
            self._save_sources(rows)
            return dict(record)


    def ensure_default_knowledge_source(self, account: str) -> dict[str, Any]:
        identity = source_id(account, DEFAULT_KNOWLEDGE_SOURCE_URI)
        with self._lock:
            exists = any(row.get("source_id") == identity for row in self._load_sources())
        return self.save_source(
            account,
            DEFAULT_KNOWLEDGE_SOURCE_URI,
            name="" if exists else "默认知识库知识源",
            tags=[SOURCE_TAG_KNOWLEDGE],
            default=True,
        )


    def update_source(self, identity: str, *, tags: Any, name: str | None = None) -> dict[str, Any]:
        with self._lock:
            rows = self._load_sources()
            match = next((row for row in rows if row.get("source_id") == identity), None)
            if match is None:
                raise KeyError(identity)
            next_tags = normalize_source_tags(tags)
            if match.get("default") and SOURCE_TAG_KNOWLEDGE not in next_tags:
                raise ValueError("默认知识库知识源必须保留“知识库知识源”标签")
            match["tags"] = next_tags
            if name is not None and str(name).strip():
                match["name"] = str(name).strip()
            match["updated_at"] = utc_now()
            self._save_sources(rows)
            return dict(match)


    def remove_source(self, identity: str) -> dict[str, Any]:
        with self._lock:
            rows = self._load_sources()
            match = next((row for row in rows if row.get("source_id") == identity), None)
            if match is None:
                raise KeyError(identity)
            if match.get("default"):
                raise ValueError("默认知识库知识源不能移除")
            self._save_sources([row for row in rows if row.get("source_id") != identity])
            return match


    def _job_dir(self, job_id: str) -> Path:
        candidate = (self.jobs_root / str(job_id)).resolve()
        if candidate.parent != self.jobs_root.resolve():
            raise ValueError("挖掘任务 ID 无效")
        return candidate


    def create_job(
        self,
        *,
        name: str,
        account: str,
        source_uri: str,
        source_snapshot_uri: str,
        output_uri: str,
        document_count: int,
        max_rounds: int,
        runtime_timeout_seconds: int = 3600,
        human_checkpoints: bool = True,
        skip_empty_checkpoints: bool = False,
        consolidate_reviewed_candidates: bool = False,
        candidate_contract_version: int = 2,
        compiler_body: str | None = None,
        mining_mode: str = "auto",
        mining_direction: str = "",
        target_skill_count: int | None = None,
        job_id: str = "",
    ) -> dict[str, Any]:
        from .mining_intent import normalize_intent
        intent = normalize_intent(mining_mode, mining_direction, target_skill_count)
        if intent["active"] and candidate_contract_version != 2:
            raise ValueError("挖掘方向与数量模式需要当前多候选编译契约，请恢复默认编译 Skill")
        if compiler_body is not None and (not isinstance(compiler_body, str) or not compiler_body.strip()):
            raise ValueError("编译 Skill 必须为非空文本")
        if isinstance(candidate_contract_version, bool) or candidate_contract_version not in (1, 2):
            raise ValueError("Skill Candidate 编译契约版本必须为 1 或 2")
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 5:
            raise ValueError("挖掘轮数上限必须在 1 到 5 之间")
        if not 60 <= runtime_timeout_seconds <= 7200:
            raise ValueError("运行期限必须在 60 到 7200 秒之间")
        requested_id = str(job_id or "")
        if requested_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", requested_id):
            raise ValueError("挖掘任务 ID 无效")
        request_digest = hashlib.sha256(json.dumps({
            "name": name, "account": account, "source_uri": source_uri,
            "source_snapshot_uri": source_snapshot_uri, "output_uri": output_uri,
            "document_count": document_count, "max_rounds": max_rounds,
            "runtime_timeout_seconds": runtime_timeout_seconds, "human_checkpoints": human_checkpoints,
            "skip_empty_checkpoints": skip_empty_checkpoints,
            "consolidate_reviewed_candidates": consolidate_reviewed_candidates,
            "candidate_contract_version": candidate_contract_version, "compiler_body": compiler_body,
            "mining_intent": intent,
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._lock:
            if requested_id:
                existing = self.backend.load_job(requested_id)
                if existing is not None:
                    if existing.get("_deleted") or existing.get("creation_request_sha256") != request_digest:
                        raise ValueError("挖掘任务标识已用于不同请求")
                    return dict(existing)
            active = sum(
                1
                for row in self._iter_jobs_unlocked()
                if row.get("engine") == "ov_compile" and row.get("status") in ACTIVE_JOB_STATES
            )
            if active >= MAX_PARALLEL_JOBS:
                raise RuntimeError(f"同时最多运行 {MAX_PARALLEL_JOBS} 个 OV Compile 挖掘任务")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            job_id = requested_id or f"ovmine-{stamp}-{_safe_slug(name)}-{uuid.uuid4().hex[:8]}"
            now = utc_now()
            if source_snapshot_uri == f"{RUNS_ROOT}/pending/source":
                source_snapshot_uri = f"{RUNS_ROOT}/{job_id}/rounds/round-1/source"
            if output_uri == f"{RUNS_ROOT}/pending/output":
                output_uri = f"{RUNS_ROOT}/{job_id}/rounds/round-1/output"
            normalized_rounds = max_rounds
            job = {
                "job_id": job_id,
                "creation_request_sha256": request_digest,
                "name": str(name or f"{source_uri.rsplit('/', 1)[-1]} 挖掘任务")[:120],
                "mining_intent": intent,
                "engine": "ov_compile",
                "status": "preparing",
                "input_dir": source_uri,
                "account": account,
                "source_uri": source_uri,
                "source_snapshot": source_snapshot_uri,
                "output_uri": output_uri,
                "skill_uri": SKILL_URI,
                "compiler_uri": "",
                "compiler_source_sha256": "",
                "ov_task_id": "",
                "document_count": int(document_count),
                "max_rounds": normalized_rounds,
                "current_round": 1,
                "orchestration_version": 2,
                "staged_execution_protocol": "te-mining-optimized.v1" if candidate_contract_version == 2 else "",
                "staged_efficiency_protocol": "te-mining-efficient.v2" if candidate_contract_version == 2 else "",
                "staged_workflow_protocol": "te-direct-workflow-planning.v1" if candidate_contract_version == 2 else "",
                "staged_workflow_partition_protocol": (
                    "te-workflow-partition.v2" if candidate_contract_version == 2 else ""
                ),
                "staged_source_scope_protocol": "te-source-scope.v3" if candidate_contract_version == 2 else "",
                "staged_selection_recovery_protocol": (
                    "te-selection-recovery.v2" if candidate_contract_version == 2 else ""
                ),
                "staged_source_fragment_protocol": "te-source-fragments.v1" if candidate_contract_version == 2 else "",
                "staged_source_input_clarity_protocol": (
                    "te-source-input-clarity.v1" if candidate_contract_version == 2 else ""
                ),
                "staged_planning_alias_protocol": "te-planning-aliases.v1" if candidate_contract_version == 2 else "",
                "staged_case_group_protocol": "te-case-groups.v1" if candidate_contract_version == 2 else "",
                "staged_source_unit_protocol": "te-exact-source-units.v1" if candidate_contract_version == 2 else "",
                "staged_skill_body_protocol": "te-skill-body-patch.v1" if candidate_contract_version == 2 else "",
                "staged_compact_planning_protocol": "te-compact-planning.v1" if candidate_contract_version == 2 else "",
                "staged_quote_alias_protocol": (
                    "te-evidence-quote-aliases.v1" if candidate_contract_version == 2 else ""
                ),
                "staged_identity_partition_protocol": (
                    ("te-candidate-identity-partition.v3" if consolidate_reviewed_candidates
                     else "te-candidate-identity-partition.v2") if candidate_contract_version == 2 else ""
                ),
                "staged_uncertainty_scope_protocol": (
                    "te-uncertain-context-units.v1" if candidate_contract_version == 2 else ""
                ),
                "candidate_contract_version": candidate_contract_version,
                "original_source_snapshot": source_snapshot_uri,
                "rounds_history": [],
                "checkpoint_history": [],
                "supplements": [],
                "pending_checkpoint": None,
                "pending_attachments": [],
                "pending_reference_snapshots": [],
                "round_context_uri": "",
                "submission_idempotency_key": f"te-sm-{job_id}-round-1",
                "runtime_timeout_seconds": runtime_timeout_seconds,
                "human_checkpoints_requested": bool(human_checkpoints),
                "human_checkpoints_effective": bool(human_checkpoints),
                "empty_review_policy": "te-empty-review-skip.v1" if skip_empty_checkpoints else "",
                "skipped_checkpoints": [],
                "rounds_mode": "orchestrated",
                "options_warnings": [],
                "remote_warnings": [],
                "ov_cancel_requested_at": "",
                "ov_cancel_reason": "",
                "ov_cancel_sent_at": "",
                "ov_cancel_error": "",
                "phase": {"step1": "active", "step2": "idle", "step3": "idle"},
                "created_at": now,
                "started_at": "",
                "finished_at": "",
                "updated_at": now,
                "error": "",
                "stop_reason": "",
                "artifact_quality": None,
                "materialized": False,
                "ov_last_synced_at": "",
                "ov_last_sync_attempt_at": "",
                "ov_sync_error": "",
                "ov_sync_failures": 0,
                "ov_next_retry_at": "",
            }
            from .mining_completion import POLICY
            from .native_compiler import PROTOCOL as NATIVE_PROTOCOL
            if compiler_body and f"completion_policy: {POLICY}" in compiler_body.splitlines():
                job["completion_policy"] = POLICY
                job["benchmark_required"] = False
            if compiler_body and NATIVE_PROTOCOL in compiler_body:
                job["execution_protocol"] = NATIVE_PROTOCOL
                job["stable_output_uri"] = f"{RUNS_ROOT}/{job_id}/output"
                job["output_uri"] = job["stable_output_uri"]
                job["native_compile_history"] = []
                # These flags describe the old host pipeline, not native execution.
                for key in list(job):
                    if key.startswith("staged_"):
                        job.pop(key)
            job_dir = self._job_dir(job_id)
            (job_dir / "workspace" / "compiled_skill").mkdir(parents=True, exist_ok=False)
            (job_dir / "workspace" / "semantic_reports").mkdir(parents=True, exist_ok=True)
            job["workspace"] = (job_dir / "workspace").relative_to(self.project_root).as_posix()
            if compiler_body is not None:
                job["compiler_body_snapshot"] = compiler_body
                (job_dir / "compiler-skill.md").write_text(compiler_body, encoding="utf-8")
                from .native_compiler import freeze_assets
                freeze_assets(job_dir, compiler_body)
            self.backend.save_job(job)
            return dict(job)


    def _iter_jobs_unlocked(self) -> list[dict[str, Any]]:
        rows = self.backend.list_jobs()
        if not isinstance(rows, list):
            raise ValueError("Skill mining backend returned invalid jobs")
        return [dict(row) for row in rows if isinstance(row, dict) and not row.get("_deleted")]


    def list_jobs(self, *, engine_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._iter_jobs_unlocked()
        if engine_only:
            rows = [row for row in rows if row.get("engine") == "ov_compile"]
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._job_dir(job_id)
            row = self.backend.load_job(job_id)
            if not isinstance(row, dict) or row.get("_deleted"):
                raise KeyError(job_id)
            row = dict(row)
            if row.get("workspace_reset_pending"):
                # Preparing is durable before any destructive workspace reset. A
                # restarted monitor completes this local step before submitting OV.
                workspace = self.workspace(row)
                if workspace.exists():
                    shutil.rmtree(workspace)
                (workspace / "compiled_skill").mkdir(parents=True)
                (workspace / "semantic_reports").mkdir(parents=True)
                row["workspace_reset_pending"] = False
                self.backend.save_job(row)
            return row


    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            row = self.get_job(job_id)
            if row.get("status") in {"stopping", "stopped"} or row.get("ov_cancel_reason"):
                # A late worker result may record acceptance, but cannot undo a
                # stop committed while it was reading or awaiting the remote ACK.
                uncertain_stop = (changes.get("status") == "interrupted" and row.get("ov_cancel_sent_at")
                                  and (changes.get("remote_terminal_unconfirmed")
                                       or row.get("remote_terminal_unconfirmed")))
                if changes.get("status") not in {"stopping", "stopped"} and not uncertain_stop:
                    changes = {
                        key: value for key, value in changes.items()
                        if key not in {"status", "finished_at", "error", "stop_reason"}
                    }
                for key in ("ov_cancel_reason", "ov_cancel_requested_at", "stop_reason", "ov_task_id"):
                    if row.get(key) and key in changes and not changes[key]:
                        changes.pop(key)
            row.update(changes)
            row["updated_at"] = utc_now()
            self.backend.save_job(row)
            return row


    def append_log(self, job_id: str, message: str) -> None:
        line = str(message).replace("\r", " ").rstrip("\n")
        if not line:
            return
        path = self._job_dir(job_id) / "run.log"
        with self._lock:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"[{utc_now()}] {line}\n")


    def delete_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.get_job(job_id)
            if row.get("status") in ACTIVE_JOB_STATES | {"waiting_human"} or row.get("remote_terminal_unconfirmed"):
                raise RuntimeError("运行中的挖掘任务请先停止")
            job_dir = self._job_dir(job_id)
            count = sum(1 for path in job_dir.rglob("*") if path.is_file())
            self.backend.save_job({**row, "_deleted": True})
            shutil.rmtree(job_dir, ignore_errors=True)
            return {"ok": True, "job_id": job_id, "deleted_files": count}


    def workspace(self, job: dict[str, Any]) -> Path:
        path = (self.project_root / str(job.get("workspace") or "")).resolve()
        if self.project_root not in path.parents:
            raise ValueError("挖掘任务工作区无效")
        return path


    def _output_target(self, job_id: str, relative_path: str) -> Path:
        job = self.get_job(job_id)
        workspace = self.workspace(job)
        raw = str(PurePosixPath(relative_path))
        parts = [part for part in raw.split("/") if part]
        if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
            raise ValueError("OV Compile 返回了无效产物路径")
        if parts[0] == "analysis":
            parts[0] = "semantic_reports"
        elif parts[0] != "compiled_skill":
            if parts[-1] in {"SKILL.md", "EVALUATION.md", "BENCHMARK.md", "benchmark.json"}:
                parts.insert(0, "compiled_skill")
        target = (workspace / Path(*parts)).resolve()
        if workspace not in target.parents:
            raise ValueError("OV Compile 产物路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


    def write_output(self, job_id: str, relative_path: str, content: str) -> Path:
        target = self._output_target(job_id, relative_path)
        target.write_text(content, encoding="utf-8")
        return target


    def write_output_stream(self, job_id: str, relative_path: str, chunks: Iterable[str]) -> Path:
        """Commit a complete validated text stream atomically, preserving any previous artifact on error."""
        target = self._output_target(job_id, relative_path)
        temporary = target.with_name(f".te-stream-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, str):
                        raise ValueError("OV Compile 产物流必须包含 UTF-8 文本")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


    def finalize_materialized(self, job_id: str, *, after_edit: bool = False, render_skill: str = "") -> dict[str, Any]:
        job = self.get_job(job_id)
        from .mining_completion import completion_only, finalize_inventory
        if completion_only(job):
            return finalize_inventory(self, job)
        workspace = self.workspace(job)
        for name in ("evidence-map.md", "gaps.md"):
            report = workspace / "semantic_reports" / name
            if not report.is_file() or not report.read_text(encoding="utf-8").strip():
                raise ValueError(f"OV Compile 未生成 analysis/{name}")
        skill_files = sorted((workspace / "compiled_skill").glob("*/SKILL.md"))
        plan = self._read_capability_plan(job) if int(job.get("candidate_contract_version") or 1) >= 2 else None
        if plan is None and len(skill_files) != 1:
            raise ValueError(f"OV Compile 应生成一个 Skill Candidate，实际找到 {len(skill_files)} 个")
        planned_names = (
            {row["name"] for row in plan["skills"]} if plan is not None else {p.parent.name for p in skill_files}
        )
        actual_names = {p.name for p in (workspace / "compiled_skill").iterdir() if p.is_dir()}
        if plan is not None and not actual_names <= planned_names:
            raise ValueError("Skill Candidate 产物与能力拆分计划不一致")
        candidate_quality = {}
        question_count = 0
        valid_count = 0
        warnings = [item.strip() for item in job.get("remote_warnings") or [] if isinstance(item, str) and item.strip()]
        for name in sorted(planned_names):
            skill_dir = workspace / "compiled_skill" / name
            try:
                count = self._validate_candidate(
                    job, skill_dir, render_view=plan is not None and (not after_edit or name == render_skill)
                )
                valid_count += 1
                question_count += count
                errors = []
            except ValueError as exc:
                if plan is None:
                    raise
                count = 0
                errors = [str(exc)]
                warnings.append(f"{name}：{exc}")
            candidate_quality[name] = {
                "has_skill": (skill_dir / "SKILL.md").is_file(),
                "level": "incomplete" if errors else "ready",
                "label": "产物不完整" if errors else "可提交",
                "confidence": "候选级",
                "summary": f"Test Dataset · {count} 条" if not errors else errors[0],
                "warnings": errors,
                "artifacts": [],
                "can_submit": not errors,
            }
        if planned_names and not valid_count and not after_edit:
            raise ValueError("所有 Skill Candidate 均未通过产物校验：" + "；".join(warnings))
        if not planned_names:
            warnings.append("Evidence 尚不足以形成可独立执行的 Skill；请查看能力拆分计划。")
        if int(job.get("orchestration_version") or 0) >= 2:
            review = self._read_review(job)
            warnings.extend(question["question"] for question in review["questions"])
        if after_edit:
            # Keep human review warnings while replacing stale per-candidate errors.
            previous_warnings = (job.get("artifact_quality") or {}).get("warnings") or []
            warnings.extend(
                value
                for value in previous_warnings
                if isinstance(value, str) and not any(value.startswith(name + "：") for name in planned_names)
            )
        quality = {
            "has_skill": bool(valid_count),
            "candidate_count": len(planned_names),
            "level": "incomplete" if planned_names and not valid_count else "caution" if warnings else "ready",
            "label": "需复核" if warnings else "可提交",
            "confidence": "候选级",
            "summary": (
                f"规划 {len(planned_names)} 个 Skill Candidate，其中 {valid_count} 个通过产物校验，"
                f"共 {question_count} 条案例。"
            ),
            "warnings": list(dict.fromkeys(item for item in warnings if item)),
            "artifacts": [
                {"kind": "skill", "state": "ready", "label": "Skill Candidate 已生成", "detail": "等待候选评审。"},
                {"kind": "evaluation", "state": "ready", "label": "评测定义已生成", "detail": "可人工编辑。"},
                {
                    "kind": "benchmark",
                    "state": "ready",
                    "label": f"Test Dataset · {question_count} 条",
                    "detail": "已通过 Test Dataset 结构校验。",
                },
            ],
            "can_submit": bool(valid_count)
            and (
                (after_edit and job.get("status") == "succeeded")
                or int(job.get("orchestration_version") or 0) < 2
                or (
                    not bool(job.get("human_checkpoints_effective"))
                    and int(job.get("current_round") or 1) >= int(job.get("max_rounds") or 1)
                )
            ),
        }
        if not planned_names:
            quality["artifacts"] = []
            quality["label"] = "未形成候选"
        return self.update_job(
            job_id,
            materialized=bool(valid_count) or not planned_names,
            artifact_quality=quality,
            capability_plan=plan,
            candidate_count=len(planned_names),
            candidate_quality=candidate_quality,
            valid_candidate_count=valid_count,
        )


    def refresh_edited_quality(self, job_id: str, relative_path: str) -> dict[str, Any]:
        """Refresh selected candidates after edits without changing the review gate."""
        job = self.get_job(job_id)
        if int(job.get("candidate_contract_version") or 1) < 2:
            return job
        parts = PurePosixPath(relative_path).parts
        # Repairs to a candidate's prerequisites may reach the deterministic
        # view renderer for the first time. Direct edits to that view stay
        # validation-only so an inconsistent human edit is never overwritten.
        render_skill = parts[-2] if parts[-1] in {"SKILL.md", "EVALUATION.md", "benchmark.json"} else ""
        try:
            return self.finalize_materialized(job_id, after_edit=True, render_skill=render_skill)
        except ValueError as exc:
            quality = dict(job.get("artifact_quality") or {})
            quality.update(
                level="incomplete",
                label="需修复",
                has_skill=False,
                can_submit=False,
                summary=str(exc),
                warnings=[str(exc)],
            )
            candidates = {
                name: {**value, "can_submit": False} for name, value in (job.get("candidate_quality") or {}).items()
            }
            return self.update_job(job_id, artifact_quality=quality, candidate_quality=candidates, materialized=False)


    def _validate_candidate(self, job: dict[str, Any], skill_dir: Path, *, render_view: bool = False) -> int:
        parsed = _skill_metadata(skill_dir / "SKILL.md")
        if parsed is None:
            raise ValueError("SKILL.md frontmatter 无效：请检查 YAML 语法和必填 name / description 字段")
        if str(parsed.get("name") or "") != skill_dir.name:
            raise ValueError("SKILL.md frontmatter 的 name 必须与目录名一致")
        evaluation = skill_dir / "EVALUATION.md"
        if not evaluation.is_file() or not evaluation.stat().st_size:
            raise ValueError("OV Compile 未生成 EVALUATION.md")

        benchmark_path = skill_dir / "benchmark.json"
        if not benchmark_path.is_file():
            raise ValueError("OV Compile 未生成 benchmark.json")
        document, errors = benchmark_format.read_document(benchmark_path)
        if document is not None:
            errors.extend(benchmark_format.validate_document(document, expected_skill_name=skill_dir.name))
        if errors:
            raise ValueError("Test Dataset 格式不合法：" + "；".join(dict.fromkeys(errors)))
        if int(job.get("candidate_contract_version") or 1) >= 2:
            if document_skill_ids(document or {}) != [skill_dir.name] or any(
                case.get("skill_ids") != [skill_dir.name] for case in (document or {}).get("cases") or []
            ):
                raise ValueError("独立 Test Dataset 只能引用当前 Skill Candidate")
        case_count = len((document or {}).get("cases") or [])
        if not 12 <= case_count <= 24:
            raise ValueError(f"Test Dataset 应有 12–24 条案例，实际为 {case_count} 条")
        for case in (document or {}).get("cases") or []:
            citations = (case.get("provenance") or {}).get("session_ids") or []
            if not citations or any(not isinstance(uri, str) or not uri.startswith("viking://") for uri in citations):
                raise ValueError("每条 Test Dataset 案例必须包含非空的来源 Viking URI")
            for uri in citations:
                if int(job.get("candidate_contract_version") or 1) >= 2:
                    uri = validate_resource_uri(uri)
                if not any(
                    uri == prefix or str(uri).startswith(prefix + "/") for prefix in self._citation_prefixes(job)
                ):
                    raise ValueError("Test Dataset 引用不属于本任务知识源")
        benchmark_md = skill_dir / "BENCHMARK.md"
        if not benchmark_md.is_file() or not benchmark_md.read_text(encoding="utf-8").strip():
            raise ValueError("OV Compile 未生成完整的 BENCHMARK.md，禁止自动补齐必需产物")
        if int(job.get("candidate_contract_version") or 1) >= 2:
            if render_view:
                # The model must emit all required files. Normalize only this derived
                # human view after the canonical JSON has passed validation.
                benchmark_md.write_text(dataset_markdown(document), encoding="utf-8")
            else:
                view = benchmark_md.read_text(encoding="utf-8")
                for case in document["cases"]:
                    required = [case["case_id"], case["query"]] + [check["text"] for check in case["checks"]]
                    if any(value not in view for value in required):
                        raise ValueError("BENCHMARK.md 与 Test Dataset 不一致，请重新编译当前候选")
        return case_count


    def _read_capability_plan(self, job: dict[str, Any]) -> dict[str, Any]:
        path = self.workspace(job) / "semantic_reports" / "capability-plan.json"
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("OV Compile 未生成合法的 analysis/capability-plan.json") from exc
        if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise ValueError("能力拆分计划 schema_version 无效")
        skills, deferred = plan.get("skills"), plan.get("deferred")
        if not isinstance(skills, list) or not isinstance(deferred, list):
            raise ValueError("能力拆分计划必须包含 skills/deferred 数组")
        from .native_compiler import is_native
        if not is_native(job) and job.get("staged_protocol") != "te-staged-compile.v1" and len(skills) > MAX_CANDIDATES:
            raise ValueError("单次 Compile 的能力拆分计划最多包含 8 个候选")
        if not skills and not deferred and job.get("staged_protocol") != "te-staged-compile.v1":
            raise ValueError("零候选的能力拆分计划必须说明暂缓原因")
        names = set()
        for row in skills + deferred:
            if not isinstance(row, dict):
                raise ValueError("能力拆分计划条目无效")
            fields = ("name", "task", "trigger", "boundary", "rationale") if row in skills else ("reason",)
            if any(not isinstance(row.get(key), str) or not row[key].strip() for key in fields):
                raise ValueError("能力拆分计划缺少任务边界或暂缓原因")
            refs = row.get("source_refs")
            if isinstance(refs, list):
                refs = [validate_resource_uri(uri) for uri in refs]
            if (
                not isinstance(refs, list)
                or not refs
                or any(
                    not isinstance(uri, str)
                    or not any(uri == root or uri.startswith(root + "/") for root in self._citation_prefixes(job))
                    for uri in refs
                )
            ):
                raise ValueError("能力拆分计划引用不属于本任务知识源")
        for row in skills:
            name = row["name"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 80 or name in names:
                raise ValueError("能力拆分计划候选名称无效或重复")
            names.add(name)
            for key in ("inputs", "outputs"):
                values = row.get(key)
                if (
                    not isinstance(values, list)
                    or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)
                ):
                    raise ValueError("能力拆分计划缺少独立任务输入输出")
        return plan


    @staticmethod
    def _citation_prefixes(job: dict[str, Any]) -> list[str]:
        prefixes = [
            str(job.get(key) or "").rstrip("/")
            for key in (
                "source_uri",
                "source_snapshot",
                "original_source_snapshot",
            )
        ]
        prefixes.extend(str(row.get("source_snapshot") or "").rstrip("/") for row in job.get("rounds_history") or [])
        return list(dict.fromkeys(prefix for prefix in prefixes if prefix))
