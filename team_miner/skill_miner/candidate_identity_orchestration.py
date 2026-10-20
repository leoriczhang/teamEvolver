# ruff: noqa: F401
from __future__ import annotations

from .candidate_identity_orchestration_helpers import (
    BUSINESS_FIELDS,
    CONSOLIDATING_PARTITION_PROTOCOL,
    GROUP_RULE,
    IDENTITY_FIELDS,
    IDENTITY_GROUP_PROTOCOL_V2,
    IDENTITY_PROTOCOL,
    MAX_PARTITION_IDENTITIES,
    MAX_RETRIEVAL_PASSES,
    SUPPORTED_IDENTITY_PARTITION_PROTOCOLS,
    IdentityCatalog,
    Path,
    _accept_groups,
    _explicit_survivor,
    _fork_prior_collision,
    _freeze_group_protocol,
    _identity_controls,
    _identity_window,
    _io,
    _legacy_identity_window,
    _partition_assignment,
    _partition_level,
    _partition_window,
    _prior_authorities,
    _replay_group_journal,
    _seed_catalog,
    _selection_known,
    _split_audit,
    asyncio,
    candidate_id,
    copy,
    identity_brief,
    identity_version,
    json,
    latest_refinement,
    mapped_method_ordinals,
    mapped_previous_source_scope,
    merge_candidate_scopes,
    metadata_sha256,
    os,
    planning,
    render_identity_partition_compiler,
    render_single_stage_compiler,
    validate_identity_groups,
)


class CandidateIdentityMixin:
    async def _ensure_aux_compiler(self, root: Path, state: dict, stage: str, body: str):
        _, _, _, sha, write = _io()
        path = root / "aux-compilers" / (stage + ".md")
        hashes = state.setdefault("aux_compilers", {})
        if stage in hashes:
            frozen = await asyncio.to_thread(path.read_text, encoding="utf-8")
            if sha(frozen) != hashes[stage]:
                raise ValueError("Frozen auxiliary compiler changed")
            return
        if path.exists():
            body = await asyncio.to_thread(path.read_text, encoding="utf-8")
        else:
            await asyncio.to_thread(write, path, body)
        hashes[stage] = sha(body)

    async def _staged_identity_enter(self, job: dict, root: Path, state: dict):
        if state.get("current"):
            raise ValueError("Must recover and accept the frozen writer child before identity review")
        state["writer_resume"] = {key: copy.deepcopy(state.get(key)) for key in (
            "phase", "candidate_names", "candidate_index", "cursor", "section_count", "case_index",
            "case_group_attempt",
        )}
        state.update(phase="evidence_review", identity_protocol=IDENTITY_PROTOCOL)
        return await self._staged_save(job, root, state)

    async def _staged_identity_next(self, job: dict, root: Path, state: dict):
        _, read, save, _, _ = _io()
        await asyncio.to_thread(_seed_catalog, job, root, "identity", include_reviews=True)
        # Replaying a prepared transaction is idempotent, even after a late filesystem failure.
        for path in sorted((root / "identity" / "journals").glob("*.json")):
            if not path.with_suffix(".committed").is_file():
                await asyncio.to_thread(_replay_group_journal, root, path)
        partitioning = state.get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        if partitioning:
            base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
            compiler = render_identity_partition_compiler(
                base, protocol=state["identity_partition_protocol"]
            )
            await self._ensure_aux_compiler(root, state, "identity_group", compiler)
            from .ov_staged_orchestration import _package

            compiler = await asyncio.to_thread(
                (root / "aux-compilers" / "identity_group.md").read_text, encoding="utf-8"
            )
            published = _package(compiler)[1]
            packet_budget = min(32000, 48000 - len(published) - 3072 - 2000)
            if packet_budget < 1000:
                raise ValueError("The frozen identity compiler leaves no bounded business metadata budget")
            previous_budget = state.setdefault("identity_partition_packet_budget", packet_budget)
            if previous_budget != packet_budget:
                raise ValueError("Frozen identity partition compiler budget changed")
        if "identity_pass" not in state:
            state.update(identity_pass=0, identity_cursor=0)
            count = (await asyncio.to_thread(read, root / "identity" / "ready.json"))["count"]
            if not partitioning:
                state["estimated_tasks"] += count * MAX_RETRIEVAL_PASSES
        window = await asyncio.to_thread(_identity_window, root, state)
        if partitioning:
            estimated = sum(v["batch_count"] for v in state.get("identity_partition_levels", {}).values())
            previous = state.get("identity_partition_estimated_tasks", 0)
            state["estimated_tasks"] += max(0, estimated - previous)
            state["identity_partition_estimated_tasks"] = max(estimated, previous)
        if window is None:
            return await self._staged_identity_finish(job, root, state)
        rows, context = window
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if partitioning:
            compiler = render_identity_partition_compiler(base)
        elif _explicit_survivor(state):
            from .compile_strategy import render_workflow_identity_compiler

            compiler = render_workflow_identity_compiler(base)
        else:
            rule = GROUP_RULE
            from .workflow_planning import WORKFLOW_PLANNING_PROTOCOL, WORKFLOW_PLANNING_RULE
            if state.get("workflow_planning_protocol") == WORKFLOW_PLANNING_PROTOCOL:
                rule += "\n" + WORKFLOW_PLANNING_RULE
            compiler = render_single_stage_compiler(base, "identity_group", rule)
        await self._ensure_aux_compiler(root, state, "identity_group", compiler)
        await asyncio.to_thread(save, root / "state.json", state)
        return await self._staged_submit(job, root, state, "identity_group", "groups.json", [],
                                         _identity_controls(rows, state), context)

    async def _staged_identity_accept(self, job: dict, root: Path, state: dict, current: dict, value):
        if (current["context"].get("identity_partition_protocol")
                != state.get("identity_partition_protocol")):
            raise ValueError("Frozen identity partition protocol changed")
        await asyncio.to_thread(_accept_groups, root, current, value)
        state["identity_cursor"] = current["context"]["after"]
        if current["context"].get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS:
            state["identity_partition_cursor"] = current["context"]["after"]

    async def _staged_identity_finish(self, job: dict, root: Path, state: dict):
        _, read, save, _, _ = _io()
        plan = await asyncio.to_thread(read, root / "plan.json")
        aliases_path = root / "identity" / "aliases.json"
        aliases = await asyncio.to_thread(read, aliases_path) if aliases_path.exists() else {}
        resume = state["writer_resume"]
        old_names = resume.get("candidate_names") or []
        index = int(resume.get("candidate_index") or 0)
        old_name = old_names[index] if index < len(old_names) else ""
        names = [row["name"] for row in plan["skills"]]
        state.pop("writer_pending_ordinals", None)
        state.update(identity_review_complete=True, candidate_names=names, candidate_index=0,
                     phase="skill_section", cursor=0, section_count=0, case_index=0)
        # Already accepted output remains valid only under its surviving canonical identity.
        # Raw child outputs and alias sections remain archival even when regeneration is required.
        surviving_name = aliases.get(old_name, {}).get("name", old_name)
        if surviving_name in names:
            # Resume the accepted current identity first, then cover every other active identity.
            state["candidate_names"] = [surviving_name] + [name for name in names if name != surviving_name]
            if surviving_name != old_name:
                for folder in ("sections", "section-groups"):
                    for i in range(int(resume.get("section_count") or 0)):
                        value = await asyncio.to_thread(read, root / folder / old_name / f"{i:08d}.json")
                        await asyncio.to_thread(save, root / folder / surviving_name / f"{i:08d}.json", value)
            scope = await asyncio.to_thread(read, root / "scopes" / (surviving_name + ".json"))
            covered = set()
            for i in range(int(resume.get("section_count") or 0)):
                group = await asyncio.to_thread(read, root / "section-groups" / surviving_name / f"{i:08d}.json")
                covered.update(group["ordinals"])
            state["section_count"] = int(resume.get("section_count") or 0)
            if covered <= set(scope["ordinals"]):
                pending = [item for item in scope["ordinals"] if item not in covered]
                prefix = scope["ordinals"][:len(covered)]
                if set(prefix) == covered:
                    state["writer_pending_ordinals"] = scope["ordinals"]
                    state["cursor"] = len(covered)
                else:
                    state["writer_pending_ordinals"] = pending
                # Subsequent evaluation/Cases are regenerated when the canonical identity or scope changes.
                if not pending and old_name == surviving_name:
                    state.update(phase=resume["phase"], cursor=int(resume.get("cursor") or 0),
                                 case_index=int(resume.get("case_index") or 0),
                                 case_group_attempt=int(resume.get("case_group_attempt") or 0))
                    if state["phase"] != "skill_section":
                        state.pop("writer_pending_ordinals", None)
            else:
                state.update(section_count=0, cursor=0)
        from .ov_staged_orchestration import _group_count
        writer_work = 0
        for name in names:
            scope = await asyncio.to_thread(read, root / "scopes" / (name + ".json"))
            writer_work += 13 + await asyncio.to_thread(_group_count, root, scope["ordinals"])
        state["estimated_tasks"] = max(state["estimated_tasks"], state["completed_tasks"] + writer_work + 1)
        summary = {
            "protocol": IDENTITY_PROTOCOL, "retrieval_passes": MAX_RETRIEVAL_PASSES,
            "max_identities_per_call": 8, "semantic_pairs_exhaustive": False,
            "aliases": aliases, "selected_candidate_count": len(names),
            "raw_dispositions_preserved": True, "retrieval": "global disk-backed token/source inverted index",
            **({"review_policy": "fixed top-seven relations per exact candidate version",
                "candidate_version_receipts": True, "unchanged_collection_skips": True,
                "passes_scheduled": state.get("identity_passes_scheduled", 1)}
               if state.get("execution_protocol") == "te-mining-optimized.v1" else {}),
        }
        partitioning = state.get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        if partitioning:
            summary.update(
                partition_protocol=state["identity_partition_protocol"],
                max_identities_per_call=MAX_PARTITION_IDENTITIES,
                review_policy="nonoverlapping complete budget partitions; at most one cross-batch workflow layer",
                retrieval="workflow tokens only for the optional cross-batch layer; no source-overlap graph",
                partition_levels=state.get("identity_partition_levels", {}),
                candidate_version_receipts=True,
            )
            summary.pop("passes_scheduled", None)
        await asyncio.to_thread(save, root / "identity" / "summary.json", summary)
        repository = self._ov_skill_mining_repository()
        for alias in aliases:
            source = repository.workspace(job) / "compiled_skill" / alias
            if source.is_dir():
                destination = root / "identity" / "archived-output" / alias
                await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(os.replace, source, destination)
        report = {
            "protocol": IDENTITY_PROTOCOL, "retrieval_passes": MAX_RETRIEVAL_PASSES,
            "max_identities_per_call": 8, "semantic_pairs_exhaustive": False,
            "selected_candidate_count": len(names), "alias_count": len(aliases),
            "raw_dispositions_preserved": True,
            **({"review_policy": "fixed top-seven relations per exact candidate version",
                "candidate_version_receipts": True, "unchanged_collection_skips": True,
                "passes_scheduled": state.get("identity_passes_scheduled", 1)}
               if state.get("execution_protocol") == "te-mining-optimized.v1" else {}),
        }
        if partitioning:
            report.update({key: summary[key] for key in (
                "partition_protocol", "max_identities_per_call", "review_policy", "retrieval", "partition_levels",
            )})
            report.pop("passes_scheduled", None)
        await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/candidate-identities.json",
                                json.dumps(report, ensure_ascii=False))
        conflict_paths = sorted((root / "identity/name-conflicts").glob("*.json"))
        for index, path in enumerate(conflict_paths):
            await asyncio.to_thread(repository.write_output, job["job_id"],
                                    f"analysis/name-conflicts/{index + 1:04d}.json",
                                    json.dumps(await asyncio.to_thread(read, path), ensure_ascii=False))
        split_count = await asyncio.to_thread(lambda: sum(1 for _ in _split_audit(root)))
        if split_count:
            await asyncio.to_thread(repository.write_output_stream, job["job_id"],
                                    "analysis/identity-split-audit.jsonl", _split_audit(root))
        chinese = state["language_policy"].get("language") == "zh-CN"
        text = "# Candidate 身份复核\n\n" if chinese else "# Candidate identity review\n\n"
        if partitioning:
            text += (
                "按完整业务身份与实际预算审查不重叠集合，每个身份及版本在一层内只出现一次。"
                "跨批最多追加一层业务词摘要复核，不以共同来源反复扫描；未承诺穷尽所有语义组合。\n\n"
                if chinese else "Complete business identities are reviewed in nonoverlapping budget partitions, "
                "each identity and version once per layer. At most one additional workflow-based cross-batch "
                "layer is used; common sources do not trigger repeated scans. Semantic pairs are not exhaustive.\n\n"
            )
        else:
            text += (
            "从完整本地候选索引检索相似身份，最多两遍、每窗最多八个；未承诺穷尽所有语义组合。\n\n"
            if chinese else "Related identities are retrieved from the complete local catalog in at most two passes, "
            "with at most eight identities per window. This does not exhaust every semantic pair.\n\n"
        )
        if state.get("execution_protocol") == "te-mining-optimized.v1":
            text += (
                "每个候选版本检索最多七个相似身份；批次可覆盖多个候选。已复核的相同版本关系会复用回执，"
                "只有身份合并、业务字段或来源范围变化后才需要下一遍复核。未检索关系不代表已通过。\n\n"
                if chinese else "Each candidate version retrieves at most seven related identities. A batch may "
                "cover several anchors. Exact version receipts are reused; another pass is needed only after "
                "grouping, business metadata or source scope changes. Unretrieved relations are not approved.\n\n"
            )
        if split_count:
            text += (
                "含不同已接受业务修订的身份合并请求已保守拆开，保留各自身份与案例编号。"
                "详见 [请求与实际分组审计](identity-split-audit.jsonl)。\n\n"
                if chinese else "Grouping requests that would discard a different accepted business revision "
                "were conservatively split, preserving their identities and Case IDs. "
                "See [requested and effective groups](identity-split-audit.jsonl).\n\n"
            )
        text += (
            "原始来源、逐方法谱系及未改写的模型输出保存在本轮审计中。\n\n"
            if chinese else "Original sources, method lineage and unchanged model outputs "
            "remain in this round's audit.\n\n"
        )
        for index in range(0, len(aliases), 64):
            rows = list(aliases.items())[index:index + 64]
            page = f"analysis/identities/page-{index // 64 + 1:04d}.md"
            title = "# Candidate 身份合并原因\n\n" if chinese else "# Candidate identity grouping reasons\n\n"
            content = title + "\n\n".join(
                f"{name} → {row['name']}\n\n{row['reason']}" for name, row in rows
            )
            await asyncio.to_thread(repository.write_output, job["job_id"], page, content)
            text += f"[{index // 64 + 1}](identities/page-{index // 64 + 1:04d}.md)\n\n"
        await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/candidate-identities.md", text)
        reviews = sorted((root / "evidence-review" / "results").glob("*.json"))
        for offset in range(0, len(reviews), 64):
            page = f"analysis/evidence-review/page-{offset // 64 + 1:04d}"
            rows = []
            for path in reviews[offset:offset + 64]:
                row = await asyncio.to_thread(read, path)
                rows.append({key: row[key] for key in (
                    "item_id", "proposal_index", "leaf_items", "outcome", "reason", "source_refs", "previous_gap",
                )})
            await asyncio.to_thread(repository.write_output, job["job_id"], page + ".json",
                                    json.dumps({"items": rows}, ensure_ascii=False))
            heading = "# 原文复核前后去向\n\n" if chinese else "# Original Evidence review dispositions\n\n"
            content = heading + "\n\n".join(
                f"{row['item_id']}: deferred → {row['outcome']}\n\n"
                f"{row['previous_gap']['reason']}\n\n{row['reason']}" for row in rows
            )
            await asyncio.to_thread(repository.write_output, job["job_id"], page + ".md", content)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)
