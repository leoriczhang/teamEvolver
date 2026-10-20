"""Independent method-planning windows over the unchanged public file Map/Finalize API."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
from starlette.exceptions import HTTPException

from . import bounded_compile_plan as planning
from .compile_strategy import MAX_FILE_MAP_PACKETS, render_merge_batch_compiler
from .output_language import output_language_instruction
from .ov_case_batch_orchestration import _host
from .ov_compile_mining import RUNS_ROOT, utc_now

MERGE_BATCH_STAGE = "merge_batch"
MERGE_BATCH_PROTOCOL = "te-merge-file-batch.v1"
MAX_MERGE_PACKETS = MAX_FILE_MAP_PACKETS
MAX_MERGE_ATTEMPTS = 3
_LEDGER_FIELDS = ("protocol", "round", "start_cursor", "end_cursor", "base_proposal_count", "catalog_count",
                  "workflow_planning_protocol")


def _private(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or Path(relative).is_absolute()
            or any(part in {".", ".."} for part in Path(relative).parts)):
        raise ValueError("Merge batch file locator must remain private to this round")
    path = root / relative
    if root.resolve() not in path.resolve().parents or any(p.is_symlink() for p in [path, *path.parents]
                                                          if p != root.parent):
        raise ValueError("Merge batch file locator escaped its frozen round")
    return path


def _immutable(root: Path, relative: str, value: dict) -> None:
    *_, read, save, _, _ = _host()
    path = _private(root, relative)
    if path.exists():
        if read(path) != value:
            raise ValueError("Accepted merge result cannot be rewritten")
    else:
        save(path, value)


def _model_item(record: dict) -> dict:
    *_, sha, _ = _host()
    item = record["item"]
    return {**{key: value for key, value in item.items() if key != "source_files"},
            "source_file_ids": ["file-" + sha(uri)[:24] for uri in item.get("source_files", [])]}


def _records(root: Path, context: dict) -> list[dict]:
    *_, read, _, sha, _ = _host()
    rows = [read(_private(root, f"catalog/method/{index:08d}.json")) for index in context["record_indexes"]]
    if [sha(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) for row in rows] != context["record_sha256"]:
        raise ValueError("Frozen merge methods or their original source lineage changed")
    return rows


def _controls(records: list[dict]) -> dict:
    return {
        "items": [_model_item(row) for row in records],
        "input_ids": list(dict.fromkeys(row["unit_id"] for row in records)),
        "allowed_source_refs": list(dict.fromkeys(ref for row in records for ref in row["item"]["source_refs"])),
        "planning_version": 2,
        "complete_source_available": True,
        "readonly_context_rule": "Only assigned items may be consumed. Every assigned method needs exactly "
        "one disposition; do not consume a sibling packet's IDs. Methods remain planning context, not Evidence.",
    }


def _instruction(policy: dict) -> str:
    return (
        "Each file Map processes only its own bounded merge_index packet and emits that packet.output_path. "
        "Preserve all assigned method IDs exactly once. Never combine sibling windows or output paths. "
        "FileResponse.inputs contains only native IDs supplied to that file Map; TE planning IDs belong in "
        "the content's derived_from. Native completed is not proof of complete independent outputs. "
        + output_language_instruction(policy)
    )


def _packet(root: Path, job: dict, context: dict, policy: dict, published: str) -> dict:
    from .ov_staged_orchestration import _assignment_revision, _json, _revision_view

    controls = _controls(_records(root, context))
    if context.get("error"):
        controls["repair_error"] = context["error"][:1000]
    packet = planning.build_stage_packet("merge_index", context["output_path"], controls=controls,
                                         language_policy=policy)
    limit = min(32000, 48000 - len(published) - len(_instruction(policy)))
    revision_path = root / "revision.txt"
    revision = revision_path.read_text(encoding="utf-8") if revision_path.is_file() else ""
    budget = min(6000, limit - len(_json(packet)) - 512)
    while budget >= 512:
        note = _revision_view(revision, budget // 2) if revision else ""
        note += _assignment_revision(root, job, controls, context, budget - len(note))
        packet["revision_context"] = note
        if len(_json(packet)) <= limit:
            return packet
        budget //= 2
    packet["revision_context"] = _json({"revision_context_is_evidence": False,
                                        "revision_context_omitted": bool(revision), "full_revision_archived": True})
    if len(_json(packet)) > limit:
        raise ValueError("A merge assignment exceeds its bounded packet; split method windows without dropping IDs")
    return packet


def _verified(root: Path, current: dict):
    _, _, json_text, _, _, _, read, _, sha, _ = _host()
    request = read(_private(root, current["request_file"]))
    body = _private(root, current["compiler_file"]).read_text(encoding="utf-8")
    manifest = _private(root, current["packet_file"]).read_text(encoding="utf-8")
    rows = json.loads(manifest)
    ledger = read(_private(root, current["ledger_file"]))
    if (current.get("stage") != MERGE_BATCH_STAGE or current.get("input_protocol") != MERGE_BATCH_PROTOCOL
            or not re.fullmatch(r"merge_batch-[a-f0-9]{24}", current.get("child", ""))
            or ledger.get("protocol") != MERGE_BATCH_PROTOCOL
            or request["from"] != [current["input_uri"]] or request["to"] != current["output_uri"]
            or sha(json_text(request)) != current["request_sha256"] or sha(body) != current["compiler_sha256"]
            or sha(manifest) != current["packet_sha256"] or not isinstance(rows, list)
            or not 1 <= len(rows) <= MAX_MERGE_PACKETS or not 1 <= len(ledger["slots"]) <= MAX_MERGE_PACKETS):
        raise ValueError("Frozen merge batch request, compiler or manifest changed")
    authority = [{key: ledger[key] for key in _LEDGER_FIELDS}, [slot["context"] for slot in ledger["slots"]]]
    if sha(json_text(authority)) != current["ledger_authority_sha256"]:
        raise ValueError("Merge ledger changed its frozen cursor, proposal base or original methods")
    position = ledger["start_cursor"]
    for index, slot in enumerate(ledger["slots"]):
        context = slot["context"]
        if (context["slot"] != index or context["before"] != position
                or context["after"] != position + len(context["record_indexes"])
                or not 1 <= len(context["record_indexes"]) <= 8
                or context["output_path"] != f"merge-{position:08d}.json"
                or slot["status"] not in {"pending", "valid"}
                or not 0 <= slot["format_attempts"] <= MAX_MERGE_ATTEMPTS):
            raise ValueError("Merge windows lost their exact ordered assignment")
        expected = list(range(position, context["after"]))
        if ledger.get("workflow_planning_protocol"):
            from .workflow_planning import workflow_method_index

            expected = [workflow_method_index(root, i, ledger["catalog_count"]) for i in expected]
        if context["record_indexes"] != expected:
            raise ValueError("Merge assignment differs from its frozen complete catalog order")
        _records(root, context)
        position = context["after"]
    if position != ledger["end_cursor"]:
        raise ValueError("Merge batch changed its exact catalog cursor")
    seen, texts = set(), []
    for index, row in enumerate(rows):
        path = _private(root, row["local_path"])
        text = path.read_text(encoding="utf-8")
        packet, context = json.loads(text), row["context"]
        slot = ledger["slots"][context["slot"]]
        stable = {key: value for key, value in context.items() if key not in {"format_attempts", "error"}}
        controls = _controls(_records(root, context))
        actual = {key: value for key, value in packet["derived_controls"].items() if key != "repair_error"}
        if (path.parent != (root / "children" / current["child"]).resolve()
                or sha(text) != row["sha256"] or len(text) > 32000
                or len(body) + len(text) + len(request["instruction"]) > 48000
                or row["uri"] != current["input_uri"] + f"/packet-{index:02d}.json"
                or row["output_path"] != context["output_path"] or row["output_path"] != packet["output_path"]
                or row["output_path"] in seen or slot["context"] != stable
                or packet["stage"] != "merge_index" or packet["evidence"] != []
                or packet["input_ids"] != [] or actual != controls):
            raise ValueError("Merge packet changed its exact method IDs, source scope or destination")
        seen.add(row["output_path"])
        texts.append(text)
    return request, body, rows, texts, ledger


def _decode(root: Path, context: dict, content: str, identity: str) -> dict:
    from .ov_staged_orchestration import _bind_unit, _canonical_planning_header, _chunk_metadata

    _, _, json_text, _, _, _, _, _, sha, _ = _host()
    records = _records(root, context)
    controls = _controls(records)
    items = [record["item"] for record in records]
    value = _canonical_planning_header(json.loads(content), input_ids=controls["input_ids"],
                                       item_ids=[item["item_id"] for item in items])
    checked = planning.validate_merge(value, items=items, input_ids=controls["input_ids"])
    parents, by_item = {}, {record["item"]["item_id"]: record for record in records}
    for record in records:
        for ref, ordinals in record["bindings"].items():
            if ref in parents and parents[ref] != ordinals:
                raise ValueError("Merged method scope aliases changed their original ancestry")
            parents[ref] = ordinals
    unit = _bind_unit(checked, parents, identity)
    files = {ref: list(dict.fromkeys(row["source_uri"] for row in _chunk_metadata(root, ordinals)))
             for ref, ordinals in unit["bindings"].items()}
    proposals = []
    for proposed, original in zip(planning.planning_items([unit], source_files=files), checked["methods"]):
        ordinals = sorted({ordinal for ref in proposed["source_refs"] for ordinal in unit["bindings"][ref]})
        proposals.append({
            "item": proposed, "unit_id": unit["id"], "ordinals": ordinals,
            "bindings": {ref: unit["bindings"][ref] for ref in proposed["source_refs"]},
            "leaf_items": list(dict.fromkeys(leaf for item_id in original["derived_from"]
                                            for leaf in by_item[item_id]["leaf_items"])),
        })
    return {"checked": checked, "proposals": proposals, "input_items": items, "identity": identity,
            "result_sha256": sha(content), "canonical_sha256": sha(json_text(checked))}


class OvMergeBatchMixin:
    async def _staged_merge_batch_next(self, job: dict, root: Path, state: dict):
        _, _, json_text, _, package, _, read, save, sha, _ = _host()
        if state.get("merge_batch_protocol") != MERGE_BATCH_PROTOCOL:
            raise ValueError("This frozen round did not opt into merge batching")
        if state.get("merge_batch_pending"):
            relative = state["merge_batch_pending"]
            ledger = await asyncio.to_thread(read, root / relative)
            return await self._submit_merge_batch(job, root, state, relative, ledger)
        count = state["catalog_counts"]["method"]
        if state["cursor"] >= count:
            state.update(phase="selection", selection_cursor=0)
            job = await self._staged_save(job, root, state)
            return await self._staged_next(job, root, state)
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if sha(base) != state["base_sha256"]:
            raise ValueError("Frozen base compiler changed")
        await self._ensure_aux_compiler(root, state, MERGE_BATCH_STAGE, render_merge_batch_compiler(base))
        body = await asyncio.to_thread((root / "aux-compilers/merge_batch.md").read_text, encoding="utf-8")
        _, published = package(body)
        cursor, slots = state["cursor"], []
        for slot in range(MAX_MERGE_PACKETS):
            if cursor >= count:
                break
            indexes, hashes = [], []
            for position in range(cursor, min(cursor + 8, count)):
                index = position
                if state.get("workflow_planning_protocol"):
                    from .workflow_planning import workflow_method_index

                    index = await asyncio.to_thread(workflow_method_index, root, position, count)
                record = await asyncio.to_thread(read, root / "catalog/method" / f"{index:08d}.json")
                tentative = {"slot": slot, "before": cursor, "after": position + 1,
                             "record_indexes": indexes + [index], "record_sha256": hashes + [sha(json_text(record))],
                             "typed_planning": "merge", "output_path": f"merge-{cursor:08d}.json"}
                try:
                    await asyncio.to_thread(_packet, root, job, tentative, state["language_policy"], published)
                except ValueError:
                    if not indexes:
                        raise
                    break
                indexes, hashes = tentative["record_indexes"], tentative["record_sha256"]
            context = {"slot": slot, "before": cursor, "after": cursor + len(indexes),
                       "record_indexes": indexes, "record_sha256": hashes,
                       "typed_planning": "merge", "output_path": f"merge-{cursor:08d}.json"}
            slots.append({"context": context, "status": "pending", "format_attempts": 0, "error": ""})
            cursor = context["after"]
        ledger = {"protocol": MERGE_BATCH_PROTOCOL, "round": state["round"], "start_cursor": state["cursor"],
                  "end_cursor": cursor, "base_proposal_count": state["proposal_count"], "catalog_count": count,
                  "workflow_planning_protocol": state.get("workflow_planning_protocol"), "slots": slots}
        relative = f"merge-batches/{sha(json_text(ledger))[:24]}/ledger.json"
        await asyncio.to_thread(save, root / relative, ledger)
        state["merge_batch_pending"] = relative
        job = await self._staged_save(job, root, state)
        return await self._submit_merge_batch(job, root, state, relative, ledger)

    async def _submit_merge_batch(self, job, root, state, ledger_file, ledger):
        _, _, json_text, _, package, _, _, save, sha, write = _host()
        body = await asyncio.to_thread((root / "aux-compilers/merge_batch.md").read_text, encoding="utf-8")
        if sha(body) != state["aux_compilers"][MERGE_BATCH_STAGE]:
            raise ValueError("Frozen merge compiler changed")
        compiler_name, published = package(body)
        contexts, packets = [], []
        for slot in ledger["slots"]:
            if slot["status"] == "valid":
                continue
            context = {**slot["context"], "format_attempts": slot["format_attempts"], "error": slot["error"]}
            packet = await asyncio.to_thread(_packet, root, job, context, state["language_policy"], published)
            packets.append(json_text(packet))
            contexts.append(context)
        if not packets:
            return await self._finish_merge_batch(job, root, state, ledger)
        digest = sha(json_text([MERGE_BATCH_PROTOCOL, state["round"], contexts,
                               [sha(text) for text in packets], sha(published)]))[:24]
        child, folder = MERGE_BATCH_STAGE + "-" + digest, "children/" + MERGE_BATCH_STAGE + "-" + digest
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{state['round']}/stages/{child}/input"
        output_uri, rows = input_uri.rsplit("/", 1)[0] + "/output", []
        for index, (text, context) in enumerate(zip(packets, contexts, strict=True)):
            filename = f"packet-{index:02d}.json"
            await asyncio.to_thread(write, root / folder / filename, text)
            rows.append({"uri": input_uri + "/" + filename, "local_path": folder + "/" + filename,
                         "sha256": sha(text), "output_path": context["output_path"], "context": context})
        request = {"from": [input_uri], "to": output_uri, "skill": "viking://agent/skills/" + compiler_name,
                   "instruction": _instruction(state["language_policy"])}
        await asyncio.to_thread(save, root / folder / "packets.json", rows)
        await asyncio.to_thread(save, root / folder / "request.json", request)
        await asyncio.to_thread(write, root / folder / "compiler.md", published)
        state["current"] = {"stage": MERGE_BATCH_STAGE, "input_protocol": MERGE_BATCH_PROTOCOL,
                            "child": child, "context": {}, "ledger_file": ledger_file,
                            "key": f"te-staged-{job['job_id']}-r{state['round']}-{digest}", "task_id": "",
                            "input_uri": input_uri, "output_uri": output_uri,
                            "request_file": folder + "/request.json", "packet_file": folder + "/packets.json",
                            "compiler_file": folder + "/compiler.md", "packet_sha256": sha(json_text(rows) + "\n"),
                            "request_sha256": sha(json_text(request)), "compiler_sha256": sha(published),
                            "compiler_source_sha256": sha(body), "attempt": 0,
                            "ledger_authority_sha256": sha(json_text([
                                {key: ledger[key] for key in _LEDGER_FIELDS},
                                [slot["context"] for slot in ledger["slots"]],
                            ]))}
        state["attempted_tasks"] += 1
        await asyncio.to_thread(save, root / folder / "metadata.json", state["current"])
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="",
                                      compile_request=request, submission_idempotency_key=state["current"]["key"])
        return await self._resume_merge_batch(job, root, state)

    async def _finish_merge_batch(self, job, root, state, ledger):
        _, _, json_text, _, _, _, read, _, sha, _ = _host()
        if any(slot["status"] != "valid" for slot in ledger["slots"]):
            raise ValueError("Cannot advance past an incomplete exact-once merge window")
        position = ledger["base_proposal_count"]
        for slot in ledger["slots"]:
            outcome = await asyncio.to_thread(read, _private(root, slot["outcome_file"]))
            if sha(json_text(outcome)) != slot["outcome_sha256"]:
                raise ValueError("Accepted merge sibling changed before deterministic proposal commit")
            content = await asyncio.to_thread(_private(root, outcome["result_file"]).read_text, encoding="utf-8")
            replay = await asyncio.to_thread(_decode, root, outcome["context"], content, outcome["identity"])
            if replay != {key: outcome[key] for key in replay}:
                raise ValueError("Accepted merge result no longer matches its frozen original method assignment")
            for proposal in outcome["proposals"]:
                await asyncio.to_thread(_immutable, root, f"proposals/{position:08d}.json", proposal)
                position += 1
            disposition_path = "dispositions/" + outcome["identity"].replace("::", "-") + ".json"
            await asyncio.to_thread(_immutable, root, disposition_path,
                                    {"kind": "merge", "input_items": outcome["input_items"],
                                     "result": outcome["checked"]})
        state.update(cursor=ledger["end_cursor"], proposal_count=position, current=None)
        state.pop("merge_batch_pending", None)
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)

    async def _advance_merge_batch(self, job, root, state, task):
        _, _, json_text, _, _, _, read, save, sha, write = _host()
        current, repository = state["current"], self._ov_skill_mining_repository()
        if not current.get("task_id"):
            return await self._resume_merge_batch(job, root, state)
        if task.get("task_id") and str(task["task_id"]) != current["task_id"]:
            return job
        try:
            _, _, rows, texts, ledger = await asyncio.to_thread(_verified, root, current)
            await self._verify_case_batch_input_directory(job, current, rows)
            try:
                tree = await self._knowledge_tree(job["account"], current["output_uri"])
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                tree = {"entries": []}
            files = [row["uri"] for row in tree.get("entries") or []
                     if not row.get("is_dir") and not row["uri"].rsplit("/", 1)[-1].startswith(".")]
            expected = {current["output_uri"] + "/" + row["output_path"] for row in rows}
            foreign, exhausted = bool(set(files) - expected or len(files) != len(set(files))), []
            for row in rows:
                context, slot = row["context"], ledger["slots"][row["context"]["slot"]]
                if slot["status"] == "valid":
                    continue
                result_file = "children/" + current["child"] + "/" + row["output_path"] + ".raw"
                outcome_file = "merge-batches/outcomes/" + current["child"] + "-" + row["output_path"] + ".json"
                if (root / outcome_file).exists():
                    outcome = await asyncio.to_thread(read, root / outcome_file)
                    if outcome["context"] != context or outcome["result_file"] != result_file:
                        raise ValueError("Durable merge outcome changed its exact window")
                else:
                    content, error, accepted = "", "", {}
                    try:
                        uri = current["output_uri"] + "/" + row["output_path"]
                        if uri not in files:
                            from .ov_extract_batch_orchestration import _missing_output_error

                            raise ValueError(_missing_output_error(task, stage="merge"))
                        response = await self._knowledge_request(job["account"], "GET", "/api/v1/content/read",
                                                                 params={"uri": uri, "offset": 0, "limit": -1,
                                                                         "raw": "true"}, timeout=20.0)
                        content = (response if isinstance(response, str)
                                   else response.get("content", response.get("text")))
                        if not isinstance(content, str) or not content.strip() or len(content) > 48000:
                            raise ValueError("Raw merge output is empty or exceeds 48K characters")
                        await asyncio.to_thread(write, root / result_file, content)
                        accepted = await asyncio.to_thread(_decode, root, context, content,
                                                          current["child"] + "::" + row["output_path"])
                    except (ValueError, KeyError, TypeError) as exc:
                        error = str(exc)[:1000]
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        error = "Completed independent merge output disappeared"
                    outcome = {**accepted, "context": context, "valid": not error, "error": error,
                               "result_file": result_file}
                    await asyncio.to_thread(_immutable, root, outcome_file, outcome)
                if outcome["valid"]:
                    slot.update(status="valid", error="", outcome_file=outcome_file,
                                outcome_sha256=sha(json_text(outcome)))
                else:
                    slot.update(format_attempts=context["format_attempts"] + 1, error=outcome["error"])
                    if slot["format_attempts"] >= MAX_MERGE_ATTEMPTS:
                        exhausted.append(context["before"])
                await asyncio.to_thread(save, root / current["ledger_file"], ledger)
            if foreign:
                raise ValueError("Merge batch returned unexpected output paths; valid siblings are retained")
            if exhausted:
                return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(),
                                              error="Merge repair limit reached: " + ", ".join(map(str, exhausted)))
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
        state["completed_tasks"] += 1
        state["current"] = None
        if all(slot["status"] == "valid" for slot in ledger["slots"]):
            return await self._finish_merge_batch(job, root, state, ledger)
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._submit_merge_batch(job, root, state, current["ledger_file"], ledger)

    async def _resume_merge_batch(self, job, root, state):
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
                    repository, job, ValueError("Merge acceptance missing task identity")
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
