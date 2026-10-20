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


class StagedPart2Mixin:
    async def _staged_accept_section(self, root: Path, state: dict, context: dict, value) -> None:
        validator = (
            planning.validate_section_skip
            if isinstance(value, dict) and value.get("schema_version") == planning.SECTION_SKIP_SCHEMA
            else planning.validate_section
        )
        value = validator(value, input_ids=context["input_ids"])
        if value.get("schema_version") == planning.SECTION_SCHEMA:
            validate_artifact_headings(value["content"], state["language_policy"], title=value["title"])
        name = context["skill_name"]
        await asyncio.to_thread(_save, root / "sections" / name / f"{state['section_count']:08d}.json", value)
        await asyncio.to_thread(
            _save,
            root / "section-groups" / name / f"{state['section_count']:08d}.json",
            {"ordinals": context["ordinals"]},
        )
        state["section_count"] += 1
        state["cursor"] = context["after"]


    async def _staged_split_invalid_selection(self, job: dict, root: Path, state: dict, error: str) -> dict:
        """Retry a smaller existing window, accepting none of the rejected model dispositions."""
        current = state["current"]
        context = current["context"]
        before, after = state["selection_cursor"], context["after"]
        indexes = context["record_indexes"]
        if (context.get("before") != before or not isinstance(indexes, list)
                or indexes != list(range(before, after)) or not 1 < after - before <= 24):
            raise ValueError("Selection recovery must partition the exact unprocessed frozen assignment")
        packet, aliases = await asyncio.to_thread(_selection_packet, root, state, current)
        expected_sha = current["canonical_packet_sha256"] if aliases is not None else current["packet_sha256"]
        if _sha(_json(packet)) != expected_sha:
            raise ValueError("Frozen selection recovery packet SHA changed")
        records = [await asyncio.to_thread(_direct_record, root, index) for index in indexes]
        if ([row["item"]["item_id"] for row in records]
                != [row["item_id"] for row in packet["derived_controls"]["items"]]):
            raise ValueError("Selection recovery cannot change assigned methods or source lineage")
        split_after = before + (after - before) // 2
        from .direct_workflow_planning import LOCAL_SELECTION_RECOVERY_PROTOCOL

        local_recovery = state["selection_recovery_protocol"] == LOCAL_SELECTION_RECOVERY_PROTOCOL
        recovery_end = state.get("direct_recovery_end", after) if local_recovery else None
        if local_recovery and (
            type(recovery_end) is not int or not after <= recovery_end <= state["proposal_count"]
        ):
            raise ValueError("Local selection recovery cannot change its original method window")
        content = await asyncio.to_thread((root / "children" / current["child"] / "result.txt").read_text,
                                         encoding="utf-8")
        await asyncio.to_thread(
            _save, root / "direct-planning/validation-splits" / f"{current['child']}.json",
            {"protocol": state["selection_recovery_protocol"], "before": before, "after": after,
             "split_after": split_after, "record_indexes": indexes, "attempt": current["attempt"],
             "error": error[:1000], "planning_coverage": context.get("planning_coverage"),
             "packet_sha256": current["packet_sha256"], "raw_sha256": _sha(content),
             "accepted_dispositions": 0, **({"recovery_end": recovery_end} if local_recovery else {})},
        )
        state["direct_window_end"] = split_after
        state["direct_recovery_limit"] = min(state.get("direct_recovery_limit", 24), split_after - before)
        if local_recovery:
            state["direct_recovery_end"] = recovery_end
        state["validation_splits"] = int(state.get("validation_splits", 0)) + 1
        state["current"] = None
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)


    async def _staged_retry_output(
        self, job: dict, root: Path, state: dict, error: str, *, selection_output: bool = False,
    ) -> dict:
        current = state["current"]
        from .direct_workflow_planning import LOCAL_SELECTION_RECOVERY_PROTOCOL, SELECTION_RECOVERY_PROTOCOL

        if (selection_output and state.get("selection_recovery_protocol") in (
            SELECTION_RECOVERY_PROTOCOL, LOCAL_SELECTION_RECOVERY_PROTOCOL,
        )
                and state.get("direct_workflow_protocol")
                and current["context"].get("typed_planning") == "selection"
                and current["attempt"] >= 1 and len(current["context"]["record_indexes"]) > 1):
            return await self._staged_split_invalid_selection(job, root, state, error)
        if current["attempt"] + 1 >= MAX_STAGE_ATTEMPTS:
            if current["stage"] == "evidence_review":
                upgraded = await self._staged_evidence_review_upgrade(job, root, state, error)
                if upgraded is not None:
                    return upgraded
            return await self._staged_save(
                job, root, state, status="failed", error=f"阶段产物校验失败：{error[:1000]}", finished_at=utc_now()
            )
        packet, _ = await asyncio.to_thread(_selection_packet, root, state, current)
        context = {**current["context"], "attempt": current["attempt"] + 1, "repair_error": error[:1000]}
        controls = packet["derived_controls"]
        if (state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL
                and context.get("typed_planning") == "selection"):
            from .selection_conflicts import repair_selection_controls

            compiler = await asyncio.to_thread((root / current["compiler_file"]).read_text, encoding="utf-8")
            _, published = _package(compiler)
            controls = await asyncio.to_thread(
                repair_selection_controls, job, root, controls, context, language_policy=state["language_policy"],
                max_packet_chars=min(32000, 48000 - len(published) - 3072),
            )
        return await self._staged_submit(
            job,
            root,
            state,
            current["stage"],
            current["expected_path"],
            packet["evidence"],
            controls,
            context,
        )


    async def _staged_defer_candidate(self, job: dict, root: Path, state: dict, name: str, reason: str) -> None:
        plan = await asyncio.to_thread(_read, root / "plan.json")
        marker = root / "deferred-candidates" / f"{name}.json"
        if marker.is_file() and (await asyncio.to_thread(_read, marker)).get("status") != "reopened":
            deferred = await asyncio.to_thread(_read, marker)
        else:
            candidate = next((row for row in plan["skills"] if row["name"] == name), None)
            if candidate is None:
                scope = await asyncio.to_thread(_read, root / "scopes" / f"{name}.json")
                values = await asyncio.to_thread(_chunk_metadata, root, scope["ordinals"])
                refs = list(dict.fromkeys(row["source_uri"] for row in values))
            else:
                refs = candidate["source_refs"]
            deferred = {"reason": f"{name}: {reason}", "source_refs": refs}
            await asyncio.to_thread(_save, marker, deferred)
        plan["skills"] = [row for row in plan["skills"] if row["name"] != name]
        if deferred not in plan["deferred"]:
            plan["deferred"].append(deferred)
        await asyncio.to_thread(_save, root / "plan.json", plan)
        workspace = self._ov_skill_mining_repository().workspace(job)
        path = workspace / "compiled_skill" / name
        if path.exists():
            # Remove it from active output without discarding the generated method and evaluation.
            archive = workspace / "excluded_skill" / name / _sha(_json(deferred))[:24]
            if not archive.exists():
                await asyncio.to_thread(archive.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.move, str(path), str(archive))
            else:
                for existing in path.rglob("*"):
                    archived = archive / existing.relative_to(path)
                    if existing.is_file() and (
                        not archived.is_file() or existing.read_bytes() != archived.read_bytes()
                    ):
                        raise ValueError("Excluded candidate archive cannot replace different generated files")
                await asyncio.to_thread(shutil.rmtree, path)
        state["candidate_names"] = [value for value in state["candidate_names"] if value != name]
        state.pop("writer_pending_ordinals", None)
        state.update(phase="skill_section", cursor=0, section_count=0, case_index=0)


    async def _staged_accept_typed(self, job: dict, root: Path, state: dict, current: dict, value) -> None:
        context = current["context"]
        merging = context["typed_planning"] == "merge"
        folder = root / "catalog" / "method" if merging else root / "proposals"
        records = [
            await asyncio.to_thread(_direct_record, root, i)
            if not merging and state.get("direct_workflow_protocol")
            else await asyncio.to_thread(_read, folder / f"{i:08d}.json")
            for i in context["record_indexes"]
        ]
        items = [record["item"] for record in records]
        packet, alias_manifest = await asyncio.to_thread(_selection_packet, root, state, current)
        value, output_limit = _selection_alias_result(value, state, alias_manifest)
        controls = packet["derived_controls"]
        if controls["input_ids"] != context["input_ids"] or [row["item_id"] for row in controls["items"]] != [
            row["item_id"] for row in items
        ]:
            from .direct_workflow_planning import FrozenPlanningIntegrityError

            raise FrozenPlanningIntegrityError("Frozen planning assignment does not match the durable method context")
        from .direct_workflow_planning import SelectionOutputValidationError

        if not merging and state.get("selection_recovery_protocol"):
            diagnostics = planning.planning_coverage_diagnostics(value, [row["item_id"] for row in items])
            if diagnostics:
                context["planning_coverage"] = diagnostics
            else:
                context.pop("planning_coverage", None)
        try:
            value = _canonical_planning_header(
                value, input_ids=context["input_ids"], item_ids=[row["item_id"] for row in items]
            )
        except ValueError as exc:
            if not merging:
                raise SelectionOutputValidationError(str(exc)) from exc
            raise
        if not merging and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
            from .workflow_planning import canonical_selection_reuse

            value = canonical_selection_reuse(value, controls)
            if state.get("compact_planning_protocol") == "te-compact-planning.v1":
                from .workflow_planning import coalesce_exact_authority_reuse

                try:
                    value, audit = coalesce_exact_authority_reuse(value, controls, _max_output_chars=output_limit)
                except (ValueError, KeyError, TypeError) as exc:
                    raise SelectionOutputValidationError(str(exc)) from exc
                audit_path = f"children/{current['child']}/reuse-coalescing.json"
                await asyncio.to_thread(_save, root / audit_path, audit)
                current.update(reuse_coalescing_file=audit_path, reuse_coalescing_sha256=_sha(_json(audit)))
                await asyncio.to_thread(_save, root / "children" / current["child"] / "metadata.json", current)
        if merging:
            checked = planning.validate_merge(value, items=items, input_ids=context["input_ids"])
        else:
            if state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
                from .selection_conflicts import collect_selection_conflicts

                context["selection_conflicts"] = await asyncio.to_thread(
                    collect_selection_conflicts, job, root, value, controls,
                    previous=context.get("selection_conflicts", []),
                )
            known = None
            refinement_enabled = controls.get("selection_schema") == planning.SELECTION_SCHEMA_V3
            if refinement_enabled and state.get("candidate_refinement_protocol") != REFINEMENT_PROTOCOL:
                raise ValueError("Frozen selection did not authorize business metadata refinement")
            if controls.get("selection_schema") in {planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3}:
                known = {row["candidate_id"]: row for row in controls["known_candidates"]}
            try:
                checked = planning.validate_selection(
                    value,
                    items=items,
                    input_ids=context["input_ids"],
                    known_candidates=known,
                    refinement_enabled=refinement_enabled,
                    max_input_items=24 if state.get("direct_workflow_protocol") else 8,
                    _max_output_chars=output_limit,
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise SelectionOutputValidationError(str(exc)) from exc
            refinement_journal = (
                await asyncio.to_thread(
                    prepare_refinements, root, job, current, value, checked, records, controls["known_candidates"],
                    state=state,
                )
                if refinement_enabled
                else None
            )
        parents = {}
        by_item = {row["item"]["item_id"]: row for row in records}
        for row in records:
            for alias, selected in row["bindings"].items():
                if alias in parents and parents[alias] != selected:
                    raise ValueError("逐项规划范围的本地谱系不一致")
                parents[alias] = selected
        if merging:
            unit = _bind_unit(checked, parents, current["child"])
            files = {}
            for alias, ordinals in unit["bindings"].items():
                metadata = await asyncio.to_thread(_chunk_metadata, root, ordinals)
                files[alias] = list(dict.fromkeys(row["source_uri"] for row in metadata))
            for proposed, original in zip(planning.planning_items([unit], source_files=files), checked["methods"]):
                selected = sorted({ordinal for ref in proposed["source_refs"] for ordinal in unit["bindings"][ref]})
                record = {
                    "item": proposed,
                    "unit_id": unit["id"],
                    "ordinals": selected,
                    "bindings": {ref: unit["bindings"][ref] for ref in proposed["source_refs"]},
                    "leaf_items": list(
                        dict.fromkeys(
                            leaf for identity in original["derived_from"] for leaf in by_item[identity]["leaf_items"]
                        )
                    ),
                }
                await asyncio.to_thread(_save, root / "proposals" / f"{state['proposal_count']:08d}.json", record)
                state["proposal_count"] += 1
            state["cursor"] = context["after"]
        else:
            plan_path = root / "plan.json"
            plan = (
                await asyncio.to_thread(_read, plan_path)
                if plan_path.exists()
                else {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []}
            )
            # Reject the entire selection before changing any scope file. A later
            # conflicting name must not leave partially accepted lineage behind.
            identities = {row["name"]: row for row in plan["skills"]}
            refined_names = {row["name"] for row in refinement_journal["changes"]} if refinement_journal else set()
            for row in checked["skills"]:
                previous = identities.get(row["name"])
                if previous and any(
                    previous[field] != row[field] for field in ("task", "trigger", "inputs", "outputs", "boundary")
                ) and row["name"] not in refined_names:
                    raise SelectionOutputValidationError(
                        f"候选名称 {row['name']} 已用于不同方法；请明确合并或使用独立名称"
                    )
                identities[row["name"]] = row
            if refinement_journal:
                await asyncio.to_thread(commit_refinements, root, refinement_journal)
            for row in checked["skills"]:
                old_scope = None
                selected = sorted({ordinal for ref in row["source_refs"] for ordinal in parents[ref]})
                metadata = await asyncio.to_thread(_chunk_metadata, root, selected)
                uris = list(dict.fromkeys(chunk["source_uri"] for chunk in metadata))
                full = []
                for uri in uris:
                    full.extend(
                        (await asyncio.to_thread(_read, root / "source-files" / (_sha(uri) + ".json")))["ordinals"]
                    )
                canonical = {key: item for key, item in row.items() if key not in {"derived_from", "source_refs"}}
                canonical["source_refs"] = uris
                previous = next((item for item in plan["skills"] if item["name"] == row["name"]), None)
                if previous:
                    for field in ("task", "trigger", "inputs", "outputs", "boundary"):
                        if previous[field] != canonical[field] and row["name"] not in refined_names:
                            raise SelectionOutputValidationError(
                                f"候选名称 {row['name']} 已用于不同方法；请基于已选方法明确合并或使用独立名称"
                            )
                    if row["name"] in refined_names:
                        previous.update({field: canonical[field] for field in BUSINESS_FIELDS})
                    previous["source_refs"] = list(dict.fromkeys(previous["source_refs"] + uris))
                    old_scope = await asyncio.to_thread(_read, root / "scopes" / f"{row['name']}.json")
                    selected = sorted(set(selected) | set(old_scope["method_ordinals"]))
                    full = sorted(set(full) | set(old_scope["ordinals"]))
                else:
                    plan["skills"].append(canonical)
                scope = {"ordinals": sorted(set(full)), "method_ordinals": selected}
                if old_scope and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                    from .candidate_refinement import merge_candidate_scopes

                    scope = merge_candidate_scopes(old_scope, scope)
                await asyncio.to_thread(
                    _save,
                    root / "scopes" / f"{row['name']}.json",
                    scope,
                )
            for row in checked["deferred"]:
                selected = sorted({ordinal for ref in row["source_refs"] for ordinal in parents[ref]})
                metadata = await asyncio.to_thread(_chunk_metadata, root, selected)
                gap = {
                    "reason": row["reason"],
                    "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in metadata)),
                }
                if gap not in plan["deferred"]:
                    plan["deferred"].append(gap)
            await asyncio.to_thread(_save, plan_path, plan)
            for field, target in (("skills", "candidate"), ("knowledge", "knowledge"), ("deferred", "deferred")):
                for row in checked[field]:
                    for identity in row["derived_from"]:
                        source = by_item[identity]
                        decision = {
                            "target": target,
                            "name": row.get("name", ""),
                            "reason": row.get("reason", row.get("rationale", "")),
                        }
                        for leaf in source["leaf_items"]:
                            await asyncio.to_thread(_save, root / "method-decisions" / f"{leaf}.json", decision)
            state["selection_cursor"] = context["after"]
            state["selection_count"] += 1
            state.pop("direct_window_end", None)
        await asyncio.to_thread(
            _save,
            root / "dispositions" / current["child"],
            {"kind": context["typed_planning"], "input_items": items, "result": checked},
        )


    async def _staged_selection_complete(self, job: dict, root: Path, state: dict) -> dict:
        path = root / "plan.json"
        if not path.exists():
            await asyncio.to_thread(_save, path, {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []})
        plan = await asyncio.to_thread(_read, path)
        refs = list(dict.fromkeys(ref for row in plan["skills"] + plan["deferred"] for ref in row["source_refs"]))
        planning.validate_plan(
            plan, source_refs=refs, bounded=False, require_nonempty_deferred=False, require_reference_coverage=False
        )
        groups = 0
        for row in plan["skills"]:
            scope = await asyncio.to_thread(_read, root / "scopes" / f"{row['name']}.json")
            groups += await asyncio.to_thread(_group_count, root, scope["ordinals"])
        state.update(
            phase="skill_section",
            candidate_names=[row["name"] for row in plan["skills"]],
            candidate_index=0,
            cursor=0,
            section_count=0,
            case_index=0,
            estimated_tasks=state["completed_tasks"] + 1 + len(plan["skills"]) * 13 + groups,
        )
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)


    async def _staged_assemble(self, job: dict, root: Path, state: dict) -> dict:
        repository = self._ov_skill_mining_repository()
        plan = await asyncio.to_thread(_read, root / "plan.json")
        if state.get("intent_selection_complete"):
            audit = await asyncio.to_thread(_read, root / "mining-intent/selection.json")
            target = state["mining_intent"]["target_count"]
            shortfall = max(0, (target or 0) - len(plan["skills"]))
            audit.update(final_count=len(plan["skills"]), final_shortfall=shortfall,
                         count_met=target is None or not shortfall)
            if shortfall:
                refs = [row["uri"] for row in await asyncio.to_thread(
                    lambda: [_read(path) for path in (root / "source-files").glob("*.json")]
                )]
                plan["deferred"] = [row for row in plan["deferred"]
                                    if not row["reason"].startswith("目标数量尚未满足：")]
                plan["deferred"].insert(0, {
                    "reason": f"目标数量尚未满足：请求 {target} 个完整 Skill，最终生成 {len(plan['skills'])} 个。"
                              "请复核实际数量或补充来源；不会硬凑、拆碎或强行归并。",
                    "source_refs": refs,
                })
                job = await self._staged_save(job, root, state, human_checkpoints_effective=True)
                await asyncio.to_thread(_save, root / "plan.json", plan)
            await asyncio.to_thread(_save, root / "mining-intent/selection.json", audit)
            await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/mining-intent.json", _json(audit))
        for candidate in plan["skills"]:
            name = candidate["name"]
            from .case_corrections import corrected_case

            cases = [await asyncio.to_thread(corrected_case, root, name, index) for index in range(12)]
            prior = await asyncio.to_thread(lambda: next(_prior_candidate_rows(root, name=name), None))
            prior_ids = prior["case_ids"] if prior else []
            dataset = planning.assemble_dataset(
                cases,
                skill_name=name,
                task=candidate["task"],
                identity=f"{job['job_id']}:{job['current_round']}:{name}",
                expected_case_ids=prior_ids if len(prior_ids) == 12 else None,
            )
            await asyncio.to_thread(
                repository.write_output, job["job_id"], f"compiled_skill/{name}/benchmark.json", _json(dataset)
            )
            await asyncio.to_thread(
                repository.write_output, job["job_id"], f"compiled_skill/{name}/BENCHMARK.md", dataset_markdown(dataset)
            )
        chinese = state["language_policy"].get("language") == "zh-CN"
        if state.get("planning_version") == 2:
            await self._staged_disposition_views(job, root, state, chinese)
        questions = []
        for index, row in enumerate(plan["deferred"][:50]):
            refs, reason = row["source_refs"], row["reason"]
            if len(refs) > 50:
                refs = [job["source_snapshot"], *refs[:49]]
                reason += (
                    " 精确全量范围见能力拆分计划及来源分片清单；所列文件仅为代表。"
                    if chinese
                    else " The complete scope is in the capability plan and source manifest; "
                    "listed files are representative."
                )
            questions.append(
                {
                    "qid": f"staged-gap-{index + 1}",
                    "question": ("请补充以下缺口的 Evidence：" if chinese else "Please supply Evidence for this gap: ")
                    + row["reason"],
                    "reason": reason,
                    "source_refs": refs,
                    "required": True,
                }
            )
        gaps = "# Evidence 缺口\n\n" if chinese else "# Evidence gaps\n\n"
        gaps += "\n\n".join(row["reason"] for row in plan["deferred"]) or (
            "未报告暂缓项。" if chinese else "No deferred items were reported."
        )
        evidence = "# Evidence 来源映射\n\n" if chinese else "# Evidence source mapping\n\n"
        evidence += "\n\n".join(row["task"] + "\n" + "\n".join(row["source_refs"]) for row in plan["skills"])
        if not plan["skills"]:
            evidence += "\n".join(ref for row in plan["deferred"] for ref in row["source_refs"])
            if state.get("planning_version") == 2:
                evidence += (
                    "\n\n所有原文已逐片段扫描；事实信息与方法去向详见 method-dispositions.md。\n"
                    if chinese
                    else "\n\nAll original source ranges were scanned. See method-dispositions.md "
                    "for factual information and each method disposition.\n"
                ) + job["source_snapshot"]

        for path, content in {
            "analysis/capability-plan.json": _json(plan),
            "analysis/evidence-map.md": evidence,
            "analysis/gaps.md": gaps,
            "analysis/review.json": _json({"schema_version": "skill-miner.review.v1", "questions": questions}),
        }.items():
            await asyncio.to_thread(repository.write_output, job["job_id"], path, content)
        job = await asyncio.to_thread(repository.finalize_materialized, job["job_id"])
        state["complete"] = True
        state["phase"] = "assemble"
        job = await self._staged_save(job, root, state, error="", status="running")
        return await self._complete_ov_skill_round(job)


    async def _staged_disposition_views(self, job: dict, root: Path, state: dict, chinese: bool) -> None:
        """Expose bounded audit pages while the complete lineage remains in the private journal."""
        repository = self._ov_skill_mining_repository()
        title = "方法与事实处置清单" if chinese else "Method and fact dispositions"
        pages, rows, counters = [], [], {"candidate": 0, "knowledge": 0, "deferred": 0, "provisional_gap": 0}

        async def flush():
            if not rows:
                return
            index = len(pages) + 1
            path = f"analysis/dispositions/page-{index:04d}"
            text = f"# {title} {index}\n\n"
            for row in rows:
                label = "条目 " + row["item_id"] if chinese else row["item_id"]
                text += f"## {label}\n\n{row['proposal']}\n\n{row['disposition']}\n\n"
                if row["candidate_name"]:
                    text += row["candidate_name"] + "\n\n"
                text += row["reason"] + "\n\n" + "\n".join(row["source_refs"]) + "\n\n"
                if row["source_refs_are_representative"]:
                    text += (
                        "完整精确范围保存在当前轮次的逐项处置账目与来源清单；以上为代表来源。\n\n"
                        if chinese
                        else "Exact complete scope is retained in this round's disposition journal "
                        "and source manifest; the listed references are representative.\n\n"
                    )
            await asyncio.to_thread(
                repository.write_output,
                job["job_id"],
                path + ".json",
                _json({"schema_version": "skill-miner.dispositions.v1", "items": rows}),
            )
            await asyncio.to_thread(repository.write_output, job["job_id"], path + ".md", text)
            pages.append(path.removeprefix("analysis/") + ".md")
            rows.clear()

        aliases_path = root / "identity" / "aliases.json"
        aliases = await asyncio.to_thread(_read, aliases_path) if aliases_path.is_file() else {}
        for kind in ("method", "fact", "gap"):
            for index in range(state["catalog_counts"][kind]):
                record = await asyncio.to_thread(_read, root / "catalog" / kind / f"{index:08d}.json")
                item = record["item"]
                if kind == "method":
                    decision = await asyncio.to_thread(_read, root / "method-decisions" / f"{item['item_id']}.json")
                    original_path = root / "identity" / "original-decisions" / f"{item['item_id']}.json"
                    original = await asyncio.to_thread(_read, original_path) if original_path.is_file() else decision
                    original_name, original_target = original["name"], original["target"]
                    alias_name = decision["name"]
                    if alias_name in aliases:
                        decision = {**decision, "name": aliases[alias_name]["name"],
                                    "reason": decision["reason"] + " " + aliases[alias_name]["reason"]}
                    marker = root / "deferred-candidates" / f"{decision['name']}.json"
                    if decision["name"] and marker.is_file():
                        deferred = await asyncio.to_thread(_read, marker)
                        decision = {"target": "deferred", "name": decision["name"], "reason": deferred["reason"]}
                else:
                    decision = {
                        "target": "knowledge" if kind == "fact" else "provisional_gap",
                        "name": "",
                        "reason": (
                            "原文事实保留，不是独立可执行方法"
                            if chinese
                            else "Original factual information retained; it is not an executable method."
                        )
                        if kind == "fact"
                        else item["reason"],
                    }
                if kind != "method":
                    original_name, original_target = "", decision["target"]
                counters[decision["target"]] += 1
                refs = item.get("source_files") or []
                labels = (
                    {
                        "candidate": "候选方法（Skill Candidate）",
                        "knowledge": "事实信息（不要求补证）",
                        "deferred": "真实暂缓",
                        "provisional_gap": "片段级限制（不自动要求补证）",
                    }
                    if chinese
                    else {
                        "candidate": "Skill Candidate",
                        "knowledge": "Factual information",
                        "deferred": "Deferred",
                        "provisional_gap": "Provisional fragment limitation",
                    }
                )
                rows.append(
                    {
                        "item_id": item["item_id"],
                        "kind": kind,
                        "proposal": item.get("task", item.get("text", item.get("reason", ""))),
                        "disposition": labels[decision["target"]],
                        "target": decision["target"],
                        "candidate_name": decision["name"],
                        "original_candidate_name": original_name,
                        "original_target": original_target,
                        "reason": decision["reason"],
                        "source_refs": refs if len(refs) <= 8 else [job["source_snapshot"], *refs[:7]],
                        "source_refs_are_representative": len(refs) > 8,
                    }
                )
                if len(rows) == 64:
                    await flush()
        await flush()
        summary = f"# {title}\n\n" + _json(counters) + "\n\n"
        summary += (
            "逐原始提议记录去向；结构校验通过不代表业务事实正确或 True Replay 通过。\n\n"
            if chinese
            else "Each original proposal retains a disposition. Structural validation does not "
            "establish business correctness or a passed True Replay.\n\n"
        )
        summary += "\n".join(
            f"- [{'第 ' + str(index) + ' 页' if chinese else path}]({path})"
            for index, path in enumerate(pages, 1)
        )
        await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/method-dispositions.md", summary)
