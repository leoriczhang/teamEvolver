# ruff: noqa: F401
from __future__ import annotations

from .ov_staged_orchestration_helpers import (
    ARTIFACT_BATCH_STAGE,
    BUSINESS_FIELDS,
    CASE_BATCH_STAGE,
    EFFICIENCY_PROTOCOL,
    EXTRACT_BATCH_STAGE,
    FINE_EXTRACT_MAX_CHUNKS,
    FINE_SOURCE_FRAGMENT_CHARS,
    IDENTITY_PROTOCOL,
    MAX_EXTRACT_RAW_CHARS,
    MAX_PLAN_DEPTH,
    MAX_STAGE_ATTEMPTS,
    MERGE_BATCH_PROTOCOL,
    MERGE_BATCH_STAGE,
    MIN_STAGE_BUDGET_SECONDS,
    REFINEMENT_PROTOCOL,
    RUNS_ROOT,
    SOURCE_FRAGMENT_PROTOCOL,
    SOURCE_INPUT_CLARITY_PROTOCOL,
    SOURCE_SCOPE_PROTOCOL_V3,
    SOURCE_SCOPE_PROTOCOLS,
    STAGED_PROTOCOL,
    WORKFLOW_BATCH_STAGE,
    CandidateIdentityMixin,
    DeferredEvidenceReviewMixin,
    HTTPException,
    OvArtifactBatchMixin,
    OvCaseBatchOrchestrationMixin,
    OvExtractBatchMixin,
    OvMergeBatchMixin,
    OvWorkflowBatchTransportMixin,
    OvWorkflowPlanningMixin,
    Path,
    _alias,
    _assignment_revision,
    _bind_unit,
    _canonical_extract_references,
    _canonical_planning_header,
    _canonical_stage_packet,
    _capacity_reason_limit,
    _chunk_metadata,
    _chunks,
    _decode_selection_aliases,
    _direct_record,
    _freeze_chunks,
    _group_count,
    _json,
    _method_groups,
    _package,
    _prior_candidate_rows,
    _project,
    _raw_group,
    _read,
    _revision_view,
    _save,
    _selection_alias_result,
    _selection_known,
    _selection_packet,
    _sha,
    _staged_execution_budget,
    _write,
    asyncio,
    commit_refinements,
    compile_admission,
    copy,
    dataset_markdown,
    hashlib,
    httpx,
    json,
    math,
    os,
    output_language_instruction,
    planning,
    prepare_refinements,
    re,
    render_refinement_plan_compiler,
    shutil,
    stage_compiler_body,
    tempfile,
    time,
    utc_now,
    validate_artifact_headings,
    yaml,
)


class StagedPart0Mixin:
    """The host owns authentication, repository locking, task polling and cancellation."""


    def _staged_root(self, job: dict) -> Path:
        repository = self._ov_skill_mining_repository()
        return repository._job_dir(job["job_id"]) / "round_inputs" / f"round-{int(job['current_round'])}" / "staged"


    @staticmethod
    def _staged_canonical_packet_for_current(root: Path, state: dict, current: dict):
        return _canonical_stage_packet(root, state, current)


    async def _staged_load(self, job: dict) -> tuple[Path, dict]:
        root = self._staged_root(job)
        state = await asyncio.to_thread(_read, root / "state.json")
        if state.get("protocol") != STAGED_PROTOCOL or state.get("round") != int(job["current_round"]):
            raise ValueError("阶段状态不属于当前任务轮次")
        if state.get("mining_intent"):
            from .mining_intent import intent_from_job, intent_sha
            intent = intent_from_job(job)
            if state["mining_intent"] != intent or state.get("mining_intent_sha256") != intent_sha(intent):
                raise ValueError("任务的冻结挖掘方向或数量已发生变化")
        return root, state


    async def _staged_save(self, job: dict, root: Path, state: dict, **changes) -> dict:
        labels = {
            "extract": "来源抽取",
            "plan": "能力规划",
            "merge_index": "方法合并",
            "selection": "逐项能力选择",
            "skill_section": "Skill 文件生成",
            "evaluation": "评测说明",
            "case": "测试案例生成",
            "assemble": "产物组装",
            "evidence_review": "暂缓方法原文复核",
            "identity_group": "候选身份复核",
            "workflow_partition": "完整方法归组",
            "workflow_metadata": "候选方法说明",
        }
        completed = state.get("completed_tasks", 0)
        if state.get("direct_workflow_protocol") and state["phase"] == "selection":
            labels["selection"] = "完整流程规划"
        state["estimated_tasks"] = (
            completed if state.get("complete") else max(state.get("estimated_tasks", 0), completed + 1)
        )
        progress = {
            "phase": state["phase"],
            "label": labels[state["phase"]],
            "completed": state.get("completed_tasks", 0),
            "total": state.get("estimated_tasks", 0),
            "subtask_index": completed if state.get("complete") else completed + 1,
        }
        budget = _staged_execution_budget(job, state)
        await asyncio.to_thread(_save, root / "state.json", state)
        return await asyncio.to_thread(
            self._ov_skill_mining_repository().update_job,
            job["job_id"],
            staged_protocol=STAGED_PROTOCOL,
            staged_state_path=str(root / "state.json"),
            staged_progress=progress,
            **budget,
            **changes,
        )


    @staticmethod
    def _staged_stopped(job: dict) -> bool:
        return bool(job.get("ov_cancel_reason")) or job.get("status") in {"stopping", "stopped"}


    async def _acquire_staged_compile_slot(self, job: dict, state: dict, current: dict) -> bool:
        """The host must provide PostgreSQL-backed Compile admission."""
        if state.get("execution_protocol") != compile_admission.OPTIMIZED_PROTOCOL:
            return True
        raise RuntimeError("Skill mining host must implement _acquire_staged_compile_slot with durable admission")


    async def _mark_staged_compile_slot(self, job: dict, current: dict) -> None:
        """The host must provide PostgreSQL-backed Compile admission."""
        raise RuntimeError("Skill mining host must implement _mark_staged_compile_slot with durable admission")


    async def _release_staged_compile_slot(self, job: dict, task_id: str = "", *, key: str = "") -> None:
        """The host must provide PostgreSQL-backed Compile admission."""
        raise RuntimeError("Skill mining host must implement _release_staged_compile_slot with durable admission")


    async def _staged_release_rejected(self, job: dict, root: Path, state: dict, exc: Exception):
        """Release proven rejection only; an unknown transport acceptance keeps its reservation."""
        current = state.get("current") or {}
        if current.get("task_id"):
            return None
        if isinstance(exc, HTTPException) and exc.status_code == 409:
            try:
                accepted = await self._knowledge_request(
                    job["account"], "GET", f"/api/v1/compile/submissions/{current['key']}", timeout=5.0
                )
            except HTTPException as lookup:
                if lookup.status_code != 404:
                    return await self._round_submission_retry(self._ov_skill_mining_repository(), job, lookup)
            except (httpx.HTTPError, OSError) as lookup:
                return await self._round_submission_retry(self._ov_skill_mining_repository(), job, lookup)
            else:
                if not isinstance(accepted, dict) or not accepted.get("task_id"):
                    return await self._round_submission_retry(
                        self._ov_skill_mining_repository(), job,
                        ValueError("Exact-key lookup returned no task identity"),
                    )
                current["task_id"] = str(accepted["task_id"])
                await self._mark_staged_compile_slot(job, current)
                await asyncio.to_thread(_save, root / "children" / current["child"] / "metadata.json", current)
                job = await self._staged_save(job, root, state)
                return await self._record_round_acceptance(
                    self._ov_skill_mining_repository(), job, accepted, time.perf_counter()
                )
            await self._release_staged_compile_slot(job, key=current["key"])
        elif not current.get("submission_started_at") or (
            isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 413, 422}
        ):
            await self._release_staged_compile_slot(job, key=current.get("key", ""))
        return None


    async def _prepare_staged_round(self, job, compiler_body, frozen_entries, language_policy, revision_instruction):
        root = self._staged_root(job)
        if (root / "state.json").is_file():
            return await self._resume_staged_round(job)
        fine_fragments = (
            job.get("staged_source_fragment_protocol") == SOURCE_FRAGMENT_PROTOCOL
            and job.get("staged_source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3
            and job.get("staged_workflow_protocol") == "te-direct-workflow-planning.v1"
            and job.get("staged_efficiency_protocol") == EFFICIENCY_PROTOCOL
        )
        fragment_chars = FINE_SOURCE_FRAGMENT_CHARS if fine_fragments else 12000
        extract_max_chunks = FINE_EXTRACT_MAX_CHUNKS if fine_fragments else 8
        count = await asyncio.to_thread(_freeze_chunks, root, frozen_entries, job["source_snapshot"],
                                        max_chars=fragment_chars)
        groups = await asyncio.to_thread(_group_count, root, range(count), max_chunks=extract_max_chunks)
        await asyncio.to_thread(_write, root / "compiler-base.md", compiler_body)
        await asyncio.to_thread(_write, root / "revision.txt", revision_instruction)
        compilers = {}
        efficient = job.get("staged_efficiency_protocol") == EFFICIENCY_PROTOCOL
        from .direct_workflow_planning import (
            DIRECT_WORKFLOW_PROTOCOL,
            LOCAL_SELECTION_RECOVERY_PROTOCOL,
            SELECTION_RECOVERY_PROTOCOL,
        )

        direct = efficient and job.get("staged_workflow_protocol") == DIRECT_WORKFLOW_PROTOCOL
        local_scope = direct and job.get("staged_source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3
        recovery_protocol = job.get("staged_selection_recovery_protocol")
        selection_recovery = local_scope and recovery_protocol in (
            SELECTION_RECOVERY_PROTOCOL, LOCAL_SELECTION_RECOVERY_PROTOCOL,
        )
        local_recovery = selection_recovery and recovery_protocol == LOCAL_SELECTION_RECOVERY_PROTOCOL
        input_clarity = local_scope and (
            job.get("staged_source_input_clarity_protocol") == SOURCE_INPUT_CLARITY_PROTOCOL
        )
        planning_aliases = local_scope and job.get("staged_planning_alias_protocol") == "te-planning-aliases.v1"
        from .candidate_identity import SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        from .evidence_quote_aliases import PROTOCOL as QUOTE_ALIAS_PROTOCOL
        from .source_scope import UNCERTAINTY_SCOPE_PROTOCOL
        from .workflow_planning import COMPACT_PLANNING_PROTOCOL

        compact_planning = planning_aliases and (
            job.get("staged_compact_planning_protocol") == COMPACT_PLANNING_PROTOCOL
        )
        quote_aliases = compact_planning and job.get("staged_quote_alias_protocol") == QUOTE_ALIAS_PROTOCOL
        identity_partitions = compact_planning and (
            job.get("staged_identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        )
        uncertainty_scope = local_scope and job.get("staged_uncertainty_scope_protocol") == UNCERTAINTY_SCOPE_PROTOCOL
        case_groups = local_scope and job.get("staged_case_group_protocol") == "te-case-groups.v1"
        for stage in ("extract", "merge_index", "plan", "skill_section", "evaluation", "case"):
            if local_scope and stage == "extract":
                from .compile_strategy import render_extract_batch_compiler_v3

                if input_clarity:
                    from .compile_strategy import render_input_clarity_extract_batch_compiler_v3

                    body = render_input_clarity_extract_batch_compiler_v3(compiler_body)
                else:
                    body = render_extract_batch_compiler_v3(compiler_body)
            elif direct and stage == "plan":
                from .compile_strategy import render_direct_workflow_compiler, render_recovery_workflow_compiler

                if compact_planning:
                    from .compile_strategy import render_compact_aliased_workflow_compiler

                    body = render_compact_aliased_workflow_compiler(compiler_body)
                elif planning_aliases:
                    from .compile_strategy import render_aliased_workflow_compiler

                    body = render_aliased_workflow_compiler(compiler_body)
                elif local_recovery:
                    from .compile_strategy import render_local_recovery_workflow_compiler

                    body = render_local_recovery_workflow_compiler(compiler_body)
                else:
                    body = (render_recovery_workflow_compiler(compiler_body) if selection_recovery
                            else render_direct_workflow_compiler(compiler_body))
            elif efficient:
                from .compile_strategy import render_efficient_stage_compiler

                body = render_efficient_stage_compiler(compiler_body, stage)
            else:
                body = stage_compiler_body(compiler_body, stage)
            await asyncio.to_thread(_write, root / "compilers" / f"{stage}.md", body)
            compilers[stage] = _sha(body)
        state = {
            "protocol": STAGED_PROTOCOL,
            "planning_version": 2,
            "identity_protocol": IDENTITY_PROTOCOL,
            "round": int(job["current_round"]),
            "phase": "extract",
            "base_sha256": _sha(compiler_body),
            "compilers": compilers,
            "language_policy": language_policy,
            "chunk_count": count,
            "cursor": 0,
            "layer": 0,
            "unit_count": 0,
            "completed_tasks": 0,
            "attempted_tasks": 0,
            "budget_base_seconds": int(job.get("runtime_timeout_seconds") or 3600),
            "estimated_tasks": groups * 2 + 1,
            "current": None,
            "complete": False,
        }
        from .mining_intent import intent_from_job, intent_sha
        intent = intent_from_job(job)
        if intent["active"]:
            state["mining_intent"] = intent
            state["mining_intent_sha256"] = intent_sha(intent)
        if job.get("staged_execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
            state["execution_protocol"] = compile_admission.OPTIMIZED_PROTOCOL
        if efficient:
            from .ov_extract_batch_orchestration import EXTRACT_BATCH_PROTOCOL
            from .workflow_planning import WORKFLOW_PLANNING_PROTOCOL

            state.update(
                efficiency_protocol=EFFICIENCY_PROTOCOL, extract_batch_protocol=EXTRACT_BATCH_PROTOCOL,
                workflow_planning_protocol=WORKFLOW_PLANNING_PROTOCOL, merge_batch_protocol=MERGE_BATCH_PROTOCOL,
            )
        if direct:
            state["direct_workflow_protocol"] = DIRECT_WORKFLOW_PROTOCOL
            state.pop("merge_batch_protocol", None)
        if local_scope:
            state["source_scope_protocol"] = SOURCE_SCOPE_PROTOCOL_V3
        if selection_recovery:
            state["selection_recovery_protocol"] = recovery_protocol
        if input_clarity:
            state["source_input_clarity_protocol"] = SOURCE_INPUT_CLARITY_PROTOCOL
        if planning_aliases:
            state["planning_alias_protocol"] = "te-planning-aliases.v1"
        if compact_planning:
            state["compact_planning_protocol"] = COMPACT_PLANNING_PROTOCOL
        if quote_aliases:
            state["quote_alias_protocol"] = QUOTE_ALIAS_PROTOCOL
        if identity_partitions:
            state["identity_partition_protocol"] = job["staged_identity_partition_protocol"]
        if uncertainty_scope:
            state["uncertainty_scope_protocol"] = UNCERTAINTY_SCOPE_PROTOCOL
        if case_groups:
            state["case_group_protocol"] = "te-case-groups.v1"
        from .case_context import CASE_NARRATIVE_PROTOCOL

        state["case_narrative_protocol"] = CASE_NARRATIVE_PROTOCOL
        if local_scope and job.get("staged_workflow_partition_protocol") == "te-workflow-partition.v2":
            state["workflow_partition_protocol"] = job["staged_workflow_partition_protocol"]
            for workflow_stage in ("workflow_partition", "workflow_metadata"):
                await self._workflow_compiler(root, state, workflow_stage)
            state["workflow_compiler_sha256"] = {
                stage: state["aux_compilers"][stage] for stage in ("workflow_partition", "workflow_metadata")
            }
        body_protocols = {"te-incremental-skill-body.v1", "te-skill-body-patch.v1"}
        if (state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL
                and job.get("staged_skill_body_protocol") in body_protocols):
            state["skill_body_protocol"] = job["staged_skill_body_protocol"]
        if local_scope and job.get("staged_source_unit_protocol") == "te-exact-source-units.v1":
            from .source_units import freeze_source_units

            state["source_unit_protocol"] = job["staged_source_unit_protocol"]
            units = await asyncio.to_thread(freeze_source_units, root, state)
            state.update(source_unit_count=units["unit_count"], source_unit_cursor=0)
        if fine_fragments:
            state.update(source_fragment_protocol=SOURCE_FRAGMENT_PROTOCOL, source_fragment_chars=fragment_chars,
                         extract_max_chunks=extract_max_chunks)
        if state["round"] > 1:
            state["candidate_refinement_protocol"] = REFINEMENT_PROTOCOL
            if direct:
                from .compile_strategy import render_direct_workflow_compiler, render_recovery_workflow_compiler

                if compact_planning:
                    from .compile_strategy import render_compact_aliased_workflow_compiler

                    body = render_compact_aliased_workflow_compiler(compiler_body, refinement=True)
                elif planning_aliases:
                    from .compile_strategy import render_aliased_workflow_compiler

                    body = render_aliased_workflow_compiler(compiler_body, refinement=True)
                elif local_recovery:
                    from .compile_strategy import render_local_recovery_workflow_compiler

                    body = render_local_recovery_workflow_compiler(compiler_body, refinement=True)
                else:
                    body = (render_recovery_workflow_compiler(compiler_body, refinement=True) if selection_recovery
                            else render_direct_workflow_compiler(compiler_body, refinement=True))
            else:
                body = render_refinement_plan_compiler(compiler_body)
            if efficient and not direct:
                from .workflow_planning import WORKFLOW_PLANNING_RULE

                body += "\n\n" + WORKFLOW_PLANNING_RULE
            await self._ensure_aux_compiler(root, state, "plan", body)
        if state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
            from .round_reuse import prepare_round_reuse

            await asyncio.to_thread(prepare_round_reuse, job, root, state, frozen_entries)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)


    async def _resume_staged_round(self, job: dict) -> dict:
        root, state = await self._staged_load(job)
        # The state journal may have committed before the public job metadata.
        job = await self._staged_save(job, root, state)
        if state.get("complete"):
            if job.get("materialized") and job.get("status") not in {"waiting_human", "succeeded", "stopped", "failed"}:
                return await self._complete_ov_skill_round(job)
            return job
        current = state.get("current")
        if not current:
            return job if self._staged_stopped(job) else await self._staged_next(job, root, state)
        if current["stage"] == EXTRACT_BATCH_STAGE:
            return await self._resume_extract_batch(job, root, state)
        if current["stage"] == WORKFLOW_BATCH_STAGE:
            return await self._resume_workflow_batch(job, root, state)
        if current["stage"] == MERGE_BATCH_STAGE:
            return await self._resume_merge_batch(job, root, state)
        if current["stage"] == CASE_BATCH_STAGE:
            return await self._resume_case_batch(job, root, state)
        if current["stage"] == ARTIFACT_BATCH_STAGE:
            return await self._resume_artifact_batch(job, root, state)
        repository = self._ov_skill_mining_repository()
        started = time.perf_counter()
        try:
            request = await asyncio.to_thread(_read, root / current["request_file"])
            body = await asyncio.to_thread((root / current["compiler_file"]).read_text, encoding="utf-8")
            packet = await asyncio.to_thread((root / current["packet_file"]).read_text, encoding="utf-8")
            if (
                _sha(_json(request)) != current["request_sha256"]
                or _sha(body) != current["compiler_sha256"]
                or _sha(packet) != current["packet_sha256"]
            ):
                if current.get("planning_alias_protocol") or current.get("quote_alias_protocol"):
                    from .direct_workflow_planning import FrozenPlanningIntegrityError

                    raise FrozenPlanningIntegrityError("Frozen alias request, compiler or packet changed")
                raise ValueError("冻结的阶段请求或编译 Skill 已被修改")
            if (current.get("context", {}).get("typed_planning") == "selection"
                    or current["stage"] == "evidence_review_quotes"):
                await asyncio.to_thread(self._staged_canonical_packet_for_current, root, state, current)
            # A lost acceptance is resolved before any provisioning or input effect.
            accepted = None
            if current.get("task_id"):
                accepted = {"task_id": current["task_id"]}
            else:
                try:
                    accepted = await self._knowledge_request(
                        job["account"], "GET", f"/api/v1/compile/submissions/{current['key']}", timeout=20.0
                    )
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
            latest = await asyncio.to_thread(repository.get_job, job["job_id"])
            if accepted is None and self._staged_stopped(latest):
                await self._release_staged_compile_slot(job, key=current["key"])
                return await asyncio.to_thread(
                    repository.update_job, job["job_id"], status="stopped", finished_at=utc_now()
                )
            job = latest
            if accepted is None:
                if not await self._acquire_staged_compile_slot(job, state, current):
                    return await self._staged_save(job, root, state, status="preparing", ov_task_id="")
                client = self._round_client(job)
                name = request["skill"].rsplit("/", 1)[-1]
                await self._round_provision_compiler(client, name, body)
                stage_root = current["input_uri"].rsplit("/", 2)[0]
                for directory in (stage_root, current["input_uri"].rsplit("/", 1)[0], current["input_uri"]):
                    await client.mkdir(directory)
                await self._round_write_immutable(job["account"], current["input_uri"] + "/packet.json", packet)
                job = await asyncio.to_thread(repository.get_job, job["job_id"])
                if self._staged_stopped(job):
                    await self._release_staged_compile_slot(job, key=current["key"])
                    return job
                current["submission_started_at"] = utc_now()
                await asyncio.to_thread(_save, root / "state.json", state)
                accepted = await self._knowledge_request(
                    job["account"],
                    "POST",
                    "/api/v1/compile",
                    timeout=60.0,
                    extra_headers={"Idempotency-Key": current["key"]},
                    json=request,
                )
            task_id = str(accepted.get("task_id") or "") if isinstance(accepted, dict) else ""
            if not task_id:
                return await self._round_submission_retry(
                    repository, job, ValueError("阶段接受响应未返回 task_id，将通过原请求标识恢复")
                )
            current["task_id"] = task_id
            await self._mark_staged_compile_slot(job, current)
            await asyncio.to_thread(_save, root / "children" / current["child"] / "metadata.json", current)
            job = await asyncio.to_thread(repository.get_job, job["job_id"])
            job = await self._staged_save(
                job,
                root,
                state,
                compile_request=request,
                submission_idempotency_key=current["key"],
                compiler_uri=request["skill"],
                skill_uri=request["skill"],
                compiler_sha256=current["compiler_sha256"],
                compiler_source_sha256=current["compiler_source_sha256"],
            )
            return await self._record_round_acceptance(repository, job, accepted, started)
        except (httpx.HTTPError, OSError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                recovered = await self._staged_release_rejected(job, root, state, exc)
                if recovered is not None:
                    return recovered
                raise
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            await self._staged_release_rejected(job, root, state, exc)
            raise


    async def _staged_submit(
        self,
        job: dict,
        root: Path,
        state: dict,
        stage: str,
        output_path: str,
        chunks: list[dict],
        controls: dict,
        context: dict,
    ) -> dict:
        packet = planning.build_stage_packet(
            stage, output_path, evidence_chunks=chunks, controls=controls, language_policy=state["language_policy"]
        )
        canonical_packet = packet
        alias_manifest = None
        alias_family = "planning"
        if (state.get("planning_alias_protocol") == "te-planning-aliases.v1"
                and context.get("typed_planning") == "selection"):
            from .planning_aliases import build_planning_alias_packet

            packet, alias_manifest = build_planning_alias_packet(canonical_packet)
        elif stage == "evidence_review_quotes" and state.get("quote_alias_protocol") == "te-evidence-quote-aliases.v1":
            from .evidence_quote_aliases import build_quote_alias_packet

            packet, alias_manifest = build_quote_alias_packet(canonical_packet)
            alias_family = "quote"
        body = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if _sha(body) != state["base_sha256"]:
            raise ValueError("本轮编译 Skill 基础快照已被修改")
        compiler_folder = "aux-compilers" if stage in state.get("aux_compilers", {}) else "compilers"
        hashes = state.get("aux_compilers", {}) if compiler_folder == "aux-compilers" else state["compilers"]
        source_body = await asyncio.to_thread((root / compiler_folder / f"{stage}.md").read_text, encoding="utf-8")
        if _sha(source_body) != hashes[stage]:
            raise ValueError("本轮冻结的阶段编译 Skill 已被修改")
        name, published = _package(source_body)
        attempt = int(context.get("attempt") or 0)
        identity_parts = [state["round"], stage, context, packet, _sha(published)]
        if alias_manifest is not None:
            identity_parts.append(_sha(_json(alias_manifest)))
        identity = _sha(_json(identity_parts))[:24]
        child = f"{stage}-{identity}"
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{job['current_round']}/stages/{child}/input"
        output_uri = input_uri.rsplit("/", 1)[0] + "/output"
        revision = await asyncio.to_thread((root / "revision.txt").read_text, encoding="utf-8")
        request = {
            "from": [input_uri],
            "to": output_uri,
            "skill": f"viking://agent/skills/{name}",
            "instruction": f"本次只执行 {stage} 阶段；唯一相对输出路径是 {output_path}，禁止生成其它文件。"
            f"required_paths 如使用只能包含实际路径 {output_path}；packet.output_path 是字段引用，不能作为字面文件名。"
            "原文仅 packet.evidence；derived_controls 和旧候选是非事实控制上下文。"
            + output_language_instruction(state["language_policy"]),
        }
        if context.get("repair_error"):
            request["instruction"] += "\n上次输出未被接受，请完整重写：" + context["repair_error"][:1000]
        packet_text = _json(packet)
        remaining = 48000 - len(published) - len(packet_text) - len(request["instruction"]) - 1000
        skip_revision = stage in {"identity_group", "evidence_review", "evidence_review_quotes"} or (
            stage == "extract" and bool(state.get("round_reuse_protocol"))
        )
        if not skip_revision:
            request["instruction"] += _revision_view(revision, remaining, context.get("skill_name", ""))
        remaining = 48000 - len(published) - len(packet_text) - len(request["instruction"]) - 200
        if not skip_revision:
            request["instruction"] += await asyncio.to_thread(
                _assignment_revision, root, job, controls, context, remaining
            )
        if len(published) + len(packet_text) + len(request["instruction"]) > 48000:
            raise ValueError("阶段完整 Skill/原文/修订上下文超过有界预算")
        folder = f"children/{child}"
        packet_text = _json(packet)
        await asyncio.to_thread(_write, root / folder / "packet.json", packet_text)
        await asyncio.to_thread(_write, root / folder / "compiler.md", published)
        await asyncio.to_thread(_save, root / folder / "request.json", request)
        if alias_manifest is not None:
            await asyncio.to_thread(_write, root / folder / "canonical-packet.json", _json(canonical_packet))
            await asyncio.to_thread(_save, root / folder / f"{alias_family}-aliases.json", alias_manifest)
        state["current"] = {
            "child": child,
            "stage": stage,
            "context": context,
            "attempt": attempt,
            "expected_path": output_path,
            "output_uri": output_uri,
            "input_uri": input_uri,
            "key": f"te-staged-{job['job_id']}-r{state['round']}-{identity}",
            "task_id": "",
            "request_file": folder + "/request.json",
            "packet_file": folder + "/packet.json",
            "compiler_file": folder + "/compiler.md",
            "packet_sha256": _sha(packet_text),
            "request_sha256": _sha(_json(request)),
            "compiler_sha256": _sha(published),
            "compiler_source_sha256": _sha(source_body),
        }
        state["attempted_tasks"] = int(state.get("attempted_tasks", 0)) + 1
        if alias_manifest is not None:
            state["current"].update(
                canonical_packet_file=folder + "/canonical-packet.json",
                canonical_packet_sha256=_sha(_json(canonical_packet)),
                **{f"{alias_family}_alias_protocol": state[f"{alias_family}_alias_protocol"],
                   f"{alias_family}_alias_file": folder + f"/{alias_family}-aliases.json",
                   f"{alias_family}_alias_sha256": _sha(_json(alias_manifest))},
            )
        await asyncio.to_thread(_save, root / folder / "metadata.json", state["current"])
        latest = await asyncio.to_thread(self._ov_skill_mining_repository().get_job, job["job_id"])
        if self._staged_stopped(latest):
            await asyncio.to_thread(_save, root / "state.json", state)
            return latest
        job = await self._staged_save(
            job,
            root,
            state,
            status="preparing",
            error="",
            finished_at="",
            ov_task_id="",
            compile_request=request,
            submission_idempotency_key=state["current"]["key"],
        )
        return await self._resume_staged_round(job)


    async def _staged_next(self, job: dict, root: Path, state: dict) -> dict:
        if self._staged_stopped(job):
            return job
        phase = state["phase"]
        if (phase in {"skill_section", "evaluation", "case"} and state.get("identity_review_complete")
                and state.get("mining_intent", {}).get("active") and not state.get("intent_selection_complete")):
            from .mining_intent import apply_intent_selection
            audit = await asyncio.to_thread(apply_intent_selection, root, state)
            await asyncio.to_thread(self._ov_skill_mining_repository().write_output, job["job_id"],
                                    "analysis/mining-intent.json", _json(audit))
            changes = {}
            if audit["shortfall"] and not job.get("human_checkpoints_effective"):
                changes = {"human_checkpoints_effective": True,
                           "options_warnings": [*(job.get("options_warnings") or []),
                                                "目标数量尚未满足，本轮需要人工复核实际数量或补充来源。"]}
            job = await self._staged_save(job, root, state, **changes)
        if (phase in {"skill_section", "evaluation", "case"} and state.get("identity_review_complete")
                and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL
                and not state.get("source_routing_complete") and not state.get("current")):
            from .source_scope import route_final_candidates

            audit = await asyncio.to_thread(route_final_candidates, root, state, job["source_snapshot"])
            state["source_routing_complete"] = True
            state["source_routing_audit"] = audit
            job = await self._staged_save(job, root, state)
        if phase in {"skill_section", "evaluation", "case"} and state.get("planning_version") == 2 and not state.get(
            "identity_review_complete"
        ) and not state.get("current"):
            job = await self._staged_identity_enter(job, root, state)
            phase = state["phase"]
        if phase == "evidence_review":
            return await self._staged_evidence_review_next(job, root, state)
        if phase == "identity_group":
            return await self._staged_identity_next(job, root, state)
        if phase in {"workflow_partition", "workflow_metadata"}:
            return await self._workflow_next(job, root, state)
        if phase == "extract":
            if state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                return await self._staged_extract_batch_next(job, root, state)
            while state["cursor"] < state["chunk_count"]:
                chunks, after = await asyncio.to_thread(
                    _raw_group, root, range(state["chunk_count"]), state["cursor"]
                )
                refs = [_alias(chunk["id"]) for chunk in chunks]
                context = {
                    "after": after,
                    "ordinals": [chunk["ordinal"] for chunk in chunks],
                    "input_ids": [chunk["id"] for chunk in chunks],
                    "source_refs": refs,
                }
                if state.get("round_reuse_protocol"):
                    from .round_reuse import try_reuse_extract

                    if await asyncio.to_thread(try_reuse_extract, job, root, state, chunks, context):
                        job = await self._staged_save(job, root, state)
                        if self._staged_stopped(await asyncio.to_thread(
                            self._ov_skill_mining_repository().get_job, job["job_id"]
                        )):
                            return job
                        continue
                return await self._staged_submit(
                    job, root, state, "extract", "index.json", chunks,
                    {"allowed_source_refs": refs, "reference_map": dict(zip(refs, context["input_ids"]))}, context,
                )
            if state.get("planning_version") == 2:
                return await self._staged_catalog_ready(job, root, state)
            state.update(phase="plan", layer=1, cursor=0, input_unit_count=state["unit_count"], unit_count=0)
            job = await self._staged_save(job, root, state)
            return await self._staged_next(job, root, state)
        if phase == "plan":
            return await self._staged_plan_next(job, root, state)
        if phase in {"merge_index", "selection"}:
            return await self._staged_catalog_next(job, root, state)
        if phase in {"skill_section", "evaluation", "case"}:
            return await self._staged_candidate_next(job, root, state)
        return await self._staged_assemble(job, root, state)
