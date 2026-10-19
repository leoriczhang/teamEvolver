"""Freeze workflow membership before producing canonical Candidate metadata once.

The catalog is read once into bounded assignments. Source text and complete
bindings remain local; only lossless method indexes enter the grouping Map.
Candidate identity review still reconciles workflows across capacity batches.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import bounded_compile_plan as planning
from .compile_strategy import MAX_FILE_MAP_PACKETS
from .direct_workflow_planning import FrozenPlanningIntegrityError
from .workflow_partition import (
    CAPACITY_SCHEMA,
    WorkflowPartitionManifestError,
    build_workflow_partition,
    restore_workflow_partition,
    validate_workflow_partition_manifest,
    validate_workflow_partition_output,
)
from .workflow_partition_compiler import (
    METADATA_STAGE,
    PARTITION_STAGE,
    render_workflow_metadata_compiler,
    render_workflow_partition_compiler,
)


def _io():
    from .ov_staged_orchestration import _json, _package, _read, _save, _sha

    return _json, _package, _read, _save, _sha


def _feedback(job: dict) -> str:
    """Feedback is control context and must not silently disappear at a size limit."""
    json_text, _, _, _, _ = _io()
    from .mining_intent import intent_from_job
    intent = intent_from_job(job)
    intent_context = json_text({"mining_intent": intent, "is_factual_evidence": False}) if intent["active"] else ""
    if int(job["current_round"]) <= 1:
        return intent_context
    submission = (job.get("supplements") or [{}])[-1]
    return json_text({"feedback": submission.get("feedback") or "",
                      "review_questions": submission.get("questions") or [],
                      "revision_context_is_evidence": False,
                      **({"mining_intent": intent} if intent["active"] else {})})


def _immutable(root: Path, path: str, value: dict) -> dict:
    _, _, read, save, _ = _io()
    target = root / path
    if target.exists():
        if read(target) != value:
            raise FrozenPlanningIntegrityError("Frozen workflow assignment changed")
    else:
        save(target, value)
    return value


def _packet(stage: str, index: int, controls: dict, state: dict) -> dict:
    return planning.build_stage_packet(
        stage, f"workflow-{index:08d}.json", controls=controls,
        language_policy=state["language_policy"], revision_context=state["workflow_revision"],
        max_chars=state["workflow_packet_budget"],
    )


def _partition_assignments(root: Path, state: dict, indexes: list[int], *, start: int) -> int:
    """Write bounded work files; do not keep an unbounded in-memory task queue."""
    json_text, _, read, _, sha = _io()
    records, positions, count = [], [], start

    def build(values):
        return build_workflow_partition(values, max_controls_chars=state["workflow_controls_budget"])

    def flush():
        nonlocal count
        built = build(records)
        if built["status"] != "ready":
            raise ValueError("One complete method index exceeds the workflow planning budget")
        packet = _packet(PARTITION_STAGE, count, built["controls"], state)
        value = {"manifest": built["manifest"], "catalog_indexes": positions[:], "packet": packet}
        path = f"workflow-planning/assignments/{count:08d}.json"
        _immutable(root, path, value)
        _immutable(root, f"workflow-planning/queue/{count:08d}.json",
                   {"assignment_file": path, "assignment_sha256": sha(json_text(value)), "index": count})
        count += 1

    for index in indexes:
        record = read(root / "catalog/method" / f"{index:08d}.json")
        tentative = [*records, record]
        if records and build(tentative)["status"] != "ready":
            flush()
            records, positions = [], []
        records.append(record)
        positions.append(index)
    if records:
        flush()
    return count


def _assignment(root: Path, context: dict) -> dict:
    json_text, _, read, _, sha = _io()
    relative = context["assignment_file"]
    path = root / relative
    if (not relative.startswith("workflow-planning/") or Path(relative).is_absolute()
            or ".." in Path(relative).parts or path.is_symlink() or root.resolve() not in path.resolve().parents):
        raise FrozenPlanningIntegrityError("Workflow manifest locator escaped its round")
    value = read(path)
    if sha(json_text(value)) != context["assignment_sha256"]:
        raise FrozenPlanningIntegrityError("Frozen workflow manifest SHA changed")
    if "catalog_indexes" in value:
        frozen = value["manifest"]["records"]
        records = [row["record"] for row in frozen.values()]
        actual = [read(root / "catalog/method" / f"{index:08d}.json") for index in value["catalog_indexes"]]
        if actual != records:
            raise FrozenPlanningIntegrityError("Workflow method catalog changed after assignment")
    return value


def _decision(root: Path, plan: dict, records: list[dict], target: str, reason: str, name: str = ""):
    _, _, _, save, _ = _io()
    for record in records:
        if target == "deferred":
            gap = {"reason": reason, "source_refs": record["item"]["source_files"]}
            if gap not in plan["deferred"]:
                plan["deferred"].append(gap)
        for leaf in record.get("leaf_items", [record["item"]["item_id"]]):
            save(root / "method-decisions" / (leaf + ".json"), {"target": target, "name": name, "reason": reason})


class OvWorkflowPlanningMixin:
    async def _workflow_compiler(self, root: Path, state: dict, stage: str) -> str:
        _, _, _, _, sha = _io()
        renderer = {PARTITION_STAGE: render_workflow_partition_compiler,
                    METADATA_STAGE: render_workflow_metadata_compiler}[stage]
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if sha(base) != state["base_sha256"]:
            raise FrozenPlanningIntegrityError("Frozen workflow compiler policy changed")
        await self._ensure_aux_compiler(root, state, stage, renderer(base))
        body = await asyncio.to_thread((root / "aux-compilers" / (stage + ".md")).read_text, encoding="utf-8")
        if sha(body) != state["aux_compilers"][stage]:
            raise FrozenPlanningIntegrityError("Frozen workflow compiler SHA changed")
        return body

    async def _workflow_enter(self, job: dict, root: Path, state: dict):
        json_text, package, _, _, _ = _io()
        from .ov_workflow_batch_transport import REPAIR_RESERVE_CHARS, _instruction

        bodies = [await self._workflow_compiler(root, state, stage) for stage in (PARTITION_STAGE, METADATA_STAGE)]
        # Reserve the transport instruction and a local format repair without
        # changing the original assignment. Metadata has slightly more framing.
        instruction_chars = len(_instruction(state["language_policy"]))
        budget = min(40000 - REPAIR_RESERVE_CHARS, *(
            48000 - len(package(body)[1]) - instruction_chars - REPAIR_RESERVE_CHARS for body in bodies
        ))
        revision = _feedback(job)
        empty = planning.build_stage_packet(PARTITION_STAGE, "workflow-00000000.json", controls={},
                                           language_policy=state["language_policy"], revision_context=revision,
                                           max_chars=40000)
        controls_budget = min(40000, budget - len(json_text(empty)) - 1024)
        if controls_budget < 1024:
            raise ValueError("Complete review feedback and compiler leave no bounded workflow planning budget")
        state.update(workflow_packet_budget=budget, workflow_controls_budget=controls_budget,
                     workflow_revision=revision, workflow_partition_cursor=0, workflow_metadata_cursor=0,
                     workflow_group_count=0, phase=PARTITION_STAGE)
        state["workflow_partition_count"] = await asyncio.to_thread(
            _partition_assignments, root, state, list(range(state["catalog_counts"]["method"])), start=0,
        )
        job = await self._staged_save(job, root, state)
        return await self._workflow_next(job, root, state)

    async def _workflow_next(self, job: dict, root: Path, state: dict):
        json_text, _, read, save, sha = _io()
        from .ov_workflow_batch_transport import WORKFLOW_BATCH_PROTOCOL

        if state.get("workflow_batch_pending"):
            path = state["workflow_batch_pending"]
            return await self._submit_workflow_batch(job, root, state, path, await asyncio.to_thread(read, root / path))
        stage = state["phase"]
        if stage == PARTITION_STAGE:
            cursor, count, folder = state["workflow_partition_cursor"], state["workflow_partition_count"], "queue"
        else:
            cursor, count, folder = state["workflow_metadata_cursor"], state["workflow_group_count"], "metadata-queue"
        if cursor == count:
            if stage == PARTITION_STAGE:
                state["phase"] = METADATA_STAGE
                job = await self._staged_save(job, root, state)
                return await self._workflow_next(job, root, state)
            return await self._staged_selection_complete(job, root, state)
        slots = []
        for index in range(cursor, min(count, cursor + MAX_FILE_MAP_PACKETS)):
            context = await asyncio.to_thread(read, root / f"workflow-planning/{folder}/{index:08d}.json")
            assigned = await asyncio.to_thread(_assignment, root, context)
            slots.append({"packet": assigned["packet"], "context": context, "status": "pending",
                          "format_attempts": 0, "error": ""})
        ledger = {"protocol": WORKFLOW_BATCH_PROTOCOL, "stage": stage, "slots": slots,
                  "before": cursor, "after": cursor + len(slots)}
        path = f"workflow-planning/batches/{sha(json_text(ledger))}.json"
        await asyncio.to_thread(save, root / path, ledger)
        state["workflow_batch_pending"] = path
        job = await self._staged_save(job, root, state)
        return await self._submit_workflow_batch(job, root, state, path, ledger)

    async def _workflow_accept_slot(self, job: dict, root: Path, state: dict, slot: dict, value):
        await self._workflow_validate_slot(root, state, slot)
        assigned = await asyncio.to_thread(_assignment, root, slot["context"])
        if state["phase"] == PARTITION_STAGE:
            checked = await asyncio.to_thread(validate_workflow_partition_output, value, assigned["manifest"])
            if state["language_policy"].get("language") == "zh-CN":
                from .output_language import _han

                reasons = ([checked["reason"]] if checked["schema_version"] == CAPACITY_SCHEMA else
                           list(checked["reasons"].values()))
                if any(not any(_han(char) for char in reason) for reason in reasons):
                    raise ValueError("方法归组和暂缓说明必须使用中文。")
            return checked
        from .workflow_metadata import validate_workflow_metadata_output

        return await asyncio.to_thread(validate_workflow_metadata_output, value, assigned["manifest"],
                                       language_policy=state["language_policy"])

    async def _workflow_validate_slot(self, root: Path, state: dict, slot: dict):
        from .workflow_metadata import WorkflowMetadataManifestError, validate_workflow_metadata_manifest

        assigned = await asyncio.to_thread(_assignment, root, slot["context"])
        if assigned["packet"] != slot["packet"]:
            raise FrozenPlanningIntegrityError("Workflow slot differs from its original canonical packet")
        validator = (validate_workflow_partition_manifest if state["phase"] == PARTITION_STAGE
                     else validate_workflow_metadata_manifest)
        try:
            await asyncio.to_thread(validator, assigned["manifest"], controls=slot["packet"]["derived_controls"])
        except (WorkflowPartitionManifestError, WorkflowMetadataManifestError) as exc:
            raise FrozenPlanningIntegrityError(str(exc)) from exc

    async def _workflow_batch_finished(self, job: dict, root: Path, state: dict, ledger: dict):
        await asyncio.to_thread(self._workflow_apply_batch, root, state, ledger)
        state.pop("workflow_batch_pending", None)
        job = await self._staged_save(job, root, state)
        return await self._workflow_next(job, root, state)

    def _workflow_apply_batch(self, root: Path, state: dict, ledger: dict):
        """Idempotent deterministic projections from the accepted batch journal."""
        json_text, _, read, save, sha = _io()
        from .workflow_metadata import build_workflow_metadata, restore_workflow_metadata

        stage = ledger["stage"]
        journal = f"workflow-planning/commits/{stage}-{ledger['before']:08d}.json"
        completed = root / journal
        ledger_sha256 = sha(json_text(ledger))
        if completed.exists():
            try:
                receipt = read(completed)
                changes = receipt["changes"]
                cursor_field = ("workflow_partition_cursor" if stage == PARTITION_STAGE
                                else "workflow_metadata_cursor")
                if (set(receipt) != {"ledger_sha256", "changes_sha256", "changes"}
                        or receipt["ledger_sha256"] != ledger_sha256
                        or not isinstance(changes, dict)
                        or set(changes) != {"workflow_partition_count", "workflow_group_count",
                                           "selection_cursor", cursor_field}
                        or any(type(value) is not int or value < 0 for value in changes.values())
                        or changes[cursor_field] != ledger["after"]
                        or receipt["changes_sha256"] != sha(json_text(changes))):
                    raise FrozenPlanningIntegrityError("Frozen workflow commit journal or ledger receipt changed")
            except (KeyError, TypeError, ValueError, OSError) as exc:
                if isinstance(exc, FrozenPlanningIntegrityError):
                    raise
                raise FrozenPlanningIntegrityError("Frozen workflow commit journal is damaged") from exc
            state.update(changes)
            return
        plan_path = root / "plan.json"
        plan = read(plan_path) if plan_path.exists() else {"schema_version": planning.PLAN_SCHEMA,
                                                        "skills": [], "deferred": []}
        next_partition, next_group = state["workflow_partition_count"], state["workflow_group_count"]
        accepted_methods = state["selection_cursor"]
        for slot in ledger["slots"]:
            if slot["status"] != "valid":
                raise FrozenPlanningIntegrityError("Cannot commit an incomplete workflow batch")
            context = slot["context"]
            assigned = _assignment(root, context)
            value = slot["acceptance"]
            if stage == PARTITION_STAGE:
                checked = validate_workflow_partition_output(value, assigned["manifest"])
                if checked["schema_version"] == CAPACITY_SCHEMA:
                    indexes = assigned["catalog_indexes"]
                    if len(indexes) < 2:
                        raise ValueError("A single method partition cannot split further")
                    midpoint = len(indexes) // 2
                    for indexes_part in (indexes[:midpoint], indexes[midpoint:]):
                        next_partition = _partition_assignments(root, state, indexes_part, start=next_partition)
                    continue
                restored = restore_workflow_partition(checked, assigned["manifest"])
                for target in ("knowledge", "deferred"):
                    for group in restored[target]:
                        _decision(root, plan, group["records"], target, group["reason"])
                for group in restored["groups"] + restored["independent"]:
                    controls, manifest = build_workflow_metadata(group, max_controls_chars=min(
                        40000, state["workflow_packet_budget"] - len(state["workflow_revision"]) - 512,
                    ), mining_intent=state.get("mining_intent"))
                    packet = _packet(METADATA_STAGE, next_group, controls, state)
                    entry = {"manifest": manifest, "group": group, "packet": packet}
                    path = f"workflow-planning/groups/{next_group:08d}.json"
                    _immutable(root, path, entry)
                    _immutable(root, f"workflow-planning/metadata-queue/{next_group:08d}.json",
                               {"assignment_file": path, "assignment_sha256": sha(json_text(entry)),
                                "index": next_group})
                    next_group += 1
                accepted_methods += len(assigned["catalog_indexes"])
            else:
                restored = restore_workflow_metadata(value, assigned["manifest"],
                                                     language_policy=state["language_policy"])
                group = assigned["group"]
                if "deferred" in restored:
                    _decision(root, plan, group["records"], "deferred", restored["deferred"]["reason"])
                    continue
                candidate = {key: value for key, value in restored["candidate"].items()
                             if key not in {"derived_from", "source_refs"}}
                # Slugs are machine labels, never proof that independently grouped
                # workflows are equal. Resolve a collision without another Compile.
                original_name = candidate["name"]
                # Reserve the final name as well as the original proposal. A
                # later model slug can equal an earlier generated suffix. Each
                # workflow owns at most one reservation, so this search is bounded
                # by the complete group count and repeats identically after a crash.
                for collision_index in range(state["workflow_group_count"] + 1):
                    if collision_index:
                        suffix = (context["assignment_sha256"][:16] if collision_index == 1 else
                                  sha(context["assignment_sha256"] + ":" + str(collision_index))[:16])
                        candidate["name"] = original_name[:47] + "-" + suffix
                    ownership = root / "workflow-planning/names" / (candidate["name"] + ".json")
                    owner = read(ownership) if ownership.exists() else None
                    if owner is None or owner["assignment_sha256"] == context["assignment_sha256"]:
                        _immutable(root, str(ownership.relative_to(root)),
                                   {"assignment_sha256": context["assignment_sha256"]})
                        break
                else:
                    raise FrozenPlanningIntegrityError("Cannot reserve a unique bounded workflow name")
                provenance = group["provenance"]
                if "intent" in value:
                    _immutable(root, f"mining-intent/assessments/{candidate['name']}.json", value["intent"])
                candidate["source_refs"] = provenance["source_files"]
                previous = next((row for row in plan["skills"] if row["name"] == candidate["name"]), None)
                if previous and previous != candidate:
                    raise FrozenPlanningIntegrityError("Accepted workflow metadata cannot change on restart")
                if previous is None:
                    plan["skills"].append(candidate)
                full = sorted({ordinal for uri in provenance["source_files"]
                               for ordinal in read(root / "source-files" / (sha(uri) + ".json"))["ordinals"]})
                scope = {"ordinals": full, "method_ordinals": provenance["ordinals"]}
                _immutable(root, f"scopes/{candidate['name']}.json", scope)
                _decision(root, plan, group["records"], "candidate", candidate["rationale"], candidate["name"])
        save(plan_path, plan)
        changes = {"workflow_partition_count": next_partition, "workflow_group_count": next_group,
                   "selection_cursor": accepted_methods,
                   ("workflow_partition_cursor" if stage == PARTITION_STAGE else "workflow_metadata_cursor"):
                       ledger["after"]}
        _immutable(root, journal, {"ledger_sha256": ledger_sha256,
                                   "changes_sha256": sha(json_text(changes)), "changes": changes})
        state.update(changes)
