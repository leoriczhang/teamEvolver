"""Independent lossless extraction packets share one unchanged public Map/Finalize Compile."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
from starlette.exceptions import HTTPException

from . import bounded_compile_plan as planning
from .compile_strategy import MAX_FILE_MAP_PACKETS, render_extract_batch_compiler
from .output_language import output_language_instruction
from .ov_case_batch_orchestration import _host
from .ov_compile_mining import RUNS_ROOT, utc_now
from .source_units import PROTOCOL as SOURCE_UNIT_PROTOCOL

EXTRACT_BATCH_STAGE = "extract_batch"
EXTRACT_BATCH_PROTOCOL = "te-extract-file-batch.v1"
MAX_EXTRACT_PACKETS = MAX_FILE_MAP_PACKETS
MAX_EXTRACT_ATTEMPTS = 3


def _missing_output_error(task: dict, *, stage: str = "extraction") -> str:
    """A completed Native task may still have failed file Maps; preserve a bounded task-level hint."""
    meta = task.get("meta")
    pipeline = meta.get("pipeline") if isinstance(meta, dict) else None
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    result = task.get("result")
    result = result if isinstance(result, dict) else {}
    hints = []
    for values in (pipeline.get("errors"), result.get("warnings")):
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value and value not in hints:
                hints.append(value[:500])
            if len(hints) == 2:
                break
        if len(hints) == 2:
            break
    message = f"Independent {stage} output is missing"
    return (message + ". Native task-level diagnostics (not a sibling verdict): "
            + "; ".join(hints))[:1000] if hints else message


def _repair_error(slot: dict) -> str:
    previous, latest = slot.get("validation_error", ""), slot["error"]
    if previous and previous != latest:
        return ("Previous invalid content: " + previous[:450] + "\nLatest attempt: " + latest[:500])[:1000]
    return latest[:1000]


def _extraction_helpers():
    from .ov_staged_orchestration import _alias, _bind_unit, _canonical_extract_references, _raw_group

    return _alias, _bind_unit, _canonical_extract_references, _raw_group


def _instruction(policy: dict) -> str:
    return (
        "本次仅做独立来源抽取。每个 file Map 只读自己的一个 packet 文件，输出该 packet.output_path "
        "对应的完整索引。禁止合并兄弟来源、索引或输出路径。FileResponse.inputs 是该 file Map 的原生 IDs；"
        "索引 source_refs/context_refs 使用其 packet.reference_map 中精确允许的逻辑别名。"
        "TE 分别验收每份索引；native completed 不代表所有输出完整。" + output_language_instruction(policy)
    )


def _verified(root: Path, current: dict):
    _, _, json_text, _, _, _, read, _, sha, _ = _host()
    request = read(root / current["request_file"])
    compiler_path, manifest_path = root / current["compiler_file"], root / current["packet_file"]
    if compiler_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Frozen extraction files cannot be symbolic links")
    body, manifest = compiler_path.read_text(encoding="utf-8"), manifest_path.read_text(encoding="utf-8")
    if (
        sha(json_text(request)) != current["request_sha256"]
        or sha(body) != current["compiler_sha256"]
        or sha(manifest) != current["packet_sha256"]
    ):
        raise ValueError("Frozen extraction batch request or compiler changed")
    rows = json.loads(manifest)
    ledger = read(root / current["ledger_file"])
    unit_mode = ledger.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL
    if (
        current.get("input_protocol") != EXTRACT_BATCH_PROTOCOL
        or ledger.get("protocol") != EXTRACT_BATCH_PROTOCOL
        or request["from"] != [current["input_uri"]]
        or request["to"] != current["output_uri"]
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= MAX_EXTRACT_PACKETS
        or not isinstance(ledger.get("slots"), list)
        or not 1 <= len(ledger["slots"]) <= MAX_EXTRACT_PACKETS
        or ledger.get("source_unit_protocol") not in (None, SOURCE_UNIT_PROTOCOL)
        or current.get("source_unit_protocol") != ledger.get("source_unit_protocol")
    ):
        raise ValueError("Extraction batch directory or ledger contract changed")
    cursor = ledger["start_cursor"]
    for index, slot in enumerate(ledger["slots"]):
        context = slot["context"]
        if (
            context["slot"] != index
            or context["unit_index"] != ledger["base_unit_count"] + index
            or context["before"] != cursor
            or context["after"] <= cursor
            or (not unit_mode and context["ordinals"] != list(range(cursor, context["after"])))
            or slot["status"] not in {"pending", "valid"}
            or not 0 <= slot["format_attempts"] <= MAX_EXTRACT_ATTEMPTS
        ):
            raise ValueError("Extraction ledger changed its contiguous original source coverage")
        if unit_mode:
            from .source_unit_extraction import verify_source_unit_context

            verify_source_unit_context(root, context)
        cursor = context["after"]
    if cursor != ledger["end_cursor"]:
        raise ValueError("Extraction ledger lost its exact final source cursor")
    seen, texts = set(), []
    for index, row in enumerate(rows):
        context = row["context"]
        path = root / row["local_path"]
        if path.is_symlink() or path.resolve().parent != (root / "children" / current["child"]).resolve():
            raise ValueError("Extraction packet escaped its child directory")
        text = path.read_text(encoding="utf-8")
        packet = json.loads(text)
        chunks, controls = packet["evidence"], packet["derived_controls"]
        slot = ledger["slots"][context["slot"]]
        frozen_context = {key: value for key, value in context.items() if key not in {"format_attempts"}}
        if (
            sha(text) != row["sha256"]
            or len(text) > 32000
            or len(body) + len(text) + len(request["instruction"]) > 48000
            or row["uri"] != current["input_uri"] + f"/packet-{index:02d}.json"
            or row["output_path"] != f"index-{context['unit_index']:08d}.json"
            or row["output_path"] != packet["output_path"]
            or row["output_path"] in seen
            or slot["context"] != frozen_context
            or packet["stage"] != "extract"
            or packet["input_ids"] != context["input_ids"]
            or packet["input_ids"] != [chunk["id"] for chunk in chunks]
            or controls["allowed_source_refs"] != context["source_refs"]
            or controls["reference_map"] != dict(zip(context["source_refs"], context["input_ids"]))
            or (not unit_mode and context["ordinals"] != [chunk["ordinal"] for chunk in chunks])
        ):
            raise ValueError("Extraction packet changed its exact assigned source, scope or unit identity")
        if unit_mode:
            from .source_unit_extraction import build_source_unit_packet

            expected_packet = build_source_unit_packet(
                root, context, packet["output_language_policy"], repair_error=controls.get("repair_error", ""),
            )
            if packet != expected_packet:
                raise ValueError("Extraction packet complete source units or structural context changed")
        else:
            planning.validate_chunks(chunks)
            for chunk in chunks:
                ordinal = read(root / "chunk-ids" / (chunk["id"] + ".json"))["ordinal"]
                if read(root / "chunks" / f"{ordinal:08d}.json") != chunk:
                    raise ValueError("Extraction packet original Evidence changed")
        seen.add(row["output_path"])
        texts.append(text)
    return request, body, rows, texts, ledger


class OvExtractBatchMixin:
    async def _staged_extract_batch_next(self, job: dict, root: Path, state: dict):
        _, _, json_text, _, _, _, read, save, sha, _ = _host()
        alias, _, _, raw_group = _extraction_helpers()
        if state.get("extract_batch_protocol") != EXTRACT_BATCH_PROTOCOL:
            raise ValueError("This round did not opt into extraction batching")
        if state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
            return await self._staged_source_unit_extract_next(job, root, state)
        if state.get("extract_batch_pending"):
            ledger_file = state["extract_batch_pending"]
            ledger = await asyncio.to_thread(read, root / ledger_file)
            return await self._submit_extract_batch(job, root, state, ledger_file, ledger)
        if state["cursor"] >= state["chunk_count"]:
            return await self._staged_catalog_ready(job, root, state)
        # Preserve existing dependency-checked leading cache reuse. Missed assignments
        # remain independent packets; no source content or success is inferred from a cache miss.
        while state["cursor"] < state["chunk_count"] and state.get("round_reuse_protocol"):
            from .round_reuse import try_reuse_extract

            chunks, after = await asyncio.to_thread(raw_group, root, range(state["chunk_count"]), state["cursor"],
                                                    max_chunks=state.get("extract_max_chunks", 8))
            context = {
                "after": after,
                "ordinals": [chunk["ordinal"] for chunk in chunks],
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": [alias(chunk["id"]) for chunk in chunks],
            }
            if not await asyncio.to_thread(try_reuse_extract, job, root, state, chunks, context):
                break
            job = await self._staged_save(job, root, state)
            latest = await asyncio.to_thread(self._ov_skill_mining_repository().get_job, job["job_id"])
            if self._staged_stopped(latest):
                return latest
        if state["cursor"] >= state["chunk_count"]:
            return await self._staged_catalog_ready(job, root, state)
        before, cursor, slots = state["cursor"], state["cursor"], []
        for index in range(MAX_EXTRACT_PACKETS):
            if cursor >= state["chunk_count"]:
                break
            chunks, after = await asyncio.to_thread(raw_group, root, range(state["chunk_count"]), cursor,
                                                    max_chunks=state.get("extract_max_chunks", 8))
            context = {
                "slot": index,
                "before": cursor,
                "after": after,
                "unit_index": state["unit_count"] + index,
                "ordinals": [chunk["ordinal"] for chunk in chunks],
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": [alias(chunk["id"]) for chunk in chunks],
            }
            slots.append({"context": context, "status": "pending", "format_attempts": 0, "error": ""})
            cursor = after
        group = sha(json_text([state["round"], before, cursor, slots]))[:24]
        ledger_file = f"extract-batches/{group}/ledger.json"
        ledger = {
            "protocol": EXTRACT_BATCH_PROTOCOL,
            "round": state["round"],
            "start_cursor": before,
            "end_cursor": cursor,
            "base_unit_count": state["unit_count"],
            "slots": slots,
        }
        await asyncio.to_thread(save, root / ledger_file, ledger)
        state["extract_batch_pending"] = ledger_file
        job = await self._staged_save(job, root, state)
        return await self._submit_extract_batch(job, root, state, ledger_file, ledger)

    async def _staged_source_unit_extract_next(self, job: dict, root: Path, state: dict):
        from .source_unit_extraction import next_source_unit_assignment, render_source_unit_extract_compiler
        from .source_units import freeze_source_units

        _, _, json_text, _, package, _, read, save, sha, _ = _host()
        manifest = await asyncio.to_thread(freeze_source_units, root, state)
        state["source_unit_count"] = manifest["unit_count"]
        if state.get("extract_batch_pending"):
            path = state["extract_batch_pending"]
            return await self._submit_extract_batch(job, root, state, path, await asyncio.to_thread(read, root / path))
        cursor = state.setdefault("source_unit_cursor", 0)
        if cursor >= manifest["unit_count"]:
            return await self._staged_catalog_ready(job, root, state)
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if sha(base) != state["base_sha256"]:
            raise ValueError("Frozen base compiler changed")
        _, published = package(render_source_unit_extract_compiler(base))
        # Preserve room for a bounded validation repair without reassigning a whole source unit.
        budget = min(32000 - 1200, 48000 - len(published) - len(_instruction(state["language_policy"])) - 1200)
        while cursor < manifest["unit_count"] and state.get("round_reuse_protocol"):
            from .round_reuse import try_reuse_source_unit_extract

            context = await asyncio.to_thread(
                next_source_unit_assignment, root, cursor=cursor, count=manifest["unit_count"],
                unit_index=state["unit_count"], language_policy=state["language_policy"], max_packet_chars=budget,
            )
            if not await asyncio.to_thread(try_reuse_source_unit_extract, job, root, state, context):
                break
            job = await self._staged_save(job, root, state)
            latest = await asyncio.to_thread(self._ov_skill_mining_repository().get_job, job["job_id"])
            if self._staged_stopped(latest):
                return latest
            cursor = state["source_unit_cursor"]
        if cursor >= manifest["unit_count"]:
            return await self._staged_catalog_ready(job, root, state)
        before, slots = cursor, []
        for index in range(MAX_EXTRACT_PACKETS):
            if cursor >= manifest["unit_count"]:
                break
            context = await asyncio.to_thread(
                next_source_unit_assignment, root, cursor=cursor, count=manifest["unit_count"],
                unit_index=state["unit_count"] + index, language_policy=state["language_policy"],
                max_packet_chars=budget,
            )
            context["slot"] = index
            slots.append({"context": context, "status": "pending", "format_attempts": 0, "error": ""})
            cursor = context["after"]
        group = sha(json_text([SOURCE_UNIT_PROTOCOL, state["round"], before, cursor, slots]))[:24]
        path = f"extract-batches/{group}/ledger.json"
        ledger = {
            "protocol": EXTRACT_BATCH_PROTOCOL, "source_unit_protocol": SOURCE_UNIT_PROTOCOL,
            "round": state["round"], "start_cursor": before, "end_cursor": cursor,
            "base_unit_count": state["unit_count"], "slots": slots,
        }
        await asyncio.to_thread(save, root / path, ledger)
        state["extract_batch_pending"] = path
        job = await self._staged_save(job, root, state)
        return await self._submit_extract_batch(job, root, state, path, ledger)

    async def _submit_extract_batch(self, job, root, state, ledger_file, ledger):
        _, chunks_at, json_text, _, package, _, _, save, sha, write = _host()
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if sha(base) != state["base_sha256"]:
            raise ValueError("Frozen base compiler changed")
        from .source_scope import SOURCE_SCOPE_PROTOCOL_V3

        if state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
            from .source_unit_extraction import render_source_unit_extract_compiler

            body = render_source_unit_extract_compiler(base)
        elif state.get("source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3:
            from .compile_strategy import render_extract_batch_compiler_v3
            from .ov_staged_orchestration import SOURCE_INPUT_CLARITY_PROTOCOL

            if state.get("source_input_clarity_protocol") == SOURCE_INPUT_CLARITY_PROTOCOL:
                from .compile_strategy import render_input_clarity_extract_batch_compiler_v3

                body = render_input_clarity_extract_batch_compiler_v3(base)
            else:
                body = render_extract_batch_compiler_v3(base)
        else:
            body = render_extract_batch_compiler(base)
        await self._ensure_aux_compiler(root, state, EXTRACT_BATCH_STAGE, body)
        body = await asyncio.to_thread(
            (root / "aux-compilers" / (EXTRACT_BATCH_STAGE + ".md")).read_text, encoding="utf-8"
        )
        if sha(body) != state["aux_compilers"][EXTRACT_BATCH_STAGE]:
            raise ValueError("Frozen extraction compiler changed")
        compiler_name, published = package(body)
        packets, contexts = [], []
        for slot in ledger["slots"]:
            if slot["status"] == "valid":
                continue
            context = {**slot["context"], "format_attempts": slot["format_attempts"]}
            if state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
                from .source_unit_extraction import build_source_unit_packet

                packet = await asyncio.to_thread(
                    build_source_unit_packet, root, context, state["language_policy"],
                    repair_error=_repair_error(slot) if slot["error"] else "",
                )
            else:
                chunks = await asyncio.to_thread(chunks_at, root, context["ordinals"])
                controls = {
                    "allowed_source_refs": context["source_refs"],
                    "reference_map": dict(zip(context["source_refs"], context["input_ids"])),
                }
                if slot["error"]:
                    controls["repair_error"] = _repair_error(slot)
                packet = planning.build_stage_packet(
                    "extract",
                    f"index-{context['unit_index']:08d}.json",
                    evidence_chunks=chunks,
                    controls=controls,
                    language_policy=state["language_policy"],
                )
            text = json_text(packet)
            if len(published) + len(text) + len(_instruction(state["language_policy"])) > 48000:
                raise ValueError("Independent extraction compiler envelope exceeds 48K characters")
            packets.append(text)
            contexts.append(context)
        if not packets:
            return await self._finish_extract_batch(job, root, state, ledger)
        digest = sha(
            json_text(
                [EXTRACT_BATCH_PROTOCOL, state["round"], contexts, [sha(text) for text in packets], sha(published)]
            )
        )[:24]
        child, folder = EXTRACT_BATCH_STAGE + "-" + digest, "children/" + EXTRACT_BATCH_STAGE + "-" + digest
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{state['round']}/stages/{child}/input"
        output_uri, locators = input_uri.rsplit("/", 1)[0] + "/output", []
        for index, (text, context) in enumerate(zip(packets, contexts, strict=True)):
            filename = f"packet-{index:02d}.json"
            await asyncio.to_thread(write, root / folder / filename, text)
            locators.append(
                {
                    "uri": input_uri + "/" + filename,
                    "local_path": folder + "/" + filename,
                    "sha256": sha(text),
                    "output_path": f"index-{context['unit_index']:08d}.json",
                    "context": context,
                }
            )
        request = {
            "from": [input_uri],
            "to": output_uri,
            "skill": "viking://agent/skills/" + compiler_name,
            "instruction": _instruction(state["language_policy"]),
        }
        await asyncio.to_thread(save, root / folder / "packets.json", locators)
        await asyncio.to_thread(save, root / folder / "request.json", request)
        await asyncio.to_thread(write, root / folder / "compiler.md", published)
        state["current"] = {
            "stage": EXTRACT_BATCH_STAGE,
            "input_protocol": EXTRACT_BATCH_PROTOCOL,
            "child": child,
            "context": {},
            "ledger_file": ledger_file,
            "key": f"te-staged-{job['job_id']}-r{state['round']}-{digest}",
            "task_id": "",
            "input_uri": input_uri,
            "output_uri": output_uri,
            "request_file": folder + "/request.json",
            "packet_file": folder + "/packets.json",
            "compiler_file": folder + "/compiler.md",
            "packet_sha256": sha(json_text(locators) + "\n"),
            "request_sha256": sha(json_text(request)),
            "compiler_sha256": sha(published),
            "compiler_source_sha256": sha(body),
            "attempt": 0,
        }
        if state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
            state["current"]["source_unit_protocol"] = SOURCE_UNIT_PROTOCOL
        state["attempted_tasks"] += 1
        await asyncio.to_thread(save, root / folder / "metadata.json", state["current"])
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
        return await self._resume_extract_batch(job, root, state)

    async def _resume_extract_batch(self, job, root, state):
        _, _, _, _, _, _, _, save, _, _ = _host()
        current, repository = state["current"], self._ov_skill_mining_repository()
        started = time.perf_counter()
        try:
            request, body, rows, texts, _ = await asyncio.to_thread(_verified, root, current)
            accepted = {"task_id": current["task_id"]} if current.get("task_id") else None
            if accepted is None:
                try:
                    accepted = await self._knowledge_request(
                        job["account"], "GET", f"/api/v1/compile/submissions/{current['key']}", timeout=20.0
                    )
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
            job = await asyncio.to_thread(repository.get_job, job["job_id"])
            if accepted is None and self._staged_stopped(job):
                await self._release_staged_compile_slot(job, key=current["key"])
                return await self._staged_save(job, root, state, status="stopped", finished_at=utc_now())
            if accepted is None:
                if not await self._acquire_staged_compile_slot(job, state, current):
                    return await self._staged_save(job, root, state, status="preparing", ov_task_id="")
                client = self._round_client(job)
                await self._round_provision_compiler(client, request["skill"].rsplit("/", 1)[-1], body)
                stage_root = current["input_uri"].rsplit("/", 2)[0]
                for directory in (stage_root, current["input_uri"].rsplit("/", 1)[0], current["input_uri"]):
                    await client.mkdir(directory)
                for row, text in zip(rows, texts, strict=True):
                    await self._round_write_immutable(job["account"], row["uri"], text)
                await self._verify_case_batch_input_directory(job, current, rows)
                job = await asyncio.to_thread(repository.get_job, job["job_id"])
                if self._staged_stopped(job):
                    await self._release_staged_compile_slot(job, key=current["key"])
                    return job
                current["submission_started_at"] = utc_now()
                await asyncio.to_thread(save, root / "state.json", state)
                accepted = await self._knowledge_request(
                    job["account"],
                    "POST",
                    "/api/v1/compile",
                    timeout=60.0,
                    extra_headers={"Idempotency-Key": current["key"]},
                    json=request,
                )
            if not isinstance(accepted, dict) or not accepted.get("task_id"):
                return await self._round_submission_retry(
                    repository, job, ValueError("Extraction acceptance missing task identity")
                )
            current["task_id"] = str(accepted["task_id"])
            await self._mark_staged_compile_slot(job, current)
            await asyncio.to_thread(save, root / "children" / current["child"] / "metadata.json", current)
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
            job = await self._record_round_acceptance(repository, job, accepted, started)
            if not self._staged_stopped(job):
                await self._verify_case_batch_input_directory(job, current, rows)
            return job
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                recovered = await self._staged_release_rejected(job, root, state, exc)
                if recovered is not None:
                    return recovered
                raise
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            await self._staged_release_rejected(job, root, state, exc)
            raise

    async def _finish_extract_batch(self, job, root, state, ledger):
        if any(slot["status"] != "valid" for slot in ledger["slots"]):
            raise ValueError("Cannot advance past incomplete original source coverage")
        if ledger.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
            state["source_unit_cursor"] = ledger["end_cursor"]
            if state["source_unit_cursor"] == state["source_unit_count"]:
                state["cursor"] = state["chunk_count"]
            else:
                from .source_units import load_source_unit

                next_unit = await asyncio.to_thread(load_source_unit, root, state["source_unit_cursor"])
                state["cursor"] = min(next_unit["ordinals"])
            state.update(unit_count=ledger["base_unit_count"] + len(ledger["slots"]), current=None)
        else:
            state.update(
                cursor=ledger["end_cursor"], unit_count=ledger["base_unit_count"] + len(ledger["slots"]), current=None
            )
        state.pop("extract_batch_pending", None)
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)

    async def _advance_extract_batch(self, job, root, state, task):
        _, _, json_text, _, _, _, read, save, sha, write = _host()
        _, bind_unit, canonical, _ = _extraction_helpers()
        current, repository = state["current"], self._ov_skill_mining_repository()

        def bind_result(content, packet, context, identity):
            if current.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
                from .source_unit_extraction import bind_source_unit_index

                return bind_source_unit_index(root, json.loads(content), packet, context, identity)
            value = canonical(
                json.loads(content), input_ids=context["input_ids"], source_refs=context["source_refs"],
                reference_map=packet["derived_controls"]["reference_map"], evidence=packet["evidence"],
            )
            value = planning.validate_index(
                value, input_ids=context["input_ids"], source_refs=context["source_refs"], routing=True,
            )
            parents = dict(zip(context["source_refs"], [[index] for index in context["ordinals"]]))
            return bind_unit(value, parents, identity)

        try:
            _, _, rows, texts, ledger = await asyncio.to_thread(_verified, root, current)
            await self._verify_case_batch_input_directory(job, current, rows)
            try:
                tree = await self._knowledge_tree(job["account"], current["output_uri"])
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                tree = {"entries": []}
            files = [
                row["uri"]
                for row in tree.get("entries") or []
                if not row.get("is_dir") and not row["uri"].rsplit("/", 1)[-1].startswith(".")
            ]
            expected = {current["output_uri"] + "/" + row["output_path"] for row in rows}
            foreign = bool(set(files) - expected or len(files) != len(set(files)))
            exhausted = []
            for row, text in zip(rows, texts, strict=True):
                context, packet = row["context"], json.loads(text)
                slot = ledger["slots"][context["slot"]]
                if slot["status"] == "valid":
                    continue
                result_file = "children/" + current["child"] + "/" + row["output_path"] + ".raw"
                outcome_file = "extract-batches/outcomes/" + current["child"] + "-" + row["output_path"] + ".json"
                if (root / outcome_file).exists():
                    outcome = await asyncio.to_thread(read, root / outcome_file)
                    if outcome["context"] != context or outcome["result_file"] != result_file:
                        raise ValueError("Durable extraction outcome changed its exact assignment")
                    if outcome["valid"]:
                        content = await asyncio.to_thread((root / result_file).read_text, encoding="utf-8")
                        if sha(content) != outcome["result_sha256"] or len(content) > 48000:
                            raise ValueError("Durable extraction result changed after validation")
                        checked = await asyncio.to_thread(
                            bind_result, content, packet, context, current["child"] + "::" + row["output_path"],
                        )
                        if checked != outcome["unit"]:
                            raise ValueError("Durable extraction unit no longer matches its raw result")
                else:
                    content, error, unit = "", "", None
                    try:
                        uri = current["output_uri"] + "/" + row["output_path"]
                        if uri not in files:
                            raise ValueError(_missing_output_error(task))
                        response = await self._knowledge_request(
                            job["account"],
                            "GET",
                            "/api/v1/content/read",
                            params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"},
                            timeout=20.0,
                        )
                        content = (
                            response if isinstance(response, str) else response.get("content", response.get("text", ""))
                        )
                        if not isinstance(content, str) or not content.strip() or len(content) > 48000:
                            raise ValueError("Raw extraction output is empty or exceeds 48K characters")
                        await asyncio.to_thread(write, root / result_file, content)
                        unit = await asyncio.to_thread(
                            bind_result, content, packet, context, current["child"] + "::" + row["output_path"],
                        )
                    except (ValueError, KeyError, TypeError) as exc:
                        error = str(exc)[:1000]
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        error = "Completed independent extraction output disappeared"
                    outcome = {
                        "context": context,
                        "valid": not error,
                        "unit": unit,
                        "error": error,
                        "result_file": result_file,
                        "result_sha256": sha(content),
                    }
                    await asyncio.to_thread(save, root / outcome_file, outcome)
                if outcome["valid"]:
                    unit = outcome["unit"]
                    await asyncio.to_thread(save, root / "units/0" / f"{context['unit_index']:08d}.json", unit)
                    locator = {
                        "unit_id": unit["id"],
                        "unit_index": context["unit_index"],
                        "unit_path": f"units/0/{context['unit_index']:08d}.json",
                        "identity": current["child"] + "::" + row["output_path"],
                        "context": context,
                        "child": current["child"],
                        "packet_file": row["local_path"],
                        "packet_sha256": row["sha256"],
                        "request_file": current["request_file"],
                        "request_sha256": current["request_sha256"],
                        "compiler_file": current["compiler_file"],
                        "compiler_sha256": current["compiler_sha256"],
                        "compiler_source_sha256": current["compiler_source_sha256"],
                        "result_file": outcome["result_file"],
                        "result_sha256": outcome["result_sha256"],
                        "unit_sha256": sha(json_text(unit) + "\n"),
                    }
                    await asyncio.to_thread(
                        save, root / "extract-batches/accepted" / (unit["id"] + ".json"), locator
                    )
                    slot.update(status="valid", error="", unit_id=unit["id"])
                else:
                    slot.update(format_attempts=context["format_attempts"] + 1, error=outcome["error"])
                    if (root / result_file).exists():
                        slot["validation_error"] = outcome["error"]
                    if slot["format_attempts"] >= MAX_EXTRACT_ATTEMPTS:
                        exhausted.append(context["unit_index"])
                await asyncio.to_thread(save, root / current["ledger_file"], ledger)
            if foreign:
                raise ValueError("Extraction batch returned unexpected outputs; valid siblings are retained")
            if exhausted:
                return await self._staged_save(
                    job,
                    root,
                    state,
                    status="failed",
                    finished_at=utc_now(),
                    error="Extraction repair limit reached: " + ", ".join(map(str, exhausted)),
                )
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
        state["completed_tasks"] += 1
        state["current"] = None
        if all(slot["status"] == "valid" for slot in ledger["slots"]):
            return await self._finish_extract_batch(job, root, state, ledger)
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._submit_extract_batch(job, root, state, current["ledger_file"], ledger)
