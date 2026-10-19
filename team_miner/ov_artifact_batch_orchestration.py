"""One durable Native task containing at most eight independent artifact file assignments."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi import HTTPException

from . import bounded_compile_plan as planning
from .compile_strategy import MAX_FILE_MAP_PACKETS
from .output_language import output_language_instruction, validate_artifact_headings
from .ov_case_batch_orchestration import _host
from .ov_compile_mining import RUNS_ROOT, utc_now
from .skill_body import PROTOCOL as SKILL_BODY_PROTOCOL
from .skill_body import iter_integrated_skill, previous_body
from .skill_body_patch import PROTOCOL as BODY_PATCH_PROTOCOL

ARTIFACT_BATCH_STAGE = "artifact_batch"
ARTIFACT_BATCH_PROTOCOL = "te-artifact-file-batch.v1"
MAX_ARTIFACT_PACKETS = MAX_FILE_MAP_PACKETS
MAX_ARTIFACT_ATTEMPTS = 3


def _instruction(policy: dict) -> str:
    return (
        "本 Compile 包含最多八份互相独立的 packet 文件。每个 file Map 只处理自己文件，"
        "只输出该 packet.output_path 对应的一个完整文件。不得合并、读取或引用兄弟 packet。"
        "FileResponse.inputs 仅当前 file Map 的原生输入 IDs；内容内 input_ids 使用该 packet 的逻辑 IDs。"
        "不同输出路径保持独立；Finalize 收集结果，TE 逐文件验收。" + output_language_instruction(policy)
    )


def _ledger(root: Path, name: str) -> dict:
    *_, read, _, _, _ = _host()
    path = root / "artifact-batches" / "candidates" / (name + ".json")
    return (
        read(path)
        if path.exists()
        else {
            "name": name,
            "cursor": 0,
            "section_count": 0,
            "skill_ready": False,
            "evaluation_ready": False,
            "reused": False,
        }
    )


def _verify(root: Path, current: dict):
    _, _, json_text, _, _, _, read, _, sha, _ = _host()
    request = read(root / current["request_file"])
    body = (root / current["compiler_file"]).read_text(encoding="utf-8")
    manifest = (root / current["packet_file"]).read_text(encoding="utf-8")
    if (
        sha(json_text(request)) != current["request_sha256"]
        or sha(body) != current["compiler_sha256"]
        or sha(manifest) != current["packet_sha256"]
    ):
        raise ValueError("Frozen artifact batch changed")
    rows = json.loads(manifest)
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ARTIFACT_PACKETS:
        raise ValueError("Artifact batch needs one to eight independent files")
    if (
        current.get("input_protocol") != ARTIFACT_BATCH_PROTOCOL
        or request["from"] != [current["input_uri"]]
        or request["to"] != current["output_uri"]
    ):
        raise ValueError("Artifact batch directory assignment changed")
    outputs, texts = set(), []
    for index, row in enumerate(rows):
        path = root / row["local_path"]
        if path.is_symlink() or path.resolve().parent != (root / "children" / current["child"]).resolve():
            raise ValueError("Artifact packet escaped its child directory")
        text = path.read_text(encoding="utf-8")
        context, packet = row["context"], json.loads(text)
        chunks, controls = packet["evidence"], packet["derived_controls"]
        if (
            sha(text) != row["sha256"]
            or len(text) > 32000
            or len(body) + len(text) + len(request["instruction"]) > 48000
            or packet["stage"] != current["artifact_stage"]
            or context["stage"] != current["artifact_stage"]
            or row["uri"] != current["input_uri"] + f"/packet-{index:02d}.json"
            or row["output_path"] != packet["output_path"]
            or row["output_path"] in outputs
            or packet["input_ids"] != context["input_ids"]
            or controls["input_ids"] != context["input_ids"]
            or controls["skill_name"] != context["skill_name"]
            or context["input_ids"] != [chunk["id"] for chunk in chunks]
            or context["source_refs"] != list(dict.fromkeys(chunk["source_uri"] for chunk in chunks))
            or controls["allowed_source_refs"] != context["source_refs"]
        ):
            raise ValueError("Artifact packet changed its exact source, candidate or output")
        planning.validate_chunks(chunks)
        if current.get("skill_body_protocol") == SKILL_BODY_PROTOCOL and context["stage"] == "skill_section":
            if controls.get("previous_body") != previous_body(root, context["skill_name"], context["section_index"]):
                raise ValueError("Frozen Skill working copy changed")
            if context.get("body_insert_protocol"):
                from .skill_body_insert import controls as insert_controls

                if controls.get("body_insert") != insert_controls(controls["previous_body"], context["input_ids"]):
                    raise ValueError("Frozen body insertion assignment changed")
        if current.get("skill_body_protocol") == BODY_PATCH_PROTOCOL and context["stage"] == "skill_section":
            from .ov_body_patch_orchestration import verify_body_assignment

            verify_body_assignment(root, context, packet)
        for chunk in chunks:
            if "unit_index" in chunk:
                from .source_units import load_source_unit

                if load_source_unit(root, chunk["id"]) != chunk:
                    raise ValueError("Artifact exact source unit changed")
                continue
            ordinal = read(root / "chunk-ids" / (chunk["id"] + ".json"))["ordinal"]
            if read(root / "chunks" / f"{ordinal:08d}.json") != chunk:
                raise ValueError("Artifact packet original Evidence changed")
        outputs.add(row["output_path"])
        texts.append(text)
    return request, body, rows, texts


class OvArtifactBatchMixin:
    async def _artifact_candidate_ready(self, job: dict, root: Path, state: dict, candidate: dict, ledger: dict):
        _, _, _, _, _, _, read, save, _, _ = _host()
        name = candidate["name"]
        scope = await asyncio.to_thread(read, root / "scopes" / (name + ".json"))
        if ledger["skill_ready"] or ledger["cursor"] < len(scope["ordinals"]):
            return
        metadata = await asyncio.to_thread(
            lambda: [read(root / "chunk-metadata" / f"{ordinal:08d}.json") for ordinal in scope["ordinals"]]
        )
        ids = [row["id"] for row in metadata]
        sections = (read(root / "sections" / name / f"{index:08d}.json") for index in range(ledger["section_count"]))
        if state.get("skill_body_protocol") == SKILL_BODY_PROTOCOL:
            rendered = iter_integrated_skill(candidate, sections, expected_input_ids=ids,
                                            source_refs=list(dict.fromkeys(row["source_uri"] for row in metadata)))
        else:
            rendered = planning.iter_skill_chunks(candidate, sections, expected_input_ids=ids)
        try:
            await asyncio.to_thread(
                self._ov_skill_mining_repository().write_output_stream,
                job["job_id"],
                f"compiled_skill/{name}/SKILL.md",
                rendered,
            )
            ledger["skill_ready"] = True
        except planning.NoOperationalSkillSectionsError:
            reason = (
                "完整关联原文范围未提供可执行的方法步骤"
                if state["language_policy"].get("language") == "zh-CN"
                else "No executable method steps across the complete assigned source scope."
            )
            await self._staged_defer_candidate(job, root, state, name, reason)
            ledger["deferred"] = True
        await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"), ledger)

    async def _staged_artifact_batch_next(self, job: dict, root: Path, state: dict):
        if state.get("skill_body_protocol") == BODY_PATCH_PROTOCOL and not state.get("body_patch_complete"):
            from .ov_body_patch_orchestration import next_body_batch

            return await next_body_batch(self, job, root, state)
        _, chunks_at, _, method_groups, package, _, read, save, sha, _ = _host()
        plan = await asyncio.to_thread(read, root / "plan.json")
        candidates = plan["skills"]
        # Completed candidates have independent journals. Replaying this scan only recreates
        # deterministic outputs; it never re-submits an accepted successful assignment.
        for candidate in candidates:
            ledger = await asyncio.to_thread(_ledger, root, candidate["name"])
            if state.get("round_reuse_protocol") and not ledger.get("reuse_checked"):
                from .round_reuse import try_reuse_candidate

                scope = await asyncio.to_thread(read, root / "scopes" / (candidate["name"] + ".json"))
                reused = await asyncio.to_thread(
                    try_reuse_candidate, job, root, state, candidate, scope, self._ov_skill_mining_repository()
                )
                ledger["reuse_checked"] = True
                if reused:
                    ledger.update(skill_ready=True, evaluation_ready=True, reused=True, cursor=len(scope["ordinals"]))
                await asyncio.to_thread(
                    save, root / "artifact-batches/candidates" / (candidate["name"] + ".json"), ledger
                )
            await self._artifact_candidate_ready(job, root, state, candidate, ledger)
        plan = await asyncio.to_thread(read, root / "plan.json")
        candidates = plan["skills"]
        phase = "skill_section"
        all_ready = True
        for row in candidates:
            if not (await asyncio.to_thread(_ledger, root, row["name"]))["skill_ready"]:
                all_ready = False
                break
        if all_ready:
            phase = "evaluation"
        packets, contexts = [], []
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if sha(base) != state["base_sha256"]:
            raise ValueError("Frozen base compiler changed")
        from .compile_strategy import render_stage_batch_compiler

        integrated = state.get("skill_body_protocol") == SKILL_BODY_PROTOCOL
        insert_enabled = integrated and state.get("body_insert_protocol") and phase == "skill_section"
        key = phase + ("_insert_batch" if insert_enabled else "_batch")
        await self._ensure_aux_compiler(
            root, state, key, render_stage_batch_compiler(base, phase, integrated_body=integrated,
                                                        body_insert=bool(insert_enabled))
        )
        body = await asyncio.to_thread((root / "aux-compilers" / (key + ".md")).read_text, encoding="utf-8")
        _, published = package(body)
        for candidate in candidates:
            name = candidate["name"]
            ledger = await asyncio.to_thread(_ledger, root, name)
            if (
                ledger.get("deferred")
                or ledger["evaluation_ready"]
                or (phase == "skill_section" and ledger["skill_ready"])
            ):
                continue
            scope = await asyncio.to_thread(read, root / "scopes" / (name + ".json"))
            if phase == "skill_section":
                from .ov_staged_orchestration import _raw_group

                chunks, after = await asyncio.to_thread(_raw_group, root, scope["ordinals"], ledger["cursor"])
                if ledger.get("body_max_chunks"):
                    chunks = chunks[:ledger["body_max_chunks"]]
                    after = ledger["cursor"] + len(chunks)
                output = f"{name}-section-{ledger['section_count']:08d}.json"
                context = {"after": after, "before": ledger["cursor"], "section_index": ledger["section_count"]}
            else:
                groups = await asyncio.to_thread(method_groups, root, scope)
                chunks = await asyncio.to_thread(chunks_at, root, groups[0])
                output = name + "-evaluation.md"
                context = {}
            context.update(
                stage=phase,
                skill_name=name,
                input_ids=[chunk["id"] for chunk in chunks],
                source_refs=list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                ordinals=[chunk["ordinal"] for chunk in chunks],
                format_attempts=int(ledger.get("format_attempts") or 0),
            )
            controls = {
                "candidate": {field: candidate[field] for field in ("name", "task", "boundary", "trigger")},
                "skill_name": name,
                "input_ids": context["input_ids"],
                "allowed_source_refs": context["source_refs"],
                "source_scope_rule": "Only this packet's original Evidence supports this independent file. "
                "Absence in this bounded fragment is not a whole-method Evidence gap.",
            }
            if ledger.get("error"):
                controls["repair_error"] = ledger["error"][:1000]
            from .identity_components import controls as component_controls
            controls.update(await asyncio.to_thread(component_controls, root, name))
            if integrated and phase == "skill_section":
                controls["previous_body"] = await asyncio.to_thread(previous_body, root, name, ledger["section_count"])
                controls["source_scope_rule"] = (
                    "Current raw Evidence supports additions/corrections. The complete previous working copy is "
                    "generated context from this candidate and round, not new Evidence. Preserve its supported "
                    "rules and citations while integrating the current batch; never concatenate duplicate workflows."
                )
                if insert_enabled and controls["previous_body"]["content"]:
                    from .skill_body_insert import PROTOCOL
                    from .skill_body_insert import controls as insert_controls

                    context["body_insert_protocol"] = PROTOCOL
                    controls["body_insert"] = insert_controls(controls["previous_body"], context["input_ids"])
            while True:
                try:
                    packet = planning.build_stage_packet(
                        phase, output, evidence_chunks=chunks, controls=controls,
                        language_policy=state["language_policy"],
                        max_chars=29000 if integrated and phase == "skill_section" else 32000,
                    )
                    break
                except ValueError as exc:
                    if (not str(exc).startswith("Stage packet exceeds") or not integrated
                            or phase != "skill_section" or len(chunks) == 1):
                        raise
                    # Shrink the current assignment, never the working copy or source text.
                    # The next cursor resumes at the first unassigned original chunk.
                    chunks = chunks[:-1]
                    context.update(after=ledger["cursor"] + len(chunks),
                                   input_ids=[chunk["id"] for chunk in chunks],
                                   source_refs=list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                                   ordinals=[chunk["ordinal"] for chunk in chunks])
                    controls.update(input_ids=context["input_ids"], allowed_source_refs=context["source_refs"])
                    if context.get("body_insert_protocol"):
                        controls["body_insert"] = insert_controls(controls["previous_body"], context["input_ids"])
            # Reuse the bounded revision projection, preserving current feedback and
            # only this candidate's prior identity; other candidates are not Evidence.
            from .ov_case_batch_orchestration import _bounded_case_revision

            packet = await asyncio.to_thread(
                _bounded_case_revision, root, packet, context, published, _instruction(state["language_policy"])
            )
            packets.append(packet)
            contexts.append(context)
            if len(packets) == MAX_ARTIFACT_PACKETS:
                break
        if not packets:
            state["artifact_batches_complete"] = True
            state.update(phase="case", candidate_names=[row["name"] for row in candidates],
                         candidate_index=0, case_index=0, cursor=0, section_count=0)
            job = await self._staged_save(job, root, state, status="preparing", ov_task_id="")
            return await self._staged_candidate_next(job, root, state)
        state["phase"] = phase
        return await self._submit_artifact_batch(job, root, state, phase, packets, contexts, compiler_key=key)

    async def _submit_artifact_batch(self, job, root, state, phase, packets, contexts, *, compiler_key=None):
        _, _, json_text, _, package, _, _, save, sha, write = _host()
        key = compiler_key or phase + "_batch"
        body = await asyncio.to_thread((root / "aux-compilers" / (key + ".md")).read_text, encoding="utf-8")
        if sha(body) != state["aux_compilers"][key]:
            raise ValueError("Frozen artifact compiler changed")
        name, published = package(body)
        texts = [json_text(packet) for packet in packets]
        digest = sha(
            json_text(
                [
                    ARTIFACT_BATCH_PROTOCOL,
                    state["round"],
                    phase,
                    contexts,
                    [sha(text) for text in texts],
                    sha(published),
                ]
            )
        )[:24]
        child = ARTIFACT_BATCH_STAGE + "-" + digest
        folder = "children/" + child
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{state['round']}/stages/{child}/input"
        output_uri = input_uri.rsplit("/", 1)[0] + "/output"
        rows = []
        for index, (packet, context, text) in enumerate(zip(packets, contexts, texts, strict=True)):
            filename = f"packet-{index:02d}.json"
            await asyncio.to_thread(write, root / folder / filename, text)
            rows.append(
                {
                    "uri": input_uri + "/" + filename,
                    "local_path": folder + "/" + filename,
                    "sha256": sha(text),
                    "output_path": packet["output_path"],
                    "context": context,
                }
            )
        request = {
            "from": [input_uri],
            "to": output_uri,
            "skill": "viking://agent/skills/" + name,
            "instruction": _instruction(state["language_policy"]),
        }
        await asyncio.to_thread(save, root / folder / "packets.json", rows)
        await asyncio.to_thread(save, root / folder / "request.json", request)
        await asyncio.to_thread(write, root / folder / "compiler.md", published)
        state["current"] = {
            "stage": ARTIFACT_BATCH_STAGE,
            "artifact_stage": phase,
            "skill_body_protocol": state.get("skill_body_protocol", ""),
            "input_protocol": ARTIFACT_BATCH_PROTOCOL,
            "child": child,
            "key": f"te-staged-{job['job_id']}-r{state['round']}-{digest}",
            "task_id": "",
            "input_uri": input_uri,
            "output_uri": output_uri,
            "context": {},
            "request_file": folder + "/request.json",
            "packet_file": folder + "/packets.json",
            "compiler_file": folder + "/compiler.md",
            "packet_sha256": sha(json_text(rows) + "\n"),
            "request_sha256": sha(json_text(request)),
            "compiler_sha256": sha(published),
            "compiler_source_sha256": sha(body),
            "attempt": 0,
        }
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
        return await self._resume_artifact_batch(job, root, state)

    async def _resume_artifact_batch(self, job, root, state):
        _, _, _, _, _, _, _, save, _, _ = _host()
        current, repository = state["current"], self._ov_skill_mining_repository()
        started = time.perf_counter()
        try:
            request, body, rows, texts = await asyncio.to_thread(_verify, root, current)
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
                    repository, job,
                    ValueError("Artifact batch acknowledgement had no task identity; recover the exact key"),
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

    async def _advance_artifact_batch(self, job, root, state, task):
        _, _, _, _, _, _, read, save, _, write = _host()
        current = state["current"]
        repository = self._ov_skill_mining_repository()
        try:
            _, _, rows, _ = await asyncio.to_thread(_verify, root, current)
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
            for row in rows:
                context, name = row["context"], row["context"]["skill_name"]
                ledger = await asyncio.to_thread(_ledger, root, name)
                audit_path = (
                    root / "artifact-batches/outcomes" / (current["child"] + "-" + row["output_path"] + ".json")
                )
                if audit_path.exists():
                    outcome = await asyncio.to_thread(read, audit_path)
                    if outcome["context"] != context:
                        raise ValueError("Durable artifact outcome changed its exact assignment")
                else:
                    content, value, error = "", None, ""
                    try:
                        uri = current["output_uri"] + "/" + row["output_path"]
                        if uri not in files:
                            raise ValueError("Independent artifact output is missing")
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
                        if isinstance(content, str):
                            await asyncio.to_thread(
                                write, root / "children" / current["child"] / (row["output_path"] + ".raw"), content
                            )
                        if (
                            not isinstance(content, str)
                            or not content.strip()
                            or len(content) > planning.MAX_OUTPUT_CHARS
                        ):
                            raise ValueError("Independent artifact must be complete and within 12K characters")
                        if (context["stage"] == "skill_section"
                                and current.get("skill_body_protocol") == BODY_PATCH_PROTOCOL):
                            from .ov_body_patch_orchestration import verify_body_assignment
                            from .skill_body_patch import validate_body_patch

                            packet = await asyncio.to_thread(read, root / row["local_path"])
                            manifest = await asyncio.to_thread(verify_body_assignment, root, context, packet)
                            value = validate_body_patch(content, manifest)
                        elif context.get("body_insert_protocol"):
                            from .skill_body_insert import project, validate

                            packet = await asyncio.to_thread(read, root / row["local_path"])
                            prior = packet["derived_controls"]["previous_body"]
                            patch = validate(content, prior, context["input_ids"], state["language_policy"])
                            value = (patch if patch["schema_version"].endswith(".capacity.v1") else
                                     project(patch, prior, context["input_ids"], state["language_policy"]))
                        elif context["stage"] == "skill_section":
                            value = json.loads(content)
                            if planning.is_deferred(value):
                                if current.get("skill_body_protocol") == SKILL_BODY_PROTOCOL:
                                    raise ValueError("An Evidence gap cannot be accepted as a no-change Skill revision")
                                value = planning.validate_deferred(value, source_refs=context["source_refs"])
                                value = {
                                    "schema_version": planning.SECTION_SKIP_SCHEMA,
                                    "input_ids": context["input_ids"],
                                    "reason": value["reason"],
                                }
                            validator = (
                                planning.validate_section_skip
                                if value.get("schema_version") == planning.SECTION_SKIP_SCHEMA
                                else planning.validate_section
                            )
                            value = validator(value, input_ids=context["input_ids"])
                            if value.get("schema_version") == planning.SECTION_SCHEMA:
                                validate_artifact_headings(
                                    value["content"], state["language_policy"], title=value["title"]
                                )
                        else:
                            validate_artifact_headings(
                                content, state["language_policy"], machine_name=context["skill_name"]
                            )
                            value = content
                    except (ValueError, KeyError, TypeError) as exc:
                        error = str(exc)[:1000]
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        error = "Completed independent artifact disappeared"
                    outcome = {"context": context, "valid": not error, "value": value, "error": error,
                               "raw_chars": len(content) if isinstance(content, str) else 0}
                    await asyncio.to_thread(save, audit_path, outcome)
                if outcome["valid"]:
                    if context["stage"] == "skill_section":
                        if ledger["cursor"] not in {context["before"], context["after"]}:
                            raise ValueError("Artifact section cursor changed")
                        if context.get("body_insert_protocol"):
                            from .skill_body_insert import CAPACITY_SCHEMA

                            if outcome["value"]["schema_version"] == CAPACITY_SCHEMA:
                                if len(context["input_ids"]) <= 1:
                                    raise ValueError("单个完整原文分片的新增正文仍超容量；未丢弃来源或旧条件")
                                ledger.update(body_max_chunks=max(1, len(context["input_ids"]) // 2),
                                              error="", format_attempts=0)
                                await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"),
                                                        ledger)
                                continue
                        if current.get("skill_body_protocol") == BODY_PATCH_PROTOCOL:
                            from .ov_body_patch_orchestration import accept_body_assignment
                            from .skill_body_patch import CAPACITY_SCHEMA

                            if outcome["value"]["schema_version"] == CAPACITY_SCHEMA:
                                if len(context["input_ids"]) <= 1:
                                    raise ValueError("单个完整原文单元的正文增量输出仍超容量，未丢弃来源或旧条件。")
                                ledger.update(body_max_units=max(1, len(context["input_ids"]) // 2),
                                              error="", format_attempts=0)
                                await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"),
                                                        ledger)
                                continue
                            packet = await asyncio.to_thread(read, root / row["local_path"])
                            await asyncio.to_thread(accept_body_assignment, root, context, outcome["value"], packet)
                        await asyncio.to_thread(
                            save, root / "sections" / name / f"{context['section_index']:08d}.json", outcome["value"]
                        )
                        await asyncio.to_thread(
                            save,
                            root / "section-groups" / name / f"{context['section_index']:08d}.json",
                            {"ordinals": context["ordinals"]},
                        )
                        ledger.update(cursor=context["after"], section_count=context["section_index"] + 1)
                    else:
                        await asyncio.to_thread(
                            repository.write_output,
                            job["job_id"],
                            f"compiled_skill/{name}/EVALUATION.md",
                            outcome["value"],
                        )
                        ledger["evaluation_ready"] = True
                    ledger.update(format_attempts=0, error="")
                else:
                    if (context.get("body_insert_protocol") and len(context["input_ids"]) > 1
                            and outcome.get("raw_chars", 0) > planning.MAX_OUTPUT_CHARS):
                        # An oversized delta changes the next assignment's source grouping,
                        # rather than spending three format repairs on the same oversized work.
                        ledger.update(body_max_chunks=max(1, len(context["input_ids"]) // 2),
                                      capacity_split_count=ledger.get("capacity_split_count", 0) + 1,
                                      error="", format_attempts=0)
                        await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"),
                                                ledger)
                        continue
                    ledger.update(format_attempts=context["format_attempts"] + 1, error=outcome["error"])
                    if ledger["format_attempts"] >= MAX_ARTIFACT_ATTEMPTS:
                        exhausted.append(name)
                await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"), ledger)
            if foreign:
                raise ValueError("Artifact batch returned unexpected output; valid independent siblings are retained")
            if exhausted:
                return await self._staged_save(
                    job,
                    root,
                    state,
                    status="failed",
                    finished_at=utc_now(),
                    error="Artifact repair limit reached: " + ", ".join(exhausted),
                )
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
        state["completed_tasks"] += 1
        state["current"] = None
        job = await self._staged_save(job, root, state, status="preparing", ov_task_id="", error="", finished_at="")
        return await self._staged_artifact_batch_next(job, root, state)
