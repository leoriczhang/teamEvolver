"""Bounded Case batching over unchanged public Compile; each file is independently validated."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
from fastapi import HTTPException

from . import bounded_compile_plan as planning
from .compile_strategy import MAX_FILE_MAP_PACKETS
from .output_language import output_language_instruction
from .ov_case_group_orchestration import CASE_GROUP_PROTOCOL
from .ov_compile_mining import RUNS_ROOT, utc_now

CASE_BATCH_STAGE = "case_batch"
CASE_BATCH_DIRECTORY_PROTOCOL = "te-case-batch-directory.v1"
MAX_CASE_PACKETS = MAX_FILE_MAP_PACKETS
MAX_CASE_ATTEMPTS = 3


def case_source_roles(
    chunks: list[dict], scope: dict, *, root: Path | None = None, group_index: int | None = None,
) -> dict:
    """Distinguish method lineage from attached context without dropping either raw source."""
    if scope.get("source_units"):
        from .source_unit_scope import exact_case_groups

        if root is None:
            raise ValueError("Exact Case roles require the frozen source-unit archive")
        groups = exact_case_groups(root, scope)
        ids = [chunk["id"] for chunk in chunks]
        if group_index is None:
            matches = [group for group in groups if group["unit_ids"] == ids]
            if len(matches) != 1:
                raise ValueError("Exact Case roles need an unambiguous frozen method group")
            group = matches[0]
        else:
            if type(group_index) is not int or not 0 <= group_index < len(groups):
                raise ValueError("Exact Case roles require a valid frozen group index")
            group = groups[group_index]
            if group["unit_ids"] != ids:
                raise ValueError("Exact Case roles do not match the assigned source-unit group")
        method_set = set(group["operative_ids"])
        method_ids = [chunk["id"] for chunk in chunks if chunk["id"] in method_set]
    else:
        method_ordinals = set(scope.get("method_ordinals") or scope["ordinals"])
        method_ids = [chunk["id"] for chunk in chunks if chunk["ordinal"] in method_ordinals]
    if not method_ids:
        raise ValueError("A Case assignment needs original method Evidence, not only neighboring context")
    return {
        "method_input_ids": method_ids,
        "context_input_ids": [chunk["id"] for chunk in chunks if chunk["id"] not in method_ids],
    }


def _bounded_case_controls(chunks: list[dict], controls: dict, policy: dict) -> dict:
    """Project only non-Evidence history/diagnostics; keep exact source and Candidate controls intact."""
    checked = copy.deepcopy(controls)
    error = checked.get("repair_error", "")
    if error and len(planning._json(error)) > 2000:
        low, high = 0, len(error)
        while low < high:
            middle = (low + high + 1) // 2
            if len(planning._json(error[:middle])) <= 2000:
                low = middle
            else:
                high = middle - 1
        checked["repair_error"] = error[:low]
        checked["repair_error_excerpted"] = True  # Complete diagnostics remain in the durable slot ledger.
    while True:
        try:
            planning.build_stage_packet("case", "case-output-11.json", evidence_chunks=chunks,
                                        controls=checked, language_policy=policy, max_chars=32000 - 512)
            return checked
        except ValueError as exc:
            if not str(exc).startswith("Stage packet exceeds"):
                raise
            queries = checked.get("previous_queries", [])
            if not queries:
                raise ValueError("Exact Case Evidence and core controls exceed the bounded packet budget") from exc
            if any(queries):
                checked["previous_queries"] = [query[:len(query) // 2] for query in queries]
                checked["previous_queries_excerpted"] = True
            else:
                checked["previous_queries"] = []
                checked["previous_queries_omitted"] = True


def _host():
    from .ov_staged_orchestration import (
        _assignment_revision,
        _chunks,
        _json,
        _method_groups,
        _package,
        _prior_candidate_rows,
        _read,
        _save,
        _sha,
        _write,
    )

    return (
        _assignment_revision,
        _chunks,
        _json,
        _method_groups,
        _package,
        _prior_candidate_rows,
        _read,
        _save,
        _sha,
        _write,
    )


def _batch_ledger(root: Path, name: str, ids: list[str], case_index: int, prior_group_attempt: int = 0) -> dict:
    *_, read, save, _, _ = _host()
    path = root / "case-batches" / name / "ledger.json"
    if path.is_file():
        ledger = read(path)
        if ledger["case_ids"] != ids:
            raise ValueError("Frozen Case batch identities changed")
        return ledger
    ledger = {
        "case_ids": ids,
        "slots": [
            {
                "index": index,
                "case_id": identity,
                "status": "valid" if index < case_index else "pending",
                "format_attempts": 0,
                "group_attempt": 0,
                "error": "",
            }
            for index, identity in enumerate(ids)
        ],
    }
    if case_index < len(ids):
        ledger["slots"][case_index]["group_attempt"] = prior_group_attempt
    for slot in ledger["slots"][:case_index]:
        value = read(root / "cases" / name / f"{slot['index']:02d}.json")
        if value["case_id"] != slot["case_id"]:
            raise ValueError("Previously accepted Case identity changed")
    save(path, ledger)
    return ledger


def _batch_instruction(policy: dict) -> str:
    return (
        "Map each input file as one independent Case packet; "
        "emit exactly its packet.output_path with one complete Case JSON. Do not combine packets, "
        "generate a whole dataset, or cite another packet's Evidence. Finalize all individual files. "
        + output_language_instruction(policy)
    )


def _bounded_case_revision(root: Path, packet: dict, context: dict, published: str, instruction: str) -> dict:
    _, _, json_text, _, _, prior_rows, _, _, sha, _ = _host()
    limit = min(32000, 48000 - len(published) - len(instruction))
    prior = next(prior_rows(root, name=context["skill_name"]), None)
    revision_path = root / "revision.txt"
    revision_text = revision_path.read_text(encoding="utf-8") if revision_path.is_file() else ""
    start = revision_text.find("{")
    try:
        revision, _ = json.JSONDecoder().raw_decode(revision_text[start:]) if start >= 0 else ({}, 0)
        if not isinstance(revision, dict):
            revision = {}
    except ValueError:
        revision = {}
    feedback = str(revision.get("feedback") or (revision_text if not revision else ""))
    questions = [row for row in revision.get("review_questions") or [] if isinstance(row, dict)]
    note = {
        "revision_context_is_evidence": False,
        "full_revision_archived": True,
        "feedback": feedback[:2000],
        "feedback_truncated": len(feedback) > 2000,
        "review_questions": [
            {
                "qid": str(row.get("qid") or "")[:160],
                "qid_truncated": len(str(row.get("qid") or "")) > 160,
                "question": str(row.get("question") or "")[:200],
                "question_truncated": len(str(row.get("question") or "")) > 200,
            }
            for row in questions[:8]
        ],
        "review_questions_omitted": len(questions) > 8,
    }
    if prior:
        note.update(
            {
                "previous_candidate": {
                    "name": prior["name"],
                    "case_id": context.get("case_id", ""),
                    "case_ids_count": len(prior["case_ids"]),
                    "case_id_mapping_sha256": sha(json_text(prior["case_ids"])),
                    "skill_body_sha256": prior["skill_body_sha256"],
                    "skill_body_excerpt": prior["skill_body_excerpt"][:3000],
                    "skill_body_excerpt_truncated": len(prior["skill_body_excerpt"]) > 3000,
                },
                "complete_previous_candidate_archived": True,
            }
        )
    while True:
        packet["revision_context"] = json_text(note)
        if len(json_text(packet)) <= limit:
            return packet
        old = note.get("previous_candidate", {})
        if old.get("skill_body_excerpt"):
            excerpt = old["skill_body_excerpt"]
            old["skill_body_excerpt"] = excerpt[: len(excerpt) // 2]
            old["skill_body_excerpt_truncated"] = True
        elif note["review_questions"]:
            note["review_questions"].pop()
            note["review_questions_omitted"] = True
        elif note["feedback"]:
            note["feedback"] = note["feedback"][: len(note["feedback"]) // 2]
            note["feedback_truncated"] = True
        else:
            # Exact own Case ID already lives in controls; complete mappings and feedback remain archived.
            packet["revision_context"] = json_text(
                {
                    "revision_context_is_evidence": False,
                    "revision_context_omitted": True,
                    "full_revision_archived": True,
                    "complete_previous_candidate_archived": bool(prior),
                }
            )
            if len(json_text(packet)) > limit:
                raise ValueError("Independent Case packet needs a smaller lossless source assignment")
            return packet


def _verified_batch(root: Path, current: dict, ledger: dict | None = None):
    if current.get("input_protocol") == CASE_GROUP_PROTOCOL:
        from .ov_case_group_orchestration import verified_group_batch

        return verified_group_batch(root, current, ledger)
    _, _, json_text, _, _, _, read, _, sha, _ = _host()
    request = read(root / current["request_file"])
    body_path, manifest_path = root / current["compiler_file"], root / current["packet_file"]
    if body_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Frozen Case batch files cannot be symbolic links")
    body, manifest = body_path.read_text(encoding="utf-8"), manifest_path.read_text(encoding="utf-8")
    if (
        sha(json_text(request)) != current["request_sha256"]
        or sha(body) != current["compiler_sha256"]
        or sha(manifest) != current["packet_sha256"]
    ):
        raise ValueError("Frozen Case batch request changed")
    locators = json.loads(manifest)
    if not isinstance(locators, list) or not 1 <= len(locators) <= MAX_CASE_PACKETS:
        raise ValueError("Frozen Case batch needs one to eight independent assignments")
    if current.get("input_protocol") == CASE_BATCH_DIRECTORY_PROTOCOL:
        sources_valid = request["from"] == [current["input_uri"]]
    elif current.get("input_protocol") is None:
        sources_valid = request["from"] == [row["uri"] for row in locators]
    else:
        sources_valid = False
    if not sources_valid or len({row["uri"] for row in locators}) != len(locators):
        raise ValueError("Frozen Case batch source assignment changed")
    if request["to"] != current["output_uri"]:
        raise ValueError("Frozen Case batch destination changed")
    texts, indices, paths = [], set(), set()
    if ledger is not None:
        if (
            not isinstance(ledger, dict)
            or len(ledger["case_ids"]) != 12
            or len(set(ledger["case_ids"])) != 12
            or len(ledger["slots"]) != 12
        ):
            raise ValueError("Case batch ledger must retain twelve exact unique Case identities")
    for row in locators:
        context = row["context"]
        index = context["case_index"]
        if type(index) is not int or not 0 <= index < 12 or index in indices:
            raise ValueError("Case batch slots must have distinct exact ordinals")
        path = root / row["local_path"]
        folder = root / "children" / current["child"]
        if path.is_symlink() or path.resolve().parent != folder.resolve():
            raise ValueError("Frozen Case packet escapes its private child directory")
        text = path.read_text(encoding="utf-8")
        if sha(text) != row["sha256"] or len(text) > 32000:
            raise ValueError("Frozen independent Case packet changed")
        if len(body) + len(text) + len(request["instruction"]) > 48000:
            raise ValueError("Independent Case compiler envelope exceeds 48K characters")
        packet = json.loads(text)
        controls, chunks = packet["derived_controls"], packet["evidence"]
        planning.validate_chunks(chunks)
        scope = read(root / "scopes" / (context["skill_name"] + ".json"))
        roles = case_source_roles(chunks, scope, root=root, group_index=context["case_group_index"])
        if any(controls.get(key) != ids for key, ids in roles.items()):
            raise ValueError("Case packet changed its method/context Evidence roles")
        if (
            packet["stage"] != "case"
            or packet["output_path"] != f"case-output-{index:02d}.json"
            or row["output_path"] != packet["output_path"]
            or row["output_path"] in paths
            or row["uri"] != current["input_uri"] + f"/packet-{index:02d}.json"
            or context["skill_name"] != current["context"]["skill_name"]
            or controls["skill_name"] != context["skill_name"]
            or controls["case_id"] != context["case_id"]
            or packet["input_ids"] != [chunk["id"] for chunk in chunks]
            or context["input_ids"] != packet["input_ids"]
            or controls["input_ids"] != packet["input_ids"]
            or controls["allowed_source_refs"] != context["source_refs"]
            or context["source_refs"] != list(dict.fromkeys(chunk["source_uri"] for chunk in chunks))
        ):
            raise ValueError("Case packet changed its exact Case, source or output assignment")
        for chunk in chunks:
            if "unit_index" in chunk:
                from .source_units import load_source_unit

                if load_source_unit(root, chunk["id"]) != chunk:
                    raise ValueError("Case exact source unit changed")
                continue
            ordinal = read(root / "chunk-ids" / (chunk["id"] + ".json"))["ordinal"]
            if read(root / "chunks" / f"{ordinal:08d}.json") != chunk:
                raise ValueError("Case packet original Evidence changed")
        if ledger is not None:
            slot = ledger["slots"][index]
            if (
                slot["index"] != index
                or slot["case_id"] != context["case_id"]
                or ledger["case_ids"][index] != context["case_id"]
                or slot["status"] not in {"pending", "valid"}
            ):
                raise ValueError("Case packet no longer matches its durable slot identity")
        indices.add(index)
        paths.add(row["output_path"])
        texts.append(text)
    return request, body, locators, texts


class OvCaseBatchOrchestrationMixin:
    def _staged_cases_batched(self) -> bool:
        """New work uses bounded batches; legacy frozen current tasks still use the serial receiver."""
        return True

    async def _verify_case_batch_input_directory(self, job: dict, current: dict, locators: list[dict]):
        """Match Native inventory visibility: hidden files are skipped, every directory is visible."""
        *_, sha, _ = _host()
        expected = {row["uri"]: row for row in locators}
        found, offset, last_key = set(), 0, None
        root = current["input_uri"].rstrip("/")
        while True:
            try:
                result = await self._knowledge_request(
                    job["account"], "GET", "/api/v1/fs/ls", params={
                        "uri": root, "output": "original", "recursive": "false", "show_all_hidden": "false",
                        "node_limit": 64, "offset": offset, "sort_by": "name", "sort_order": "asc",
                    }, timeout=20.0,
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ValueError("Frozen Case input directory is missing") from exc
                raise
            page = result
            if isinstance(result, dict):
                if any(result.get(flag) for flag in ("partial", "truncated", "incomplete")):
                    raise ValueError("Case input directory listing is incomplete")
                page = result.get("entries", result.get("items"))
            if not isinstance(page, list) or len(page) > 64:
                raise ValueError("Case input directory must return a bounded complete entry page")
            for entry in page:
                if not isinstance(entry, dict) or entry.get("access") == "denied":
                    raise ValueError("Case input directory has an inaccessible or invalid entry")
                uri = entry.get("uri")
                name = uri[len(root) + 1:] if isinstance(uri, str) and uri.startswith(root + "/") else ""
                is_dir = entry.get("isDir", entry.get("is_dir"))
                if (not name or "/" in name.rstrip("/") or "\\" in name or name in {".", ".."}
                        or type(is_dir) is not bool):
                    raise ValueError("Case input directory contains an invalid non-child entry")
                key = (not is_dir, name.lower(), name)
                if last_key is not None and key <= last_key:
                    raise ValueError("Case input directory pagination repeated or reordered entries")
                last_key = key
                if is_dir:
                    raise ValueError("Case input directory cannot contain subdirectories, including hidden ones")
                if name.startswith("."):
                    continue
                if uri not in expected or uri in found:
                    raise ValueError("Case input directory contains an unexpected or duplicate packet file")
                found.add(uri)
            if len(page) < 64:
                break
            offset += len(page)
        if found != set(expected):
            raise ValueError("Case input directory differs from its complete frozen packet manifest")
        for uri, row in expected.items():
            try:
                result = await self._knowledge_request(
                    job["account"], "GET", "/api/v1/content/read",
                    params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"}, timeout=20.0,
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise ValueError("Frozen remote Case packet is missing") from exc
                raise
            if isinstance(result, str):
                text = result
            elif isinstance(result, dict):
                text = result.get("content", result.get("text", ""))
            else:
                raise ValueError("Remote Case packet must return its exact frozen text")
            if not isinstance(text, str) or sha(text) != row["sha256"]:
                raise ValueError("Remote Case packet differs from its frozen local content")

    async def _supersede_case_batch_file_sources(self, job: dict, root: Path, state: dict,
                                                request: dict, locators: list[dict], texts: list[str]):
        """Called only after exact-key lookup returned 404; retain every old frozen byte and slot."""
        _, _, json_text, _, package, _, read, save, sha, _ = _host()
        old = copy.deepcopy(state["current"])
        if old.get("task_id") or old.get("input_protocol") is not None:
            raise ValueError("Only an unaccepted legacy file-source request can be superseded")
        body = await asyncio.to_thread((root / "aux-compilers/case_batch.md").read_text, encoding="utf-8")
        _, published = package(body)
        if (sha(body) != old["compiler_source_sha256"] or sha(body) != state["aux_compilers"][CASE_BATCH_STAGE]
                or sha(published) != old["compiler_sha256"]):
            raise ValueError("Legacy Case batch supersession must preserve its exact frozen compiler")
        contexts = [row["context"] for row in locators]
        digest = sha(json_text([
            CASE_BATCH_DIRECTORY_PROTOCOL, state["round"], contexts, [sha(text) for text in texts], sha(published),
        ]))[:24]
        audit = {
            "schema_version": "te-case-batch-source-supersession.v1", "old_current": old,
            "old_request": request, "submission_lookup": "not_found", "lookup_key": old["key"],
            "replacement_child": CASE_BATCH_STAGE + "-" + digest,
            "replacement_protocol": CASE_BATCH_DIRECTORY_PROTOCOL,
            "reason": "Public Compile requires directory sources; the exact old key has no accepted task",
            "case_ids": [row["context"]["case_id"] for row in locators],
            "case_slots_and_attempts_preserved": True,
        }
        path = root / "case-batches" / "superseded" / (old["child"] + ".json")
        if path.is_file():
            if await asyncio.to_thread(read, path) != audit:
                raise ValueError("Legacy Case batch supersession audit changed")
        else:
            await asyncio.to_thread(save, path, audit)
        return await self._submit_case_batch(job, root, state, [json.loads(text) for text in texts], contexts)

    async def _staged_case_batch_next(self, job: dict, root: Path, state: dict, candidate: dict, scope: dict,
                                     *, force_single_cases: bool = False):
        if state.get("case_group_protocol") == CASE_GROUP_PROTOCOL and not force_single_cases:
            from .ov_case_group_orchestration import next_case_groups

            return await next_case_groups(self, job, root, state, candidate, scope)
        _, chunks_at, json_text, method_groups, package, prior_rows, read, save, sha, _ = _host()
        name = candidate["name"]
        prior = await asyncio.to_thread(lambda: next(prior_rows(root, name=name), None))
        prior_ids = prior["case_ids"] if prior else []
        ids = prior_ids if len(prior_ids) == 12 else [f"{name}-case-{index + 1:02d}" for index in range(12)]
        ledger = await asyncio.to_thread(
            _batch_ledger, root, name, ids, int(state["case_index"]), int(state.get("case_group_attempt") or 0)
        )
        pending = [slot for slot in ledger["slots"] if slot["status"] != "valid"]
        if not pending:
            state.pop("writer_pending_ordinals", None)
            state.update(
                phase="skill_section",
                candidate_index=state["candidate_index"] + 1,
                cursor=0,
                section_count=0,
                case_index=0,
                case_group_attempt=0,
            )
            job = await self._staged_save(job, root, state)
            return await self._staged_next(job, root, state)
        if force_single_cases:
            # Repair only slots whose affected group reached the original one-Case receiver.
            pending = [slot for slot in pending if slot.get("group_limit") == 1]
            if not pending:
                raise ValueError("Case group fallback requires an explicit pending single-Case slot")
        groups = await asyncio.to_thread(method_groups, root, scope)
        from .compile_strategy import render_case_batch_compiler

        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if sha(base) != state["base_sha256"]:
            raise ValueError("Frozen base compiler changed")
        await self._ensure_aux_compiler(root, state, CASE_BATCH_STAGE, render_case_batch_compiler(base))
        body = await asyncio.to_thread((root / "aux-compilers" / "case_batch.md").read_text, encoding="utf-8")
        previous = [
            await asyncio.to_thread(read, root / "cases" / name / f"{slot['index']:02d}.json")
            for slot in ledger["slots"]
            if slot["status"] == "valid"
        ]
        packets, contexts = [], []
        for slot in pending[:MAX_CASE_PACKETS]:
            ordinal = slot["index"]
            group_index = (ordinal * len(groups) // 12 + slot["group_attempt"]) % len(groups)
            chunks = await asyncio.to_thread(chunks_at, root, groups[group_index])
            context = {
                "skill_name": name,
                "case_id": slot["case_id"],
                "case_index": ordinal,
                "case_group_index": group_index,
                "case_group_count": len(groups),
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                "format_attempts": slot["format_attempts"],
                "group_attempt": slot["group_attempt"],
            }
            controls = {
                "candidate": {key: candidate[key] for key in ("name", "task", "boundary", "trigger")},
                "skill_name": name,
                "case_id": slot["case_id"],
                "allowed_source_refs": context["source_refs"],
                "input_ids": context["input_ids"],
                **case_source_roles(chunks, scope, root=root, group_index=group_index),
                "coverage_dimension": planning.CASE_DIMENSIONS[ordinal],
                "previous_queries": [case["query"][:300] for case in previous],
                "previous_queries_excerpted": any(len(case["query"]) > 300 for case in previous),
                "source_scope_rule": "Only this packet's exact original Evidence may support this Case. "
                "Other packets are separate assignments. Missing runtime inputs are not missing business rules.",
                "case_batch_size": min(MAX_CASE_PACKETS, len(pending)),
            }
            if slot["error"]:
                controls["repair_error"] = slot["error"][:1000]
            from .source_scope import EFFICIENCY_PROTOCOL
            if state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                controls = _bounded_case_controls(chunks, controls, state["language_policy"])
            packet = planning.build_stage_packet(
                "case",
                f"case-output-{ordinal:02d}.json",
                evidence_chunks=chunks,
                controls=controls,
                language_policy=state["language_policy"],
            )
            _, published = package(body)
            packet = await asyncio.to_thread(
                _bounded_case_revision, root, packet, context, published, _batch_instruction(state["language_policy"])
            )
            packets.append(packet)
            contexts.append(context)
        return await self._submit_case_batch(job, root, state, packets, contexts)

    async def _submit_case_batch(self, job: dict, root: Path, state: dict, packets: list[dict], contexts: list[dict],
                                 *, compiler_stage: str = CASE_BATCH_STAGE,
                                 input_protocol: str = CASE_BATCH_DIRECTORY_PROTOCOL,
                                 instruction: str | None = None):
        _, _, json_text, _, package, _, _, save, sha, write = _host()
        if not 1 <= len(packets) <= MAX_CASE_PACKETS or len(packets) != len(contexts):
            raise ValueError("Case batch accepts one to eight independent packets")
        body = await asyncio.to_thread((root / "aux-compilers" / (compiler_stage + ".md")).read_text, encoding="utf-8")
        if sha(body) != state["aux_compilers"][compiler_stage]:
            raise ValueError("Frozen Case batch compiler changed")
        compiler_name, published = package(body)
        texts = [json_text(packet) for packet in packets]
        if any(len(text) > 32000 for text in texts):
            raise ValueError("Each Case packet must fit its own 32K character input budget")
        digest = sha(json_text([
            input_protocol, state["round"], contexts, [sha(text) for text in texts], sha(published),
        ]))[:24]
        child = CASE_BATCH_STAGE + "-" + digest
        folder = "children/" + child
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{job['current_round']}/stages/{child}/input"
        output_uri = input_uri.rsplit("/", 1)[0] + "/output"
        locators = []
        for packet, context, text in zip(packets, contexts, texts, strict=True):
            filename = f"packet-{context['case_index']:02d}.json"
            local_path = folder + "/" + filename
            await asyncio.to_thread(write, root / local_path, text)
            locators.append(
                {
                    "uri": input_uri + "/" + filename,
                    "local_path": local_path,
                    "sha256": sha(text),
                    "output_path": packet["output_path"],
                    "context": context,
                }
            )
        request = {
            "from": [input_uri],
            "to": output_uri,
            "skill": "viking://agent/skills/" + compiler_name,
            "instruction": instruction if instruction is not None else _batch_instruction(state["language_policy"]),
        }
        await asyncio.to_thread(save, root / folder / "packets.json", locators)
        await asyncio.to_thread(save, root / folder / "request.json", request)
        await asyncio.to_thread(write, root / folder / "compiler.md", published)
        state["current"] = {
            "stage": CASE_BATCH_STAGE,
            "input_protocol": input_protocol,
            "child": child,
            "context": {"skill_name": contexts[0]["skill_name"]},
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
        state["attempted_tasks"] = int(state.get("attempted_tasks", 0)) + 1
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
        return await self._resume_case_batch(job, root, state)

    async def _resume_case_batch(self, job: dict, root: Path, state: dict):
        _, _, json_text, _, _, _, read, save, sha, _ = _host()
        current, repository = state["current"], self._ov_skill_mining_repository()
        started = time.perf_counter()
        try:
            request, body, locators, packets = await asyncio.to_thread(_verified_batch, root, current)
            accepted = {"task_id": current["task_id"]} if current.get("task_id") else None
            lookup_not_found = False
            if accepted is None:
                try:
                    accepted = await self._knowledge_request(
                        job["account"],
                        "GET",
                        f"/api/v1/compile/submissions/{current['key']}",
                        timeout=20.0,
                    )
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    lookup_not_found = True
            job = await asyncio.to_thread(repository.get_job, job["job_id"])
            if accepted is None and self._staged_stopped(job):
                await self._release_staged_compile_slot(job, key=current["key"])
                return await asyncio.to_thread(
                    repository.update_job, job["job_id"], status="stopped", finished_at=utc_now()
                )
            if accepted is None:
                if current.get("input_protocol") is None:
                    if not lookup_not_found:
                        raise ValueError("Legacy Case batch needs a confirmed exact-key lookup before supersession")
                    return await self._supersede_case_batch_file_sources(job, root, state, request, locators, packets)
                if not await self._acquire_staged_compile_slot(job, state, current):
                    return await self._staged_save(job, root, state, status="preparing", ov_task_id="")
                client = self._round_client(job)
                await self._round_provision_compiler(client, request["skill"].rsplit("/", 1)[-1], body)
                stage_root = current["input_uri"].rsplit("/", 2)[0]
                for directory in (stage_root, current["input_uri"].rsplit("/", 1)[0], current["input_uri"]):
                    await client.mkdir(directory)
                for row, text in zip(locators, packets, strict=True):
                    await self._round_write_immutable(job["account"], row["uri"], text)
                await self._verify_case_batch_input_directory(job, current, locators)
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
            if not isinstance(accepted, dict):
                return await self._round_submission_retry(
                    repository, job, ValueError("Case batch acknowledgement was invalid; recover the exact request key")
                )
            current["task_id"] = str(accepted.get("task_id") or "")
            if not current["task_id"]:
                return await self._round_submission_retry(
                    repository, job,
                    ValueError("Case batch acknowledgement had no task identity; recover the exact key"),
                )
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
            # A recovered lost ACK is already an accepted task even if its input was later changed.
            # Persist that identity before any validation failure so cancellation and audit remain exact.
            if current.get("input_protocol") in {CASE_BATCH_DIRECTORY_PROTOCOL, CASE_GROUP_PROTOCOL} \
                    and not self._staged_stopped(job):
                await self._verify_case_batch_input_directory(job, current, locators)
            return job
        except (httpx.HTTPError, OSError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                recovered = await self._staged_release_rejected(job, root, state, exc)
                if recovered is not None:
                    return recovered
                raise
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            await self._staged_release_rejected(job, root, state, exc)
            return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())

    async def _advance_case_batch(self, job: dict, root: Path, state: dict, task: dict):
        _, _, _, _, _, _, read, save, _, write = _host()
        current = state["current"]
        if str(task.get("status") or "") not in {"completed", "succeeded", "failed"}:
            return job
        repository = self._ov_skill_mining_repository()
        name = current["context"]["skill_name"]
        ledger_path = root / "case-batches" / name / "ledger.json"
        try:
            ledger = await asyncio.to_thread(read, ledger_path)
            _, _, locators, _ = await asyncio.to_thread(_verified_batch, root, current, ledger)
            grouped = current.get("input_protocol") == CASE_GROUP_PROTOCOL
            if grouped and task.get("task_id") != current.get("task_id"):
                raise ValueError("Case group completion must match the exact accepted Native task")
            if current.get("input_protocol") in {CASE_BATCH_DIRECTORY_PROTOCOL, CASE_GROUP_PROTOCOL}:
                await self._verify_case_batch_input_directory(job, current, locators)
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
            expected = {current["output_uri"] + "/" + row["output_path"] for row in locators}
            unknown = set(files) - expected
            global_error = bool(unknown or len(files) != len(set(files)))
            group_outputs = {}
            if grouped:
                from .ov_case_group_orchestration import read_group_outputs

                locators, group_outputs = await read_group_outputs(self, job, root, current, locators, files)
            exhausted, missing_evidence = [], None
            for row in sorted(locators, key=lambda item: item["context"]["case_index"]):
                context = row["context"]
                slot = ledger["slots"][context["case_index"]]
                audit = root / "case-batches" / name / "outcomes" / (current["child"] + f"-{slot['index']:02d}.json")
                # An already durable valid sibling is never regenerated or penalized during recovery.
                if slot["status"] == "valid":
                    if grouped and audit.is_file():
                        committed = await asyncio.to_thread(read, audit)
                        accepted_case = await asyncio.to_thread(
                            read, root / "cases" / name / f"{slot['index']:02d}.json"
                        )
                        if (committed["context"] != context or committed["status"] != "valid"
                                or committed["case"] != accepted_case):
                            raise ValueError("Previously durable grouped Case or audit changed")
                    continue
                if audit.is_file():
                    committed = await asyncio.to_thread(read, audit)
                    if committed["context"] != context:
                        raise ValueError("Durable Case audit changed its frozen assignment")
                    slot.update(committed["slot"])
                    if committed["status"] == "valid":
                        if grouped:
                            planning.validate_case(
                                committed["case"], case_id=context["case_id"], skill_name=name,
                                source_refs=context["source_refs"],
                            )
                            if slot["case_id"] != context["case_id"] or slot["index"] != context["case_index"]:
                                raise ValueError("Durable grouped Case audit changed its exact slot identity")
                        await asyncio.to_thread(
                            save, root / "cases" / name / f"{slot['index']:02d}.json", committed["case"]
                        )
                    elif committed["status"] == "deferred" and slot["group_attempt"] >= context["case_group_count"]:
                        missing_evidence = committed["error"]
                    elif committed["status"] == "invalid" and slot["format_attempts"] >= MAX_CASE_ATTEMPTS:
                        exhausted.append(slot["case_id"])
                    await asyncio.to_thread(save, ledger_path, ledger)
                    continue
                error, deferred, capacity = "", False, False
                content = ""
                try:
                    if grouped:
                        content, group_error, capacity = group_outputs[slot["index"]]
                        if group_error:
                            raise ValueError(group_error)
                    else:
                        uri = current["output_uri"] + "/" + row["output_path"]
                        if uri not in files:
                            raise ValueError("Expected independent Case output is missing")
                        try:
                            response = await self._knowledge_request(
                                job["account"],
                                "GET",
                                "/api/v1/content/read",
                                params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"},
                                timeout=20.0,
                            )
                        except HTTPException as exc:
                            if exc.status_code != 404:
                                raise
                            raise ValueError("Expected Case output disappeared") from exc
                        content = (
                            response if isinstance(response, str) else response.get("content", response.get("text", ""))
                        )
                    if not isinstance(content, str) or not content.strip() or len(content) > planning.MAX_OUTPUT_CHARS:
                        raise ValueError("Independent Case output is empty or exceeds 12K characters")
                    if not grouped:
                        await asyncio.to_thread(
                            write, root / "children" / current["child"] / (row["output_path"] + ".raw"), content
                        )
                    value = json.loads(content)
                    if planning.is_deferred(value):
                        value = planning.validate_deferred(value, source_refs=context["source_refs"])
                        deferred = True
                        raise ValueError(value["reason"])
                    value, _ = planning.normalize_case_materials(value)
                    value = planning.validate_case(
                        value, case_id=context["case_id"], skill_name=name, source_refs=context["source_refs"]
                    )
                    query = planning.normalize_case_query(value["query"])
                    for previous in ledger["slots"]:
                        if previous["status"] == "valid":
                            accepted_case = await asyncio.to_thread(
                                read, root / "cases" / name / f"{previous['index']:02d}.json"
                            )
                            if planning.normalize_case_query(accepted_case["query"]) == query:
                                raise ValueError("Case query duplicates an already accepted substantive query")
                    # Write a transaction record before the Case/ledger writes; recovery can replay it.
                    await asyncio.to_thread(
                        save,
                        audit,
                        {
                            "status": "valid",
                            "case": value,
                            "context": context,
                            "slot": {**slot, "status": "valid", "error": ""},
                        },
                    )
                    await asyncio.to_thread(save, root / "cases" / name / f"{slot['index']:02d}.json", value)
                    slot.update(status="valid", error="")
                except (ValueError, KeyError, TypeError) as exc:
                    error = str(exc)[:1000]
                    if deferred:
                        slot["group_attempt"] += 1
                        if slot["group_attempt"] >= context["case_group_count"]:
                            missing_evidence = error
                    elif not capacity:
                        slot["format_attempts"] += 1
                        if slot["format_attempts"] >= MAX_CASE_ATTEMPTS:
                            exhausted.append(slot["case_id"])
                    slot.update(status="pending", error=error)
                    if grouped and not deferred:
                        slot["group_limit"] = max(1, row["group_width"] // 2)
                    await asyncio.to_thread(
                        save,
                        audit,
                        {
                            "status": "deferred" if deferred else "capacity" if capacity else "invalid",
                            "error": error,
                            "context": context,
                            "raw": content,
                            "slot": copy.deepcopy(slot),
                        },
                    )
                await asyncio.to_thread(save, ledger_path, ledger)
            if global_error:
                raise ValueError(
                    "Case batch generated unexpected or duplicate output paths; valid siblings are retained"
                )
            if missing_evidence:
                await self._staged_defer_candidate(job, root, state, name, missing_evidence)
            elif exhausted:
                return await self._staged_save(
                    job,
                    root,
                    state,
                    status="failed",
                    finished_at=utc_now(),
                    error="Case format repair limit reached: " + ", ".join(exhausted),
                )
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(), error=str(exc))
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            # No extra output is silently accepted; keep all previously durable Cases.
            return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(), error=str(exc))
        state["completed_tasks"] += 1
        state["current"] = None
        if not missing_evidence:
            state["case_index"] = next((slot["index"] for slot in ledger["slots"] if slot["status"] != "valid"), 12)
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)
