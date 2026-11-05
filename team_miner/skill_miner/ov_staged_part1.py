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


class StagedPart1Mixin:
    async def _staged_catalog_ready(self, job: dict, root: Path, state: dict) -> dict:
        counts = {"method": 0, "fact": 0, "gap": 0}
        for index in range(state["unit_count"]):
            unit = await asyncio.to_thread(_read, root / "units" / "0" / f"{index:08d}.json")
            files = {}
            for alias, ordinals in unit["bindings"].items():
                metadata = await asyncio.to_thread(_chunk_metadata, root, ordinals)
                files[alias] = list(dict.fromkeys(row["source_uri"] for row in metadata))
            for item in planning.planning_items([unit], source_files=files):
                selected = sorted({ordinal for ref in item["source_refs"] for ordinal in unit["bindings"][ref]})
                record = {
                    "item": item,
                    "unit_id": unit["id"],
                    "ordinals": selected,
                    "bindings": {ref: unit["bindings"][ref] for ref in item["source_refs"]},
                    "leaf_items": [item["item_id"]],
                }
                if state.get("source_unit_protocol"):
                    record["source_unit_bindings"] = {
                        ref: unit["source_unit_bindings"][ref] for ref in item["source_refs"]
                    }
                kind = item["kind"]
                await asyncio.to_thread(_save, root / "catalog" / kind / f"{counts[kind]:08d}.json", record)
                counts[kind] += 1
        await asyncio.to_thread(_save, root / "catalog" / "manifest.json", counts)
        if (state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL
                and not state.get("workflow_partition_protocol")):
            from .workflow_planning import prepare_workflow_order

            await asyncio.to_thread(prepare_workflow_order, root, counts["method"])
        state.update(
            phase="merge_index",
            catalog_counts=counts,
            cursor=0,
            proposal_count=0,
            selection_cursor=0,
            selection_count=0,
        )
        if state.get("direct_workflow_protocol"):
            from .direct_workflow_planning import DIRECT_WORKFLOW_PROTOCOL
            from .workflow_planning import workflow_method_index

            if state["direct_workflow_protocol"] != DIRECT_WORKFLOW_PROTOCOL:
                raise ValueError("Unknown frozen direct workflow protocol")
            # Local exact copies replace the redundant model merge. All existing
            # source review and disposition readers keep the same durable layout.
            for position in range(counts["method"]):
                index = (position if state.get("workflow_partition_protocol") else
                         await asyncio.to_thread(workflow_method_index, root, position, counts["method"]))
                record = await asyncio.to_thread(_read, root / "catalog/method" / f"{index:08d}.json")
                path = root / "proposals" / f"{position:08d}.json"
                if path.exists() and await asyncio.to_thread(_read, path) != record:
                    raise ValueError("Frozen direct planning method or source lineage changed")
                await asyncio.to_thread(_save, path, record)
                await asyncio.to_thread(_save, root / "direct-planning/mapping" / f"{position:08d}.json",
                                        {"catalog_index": index, "record_sha256": _sha(_json(record)),
                                         "item_id": record["item"]["item_id"]})
            await asyncio.to_thread(_save, root / "direct-planning/manifest.json",
                                    {"protocol": DIRECT_WORKFLOW_PROTOCOL, "method_count": counts["method"],
                                     "model_merge_submissions": 0, "original_catalog_unchanged": True})
            state.update(phase="selection", proposal_count=counts["method"])
        if state.get("workflow_partition_protocol") == "te-workflow-partition.v2":
            return await self._workflow_enter(job, root, state)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)


    @staticmethod
    def _staged_model_item(record: dict) -> dict:
        item = record["item"]
        result = {key: item[key] for key in item if key != "source_files"}
        result["source_file_ids"] = ["file-" + _sha(uri)[:24] for uri in item.get("source_files", [])]
        return result


    @staticmethod
    def _staged_readonly_note(record: dict) -> dict:
        """Context is deliberately not shaped like a consumable planning item."""
        item = record["item"]
        return {
            **{key: item[key] for key in ("text", "reason") if key in item},
            "source_file_ids": ["file-" + _sha(uri)[:24] for uri in item.get("source_files", [])],
            "readonly": True,
        }


    async def _staged_catalog_next(self, job: dict, root: Path, state: dict) -> dict:
        merging = state["phase"] == "merge_index"
        if merging and state.get("merge_batch_protocol") == MERGE_BATCH_PROTOCOL:
            return await self._staged_merge_batch_next(job, root, state)
        count = state["catalog_counts"]["method"] if merging else state["proposal_count"]
        cursor = state["cursor"] if merging else state["selection_cursor"]
        from .direct_workflow_planning import LOCAL_SELECTION_RECOVERY_PROTOCOL, MAX_DIRECT_METHODS

        local_recovery = not merging and state.get("selection_recovery_protocol") == LOCAL_SELECTION_RECOVERY_PROTOCOL
        recovery_end = state.get("direct_recovery_end") if local_recovery else None
        if local_recovery and ((state.get("direct_recovery_limit") is not None) != (recovery_end is not None)):
            raise ValueError("Frozen local selection recovery requires both its limit and original window end")
        if local_recovery and state.get("direct_recovery_limit") is not None:
            if type(recovery_end) is not int or not 0 < recovery_end <= count or cursor > recovery_end:
                raise ValueError("Frozen local selection recovery must stay inside its original method window")
            if cursor == recovery_end:
                state.pop("direct_recovery_end")
                state.pop("direct_recovery_limit")
                recovery_end = None
                job = await self._staged_save(job, root, state)
                if self._staged_stopped(job):
                    return job
        if cursor >= count:
            if merging:
                state.update(phase="selection", selection_cursor=0)
                job = await self._staged_save(job, root, state)
                return await self._staged_next(job, root, state)
            return await self._staged_selection_complete(job, root, state)
        records = []
        folder = root / "catalog" / "method" if merging else root / "proposals"
        stage = "merge_index" if merging else "plan"
        record_indexes = []
        direct = not merging and bool(state.get("direct_workflow_protocol"))
        limit = MAX_DIRECT_METHODS if direct else 8
        if direct and state.get("selection_recovery_protocol"):
            recovery_limit = state.get("direct_recovery_limit", MAX_DIRECT_METHODS)
            if type(recovery_limit) is not int or not 1 <= recovery_limit <= MAX_DIRECT_METHODS:
                raise ValueError("Frozen selection recovery limit is outside its bounded method assignment")
            limit = min(limit, recovery_limit)
        end = min(cursor + limit, count, state.get("direct_window_end", count)) if direct else min(cursor + 8, count)
        if local_recovery and recovery_end is not None:
            end = min(end, recovery_end)
        for position in range(cursor, end):
            index = position
            if merging and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                from .workflow_planning import workflow_method_index

                index = await asyncio.to_thread(workflow_method_index, root, position, count)
            record = (await asyncio.to_thread(_direct_record, root, index) if direct
                      else await asyncio.to_thread(_read, folder / f"{index:08d}.json"))
            tentative = records + [record]
            controls = {
                "items": [self._staged_model_item(row) for row in tentative],
                "input_ids": list(dict.fromkeys(row["unit_id"] for row in tentative)),
                "allowed_source_refs": list(
                    dict.fromkeys(ref for row in tentative for ref in row["item"]["source_refs"])
                ),
                "planning_version": 2,
                "complete_source_available": True,
                "readonly_context_rule": "Only items are disposition inputs. Related facts/gaps are readonly; "
                "never reference their IDs in derived_from/resolved. For this method-only window resolved must be [].",
            }
            try:
                planning.build_stage_packet(
                    stage,
                    "merge.json" if merging else "selection.json",
                    controls=controls,
                    language_policy=state["language_policy"],
                    # Reserve space for exact candidate authorities and bounded
                    # readonly notes before fixing the immutable assignment.
                    max_chars=22000 if direct else 32000,
                )
            except ValueError:
                if not records:
                    raise
                break
            records = tentative
            record_indexes.append(index)
        controls = {
            "items": [self._staged_model_item(row) for row in records],
            "input_ids": list(dict.fromkeys(row["unit_id"] for row in records)),
            "allowed_source_refs": list(dict.fromkeys(ref for row in records for ref in row["item"]["source_refs"])),
            "planning_version": 2,
            "complete_source_available": True,
            "readonly_context_rule": "Only items are disposition inputs. related_facts/provisional_gaps are readonly; "
            "never reference their IDs in derived_from/resolved. For this method-only window resolved must be [].",
        }
        if not merging:
            if direct:
                from .direct_workflow_planning import related_context

                controls.update(await asyncio.to_thread(related_context, root, state, records))
                controls["direct_workflow_protocol"] = state["direct_workflow_protocol"]
                controls["max_input_items"] = MAX_DIRECT_METHODS
            else:
                await self._staged_add_related_context(root, state, records, controls)
            if state.get("identity_protocol") == IDENTITY_PROTOCOL:
                await asyncio.to_thread(
                    _selection_known,
                    job,
                    root,
                    records,
                    controls,
                    refinement_enabled=state.get("candidate_refinement_protocol") == REFINEMENT_PROTOCOL,
                )
        if direct:
            compiler_folder = "aux-compilers" if stage in state.get("aux_compilers", {}) else "compilers"
            compiler = await asyncio.to_thread((root / compiler_folder / f"{stage}.md").read_text, encoding="utf-8")
            _, published = _package(compiler)
            packet_limit = min(32000, 48000 - len(published) - 3072)
            try:
                planning.build_stage_packet(stage, "selection.json", controls=controls,
                                            language_policy=state["language_policy"], max_chars=packet_limit)
            except ValueError:
                if len(records) <= 1:
                    raise ValueError("One complete method and its compiler cannot fit the bounded planning envelope")
                split_after = cursor + len(records) // 2
                await asyncio.to_thread(
                    _save, root / "direct-planning/input-splits" / f"{_sha(_json([cursor, controls]))[:24]}.json",
                    {"before": cursor, "after": cursor + len(records), "split_after": split_after,
                     "record_indexes": record_indexes, "packet_limit": packet_limit,
                     "compiler_chars": len(published), "reason": "complete planning envelope input budget"},
                )
                state["direct_window_end"] = split_after
                state["input_budget_splits"] = int(state.get("input_budget_splits", 0)) + 1
                job = await self._staged_save(job, root, state)
                return await self._staged_catalog_next(job, root, state)
        return await self._staged_submit(
            job,
            root,
            state,
            stage,
            "merge.json" if merging else "selection.json",
            [],
            controls,
            {
                "typed_planning": "merge" if merging else "selection",
                "record_indexes": record_indexes,
                "input_ids": controls["input_ids"],
                "before": cursor,
                "after": cursor + len(records),
            },
        )


    async def _staged_add_related_context(self, root: Path, state: dict, records: list[dict], controls: dict) -> None:
        files = {uri for row in records for uri in row["item"].get("source_files", [])}
        controls.update(related_facts=[], provisional_gaps=[], context_omitted=False)
        for kind, field in (("fact", "related_facts"), ("gap", "provisional_gaps")):
            for index in range(state["catalog_counts"][kind]):
                row = await asyncio.to_thread(_read, root / "catalog" / kind / f"{index:08d}.json")
                if not files.intersection(row["item"].get("source_files", [])):
                    continue
                item = self._staged_readonly_note(row)
                controls[field].append(item)
                if len(_json(controls)) > 26000:
                    controls[field].pop()
                    controls["context_omitted"] = True
                    break
        controls["context_rule"] = (
            "Unshown catalog content is still available in original source; omission is not a whole-file gap."
        )
        path = root / "plan.json"
        if path.exists():
            plan = await asyncio.to_thread(_read, path)
            controls["already_selected"] = []
            tasks = {row["item"]["task"] for row in records}
            for row in plan["skills"]:
                if not files.intersection(row["source_refs"]) and row["task"] not in tasks:
                    continue
                brief = {key: row[key] for key in ("name", "task", "trigger", "inputs", "outputs", "boundary")}
                controls["already_selected"].append(brief)
                if len(_json(controls)) > 27000:
                    controls["already_selected"].pop()
                    controls["context_omitted"] = True
                    break


    async def _staged_plan_next(self, job: dict, root: Path, state: dict) -> dict:
        if state["cursor"] >= state["input_unit_count"]:
            if state["unit_count"] >= state["input_unit_count"]:
                raise ValueError("分层能力规划未收敛；没有丢弃任何 Evidence，请查看阶段审计")
            state.update(layer=state["layer"] + 1, cursor=0, input_unit_count=state["unit_count"], unit_count=0)
            if state["layer"] > MAX_PLAN_DEPTH:
                raise ValueError("分层能力规划超过有限深度")
        units = []
        for index in range(state["cursor"], min(state["cursor"] + 8, state["input_unit_count"])):
            row = await asyncio.to_thread(_read, root / "units" / str(state["layer"] - 1) / f"{index:08d}.json")
            projected = {"id": row["id"], "value": _project(row["value"], row["id"])}
            tentative = [*units, {**row, "projected": projected}]
            ids = [item["id"] for item in tentative]
            refs = list(dict.fromkeys(reference for item in tentative for reference in item["bindings"]))
            try:
                planning.build_stage_packet(
                    "plan",
                    "plan.json",
                    controls={
                        "inputs": [item["projected"] for item in tentative],
                        "input_ids": ids,
                        "allowed_source_refs": refs,
                        "final_merge": state["cursor"] == 0 and len(tentative) == state["input_unit_count"],
                    },
                    language_policy=state["language_policy"],
                )
            except ValueError:
                if not units:
                    raise
                break
            units = tentative
        if len(units) == 1 and state["input_unit_count"] > 1 and state["cursor"] > 0:
            row = {key: value for key, value in units[0].items() if key != "projected"}
            await asyncio.to_thread(
                _save, root / "units" / str(state["layer"]) / f"{state['unit_count']:08d}.json", row
            )
            state["unit_count"] += 1
            state["cursor"] += 1
            job = await self._staged_save(job, root, state)
            return await self._staged_plan_next(job, root, state)
        if not units or (len(units) == 1 and state["input_unit_count"] > 1):
            raise ValueError("合法的紧凑规划单元不能同时容纳两个；拒绝循环或截断材料")
        ids = [row["id"] for row in units]
        refs = list(dict.fromkeys(reference for row in units for reference in row["bindings"]))
        context = {
            "unit_indexes": list(range(state["cursor"], state["cursor"] + len(units))),
            "input_ids": ids,
            "source_refs": refs,
            "after": state["cursor"] + len(units),
            "final_merge": state["cursor"] == 0 and len(units) == state["input_unit_count"],
        }
        return await self._staged_submit(
            job,
            root,
            state,
            "plan",
            "plan.json",
            [],
            {
                "inputs": [row["projected"] for row in units],
                "input_ids": ids,
                "allowed_source_refs": refs,
                "final_merge": context["final_merge"],
            },
            context,
        )


    async def _staged_candidate_next(self, job: dict, root: Path, state: dict) -> dict:
        if state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
            if not state.get("artifact_batches_complete"):
                return await self._staged_artifact_batch_next(job, root, state)
            while state["candidate_index"] < len(state["candidate_names"]):
                state["phase"] = "case"
                from .ov_artifact_batch_orchestration import _ledger

                name = state["candidate_names"][state["candidate_index"]]
                if (await asyncio.to_thread(_ledger, root, name)).get("reused"):
                    state["candidate_index"] += 1
                    state.update(case_index=0, case_group_attempt=0)
                    job = await self._staged_save(job, root, state)
                    continue
                break
        plan = await asyncio.to_thread(_read, root / "plan.json")
        names = state["candidate_names"]
        if state["candidate_index"] >= len(names):
            state["phase"] = "assemble"
            job = await self._staged_save(job, root, state)
            return await self._staged_assemble(job, root, state)
        name = names[state["candidate_index"]]
        candidate = next(row for row in plan["skills"] if row["name"] == name)
        scope = await asyncio.to_thread(_read, root / "scopes" / f"{name}.json")
        if state["phase"] == "case" and self._staged_cases_batched():
            return await self._staged_case_batch_next(job, root, state, candidate, scope)
        if state["phase"] == "skill_section":
            pending = state.get("writer_pending_ordinals", scope["ordinals"])
            if state["cursor"] >= len(pending):
                sections = (_read(root / "sections" / name / f"{i:08d}.json") for i in range(state["section_count"]))
                ids = await asyncio.to_thread(
                    lambda: [
                        _read(root / "chunk-metadata" / f"{ordinal:08d}.json")["id"] for ordinal in scope["ordinals"]
                    ]
                )
                try:
                    await asyncio.to_thread(
                        self._ov_skill_mining_repository().write_output_stream,
                        job["job_id"],
                        f"compiled_skill/{name}/SKILL.md",
                        planning.iter_skill_chunks(candidate, sections, expected_input_ids=ids),
                    )
                except planning.NoOperationalSkillSectionsError:
                    await self._staged_defer_candidate(
                        job,
                        root,
                        state,
                        name,
                        "完整关联原文范围未提供可执行的方法步骤"
                        if state["language_policy"].get("language") == "zh-CN"
                        else "No executable method steps were supported across the complete assigned source scope.",
                    )
                    job = await self._staged_save(job, root, state)
                    return await self._staged_candidate_next(job, root, state)
                state.pop("writer_pending_ordinals", None)
                state.update(phase="evaluation", cursor=0)
                job = await self._staged_save(job, root, state)
                return await self._staged_candidate_next(job, root, state)
            chunks, after = await asyncio.to_thread(_raw_group, root, pending, state["cursor"])
            path = "section.json"
            context = {
                "skill_name": name,
                "after": after,
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                "ordinals": [chunk["ordinal"] for chunk in chunks],
            }
        else:
            groups = await asyncio.to_thread(_method_groups, root, scope)
            primary = 0 if state["phase"] == "evaluation" else state["case_index"] * len(groups) // 12
            tried = int(state.get("case_group_attempt", 0)) if state["phase"] == "case" else 0
            group_index = (primary + tried) % len(groups)
            chunks = await asyncio.to_thread(_chunks, root, groups[group_index])
            context = {
                "skill_name": name,
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                "case_group_index": group_index,
                "case_group_count": len(groups),
            }
            path = "evaluation.md"
            if state["phase"] == "case":
                prior = await asyncio.to_thread(lambda: next(_prior_candidate_rows(root, name=name), None))
                prior_ids = prior["case_ids"] if prior else []
                context["case_id"] = (
                    prior_ids[state["case_index"]]
                    if len(prior_ids) == 12
                    else f"{name}-case-{state['case_index'] + 1:02d}"
                )
                path = "case.json"
        controls = {
            "candidate": {key: candidate[key] for key in ("name", "task", "boundary", "trigger")},
            "skill_name": name,
            "allowed_source_refs": context["source_refs"],
            "input_ids": context["input_ids"],
            "source_scope_rule": "This is a bounded assigned part of a larger, fully scanned original source. "
            "Unrelated sections can be skipped; absence in this part is not proof of a whole-method Evidence gap.",
        }
        if context.get("case_id"):
            controls["case_id"] = context["case_id"]
            controls["coverage_dimension"] = planning.CASE_DIMENSIONS[state["case_index"]]
            prior = [
                await asyncio.to_thread(_read, root / "cases" / name / f"{i:02d}.json")
                for i in range(state["case_index"])
            ]
            from .case_context import CASE_NARRATIVE_PROTOCOL, case_history_controls

            controls.update(case_history_controls(prior))
            state["case_narrative_generation_protocol"] = CASE_NARRATIVE_PROTOCOL
        return await self._staged_submit(job, root, state, state["phase"], path, chunks, controls, context)


    async def _advance_staged_round(self, job: dict, task: dict) -> dict:
        status = str(task.get("status") or "")
        if status in compile_admission.TERMINAL_NATIVE_STATES:
            await self._release_staged_compile_slot(job, str(task.get("task_id") or job.get("ov_task_id") or ""))
        root, state = await self._staged_load(job)
        current = state.get("current")
        if current and str(job.get("ov_task_id") or "") != current.get("task_id"):
            return await self._resume_staged_round(job)
        if self._staged_stopped(job):
            return await asyncio.to_thread(
                self._ov_skill_mining_repository().update_job, job["job_id"], status="stopped", finished_at=utc_now()
            )
        if state.get("complete"):
            return await self._complete_ov_skill_round(job)
        if not current:
            return await self._resume_staged_round(job)
        if status not in {"completed", "succeeded", "failed", "cancelled"}:
            return job
        if status == "cancelled":
            return await self._staged_save(job, root, state, status="stopped", finished_at=utc_now())
        if current["stage"] == CASE_BATCH_STAGE:
            return await self._advance_case_batch(job, root, state, task)
        if current["stage"] == EXTRACT_BATCH_STAGE:
            return await self._advance_extract_batch(job, root, state, task)
        if current["stage"] == WORKFLOW_BATCH_STAGE:
            return await self._advance_workflow_batch(job, root, state, task)
        if current["stage"] == MERGE_BATCH_STAGE:
            return await self._advance_merge_batch(job, root, state, task)
        if current["stage"] == ARTIFACT_BATCH_STAGE:
            return await self._advance_artifact_batch(job, root, state, task)
        try:
            if status == "failed":
                error = task.get("error") or {}
                raise ValueError(str(error.get("message") if isinstance(error, dict) else error) or "阶段 Compile 失败")
            pipeline = (task.get("meta") or {}).get("pipeline") or {}
            warnings = [str(value) for value in pipeline.get("warnings") or []]
            warnings.extend(str(value) for value in (task.get("result") or {}).get("warnings") or [])
            if pipeline.get("errors") or any(
                "partial" in value.casefold() or "unfinished" in value.casefold() for value in warnings
            ):
                raise ValueError("阶段 Compile 报告部分处理或输入遗漏，不接受该产物")
            expected = current["output_uri"] + "/" + current["expected_path"]
            tree = await self._knowledge_tree(job["account"], current["output_uri"])
            files = [
                item["uri"]
                for item in tree.get("entries") or []
                if not item.get("is_dir") and not item["uri"].rsplit("/", 1)[-1].startswith(".")
            ]
            if set(files) != {expected} or len(files) != 1:
                raise ValueError("已结束的阶段未生成精确唯一的预定文件")
            result = await self._knowledge_request(
                job["account"],
                "GET",
                "/api/v1/content/read",
                params={"uri": expected, "offset": 0, "limit": -1, "raw": "true"},
                timeout=20.0,
            )
            content = result if isinstance(result, str) else result.get("content", result.get("text", ""))
            raw_limit = MAX_EXTRACT_RAW_CHARS if current["stage"] == "extract" else planning.MAX_OUTPUT_CHARS
            if not isinstance(content, str) or not content.strip() or len(content) > raw_limit:
                if isinstance(content, str) and content.strip() and (
                    current["context"].get("typed_planning") == "selection"
                ):
                    from .direct_workflow_planning import SelectionOutputValidationError

                    await asyncio.to_thread(_write, root / "children" / current["child"] / "result.txt", content)
                    raise SelectionOutputValidationError(f"Planning output exceeds the {raw_limit}-character budget")
                raise ValueError(f"阶段产物缺失或超过 {raw_limit} 字符，不接受截断结果")
            await asyncio.to_thread(_write, root / "children" / current["child"] / "result.txt", content)
            try:
                value = json.loads(content) if current["stage"] != "evaluation" else content
            except json.JSONDecodeError as exc:
                if current["context"].get("typed_planning") == "selection":
                    from .direct_workflow_planning import SelectionOutputValidationError

                    raise SelectionOutputValidationError("Planning output must be complete valid JSON") from exc
                raise
            if (state.get("direct_workflow_protocol") and current["context"].get("typed_planning") == "selection"
                    and isinstance(value, dict)
                    and value.get("schema_version") == "skill-miner.selection.capacity.v1"):
                from .direct_workflow_planning import validate_capacity

                packet, alias_manifest = await asyncio.to_thread(_selection_packet, root, state, current)
                capacity_limit = _capacity_reason_limit(state)
                value, output_limit = _selection_alias_result(value, state, alias_manifest)
                controls = packet["derived_controls"]
                validate_capacity(value, input_ids=controls["input_ids"],
                                  source_refs=controls["allowed_source_refs"], max_reason_chars=capacity_limit,
                                  _max_output_chars=output_limit)
                context = current["context"]
                before, after = state["selection_cursor"], context["after"]
                if context.get("before") != before or after - before <= 1:
                    raise ValueError("A capacity split requires at least two exact unprocessed methods")
                split_after = before + (after - before) // 2
                await asyncio.to_thread(_save, root / "direct-planning/splits" / f"{current['child']}.json",
                                        {"before": before, "after": after, "split_after": split_after,
                                         "record_indexes": context["record_indexes"], "capacity": value,
                                         "packet_sha256": current["packet_sha256"], "raw_sha256": _sha(content)})
                state["direct_window_end"] = split_after
                state["capacity_splits"] = int(state.get("capacity_splits", 0)) + 1
                state["current"] = None
                job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="",
                                              ov_task_id="")
                return await self._staged_next(job, root, state)
            await self._staged_accept(job, root, state, current, value)
        except (ValueError, KeyError, TypeError) as exc:
            from .direct_workflow_planning import FrozenPlanningIntegrityError, SelectionOutputValidationError

            if isinstance(exc, FrozenPlanningIntegrityError):
                return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
            return await self._staged_retry_output(
                job, root, state, str(exc), selection_output=isinstance(exc, SelectionOutputValidationError),
            )
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 404:
                return await self._staged_retry_output(job, root, state, "已完成阶段的预定产物不存在")
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                return await self._staged_save(
                    job, root, state, status="failed", error=f"阶段产物读取失败：{exc.detail}", finished_at=utc_now()
                )
            job = await asyncio.to_thread(
                self._ov_skill_mining_repository().update_job, job["job_id"], status="running", error="", finished_at=""
            )
            return await self._round_submission_retry(self._ov_skill_mining_repository(), job, exc)
        state["completed_tasks"] += 1
        state["current"] = None
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)


    async def _staged_accept(self, job: dict, root: Path, state: dict, current: dict, value) -> None:
        context, phase = current["context"], current["stage"]
        if phase in {"evidence_review", "evidence_review_quotes"}:
            return await self._staged_evidence_review_accept(job, root, state, current, value)
        if phase == "identity_group":
            return await self._staged_identity_accept(job, root, state, current, value)
        if context.get("typed_planning"):
            return await self._staged_accept_typed(job, root, state, current, value)
        if planning.is_deferred(value) and phase in {"skill_section", "case"}:
            value = planning.validate_deferred(value, source_refs=context["source_refs"])
            if state.get("planning_version") == 2 and phase == "skill_section":
                value = {
                    "schema_version": planning.SECTION_SKIP_SCHEMA,
                    "input_ids": context["input_ids"],
                    "reason": value["reason"],
                }
                return await self._staged_accept_section(root, state, context, value)
            if phase == "case" and int(state.get("case_group_attempt", 0)) + 1 < context.get("case_group_count", 1):
                await asyncio.to_thread(
                    _save,
                    root
                    / "case-group-deferrals"
                    / context["skill_name"]
                    / f"{state['case_index']:02d}-{context['case_group_index']:08d}.json",
                    value,
                )
                state["case_group_attempt"] = int(state.get("case_group_attempt", 0)) + 1
                state["estimated_tasks"] += 1
                return
            await self._staged_defer_candidate(job, root, state, context["skill_name"], value["reason"])
            return
        if phase == "extract":
            packet = await asyncio.to_thread(_read, root / current["packet_file"])
            if packet["input_ids"] != context["input_ids"]:
                raise ValueError("Frozen extract assignment does not match the durable chunk context")
            value = _canonical_extract_references(
                value,
                input_ids=packet["input_ids"],
                source_refs=context["source_refs"],
                reference_map=packet["derived_controls"]["reference_map"],
                evidence=packet["evidence"],
            )
            value = planning.validate_index(value, input_ids=context["input_ids"], source_refs=context["source_refs"],
                                            routing=state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL)
            parents = dict(zip(context["source_refs"], [[ordinal] for ordinal in context["ordinals"]]))
            unit = _bind_unit(value, parents, current["child"])
            await asyncio.to_thread(_save, root / "units" / "0" / f"{state['unit_count']:08d}.json", unit)
            state["unit_count"] += 1
            state["cursor"] = context["after"]
        elif phase == "plan":
            value = planning.validate_plan(value, source_refs=context["source_refs"], input_ids=context["input_ids"])
            units = [
                await asyncio.to_thread(_read, root / "units" / str(state["layer"] - 1) / f"{i:08d}.json")
                for i in context["unit_indexes"]
            ]
            parents = {}
            for row in units:
                for alias, selected in row["bindings"].items():
                    if alias in parents and parents[alias] != selected:
                        raise ValueError("规划范围别名的本地谱系不一致")
                    parents[alias] = selected
            unit = _bind_unit(value, parents, current["child"])
            await asyncio.to_thread(
                _save, root / "units" / str(state["layer"]) / f"{state['unit_count']:08d}.json", unit
            )
            state["unit_count"] += 1
            state["cursor"] = context["after"]
            if context["final_merge"]:
                allowed = []
                by_alias = parents
                for row in value["skills"] + value["deferred"]:
                    selected = sorted({ordinal for alias in row["source_refs"] for ordinal in by_alias[alias]})
                    raw = await asyncio.to_thread(_chunk_metadata, root, selected)
                    row["source_refs"] = list(dict.fromkeys(chunk["source_uri"] for chunk in raw))
                    allowed.extend(row["source_refs"])
                    if row in value["skills"]:
                        await asyncio.to_thread(_save, root / "scopes" / f"{row['name']}.json", {"ordinals": selected})
                value.pop("input_ids", None)
                planning.validate_plan(value, source_refs=list(dict.fromkeys(allowed)), bounded=False)
                await asyncio.to_thread(_save, root / "plan.json", value)
                groups = sum(
                    [
                        await asyncio.to_thread(
                            _group_count,
                            root,
                            (await asyncio.to_thread(_read, root / "scopes" / f"{row['name']}.json"))["ordinals"],
                        )
                        for row in value["skills"]
                    ]
                )
                state.update(
                    phase="skill_section",
                    candidate_names=[row["name"] for row in value["skills"]],
                    candidate_index=0,
                    cursor=0,
                    section_count=0,
                    case_index=0,
                    estimated_tasks=state["completed_tasks"] + 2 + len(value["skills"]) * 13 + groups,
                )
        elif phase == "skill_section":
            await self._staged_accept_section(root, state, context, value)
        elif phase == "evaluation":
            validate_artifact_headings(value, state["language_policy"])
            await asyncio.to_thread(
                self._ov_skill_mining_repository().write_output,
                job["job_id"],
                f"compiled_skill/{context['skill_name']}/EVALUATION.md",
                value,
            )
            state.update(phase="case", case_index=0, case_group_attempt=0)
        elif phase == "case":
            value = planning.validate_case(
                value, case_id=context["case_id"], skill_name=context["skill_name"], source_refs=context["source_refs"]
            )
            prior = [
                await asyncio.to_thread(_read, root / "cases" / context["skill_name"] / f"{i:02d}.json")
                for i in range(state["case_index"])
            ]
            query = planning.normalize_case_query(value["query"])
            if any(planning.normalize_case_query(row["query"]) == query for row in prior):
                raise ValueError("当前案例 query 与已有案例重复；请按原 Evidence 生成独立场景，无法支持时明确暂缓")
            await asyncio.to_thread(
                _save, root / "cases" / context["skill_name"] / f"{state['case_index']:02d}.json", value
            )
            state["case_index"] += 1
            state["case_group_attempt"] = 0
            if state["case_index"] == 12:
                state.pop("writer_pending_ordinals", None)
                state.update(
                    phase="skill_section",
                    candidate_index=state["candidate_index"] + 1,
                    cursor=0,
                    section_count=0,
                    case_index=0,
                )
        else:
            raise ValueError("未知的阶段产物")
