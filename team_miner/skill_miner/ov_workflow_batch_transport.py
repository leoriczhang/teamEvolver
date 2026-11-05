"""Frozen independent workflow Maps over the unchanged public Compile API.

The host owns workflow meaning, capacity splitting and final metadata. Its
accept callback must be idempotent for a frozen slot: a process may stop after
the callback commits but before this transport records its acceptance receipt.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import time
from pathlib import Path

import httpx
from starlette.exceptions import HTTPException

from .compile_strategy import MAX_FILE_MAP_PACKETS
from .direct_workflow_planning import FrozenPlanningIntegrityError
from .output_language import output_language_instruction
from .ov_case_batch_orchestration import _host
from .ov_compile_mining import RUNS_ROOT, utc_now
from .ov_extract_batch_orchestration import _missing_output_error

WORKFLOW_BATCH_PROTOCOL = "te-workflow-batch.v1"
WORKFLOW_BATCH_STAGE = "workflow_batch"
WORKFLOW_STAGES = {"workflow_partition", "workflow_metadata"}
MAX_WORKFLOW_PACKETS = MAX_FILE_MAP_PACKETS
MAX_WORKFLOW_ATTEMPTS = 3
MAX_PACKET_CHARS = 40000
MAX_ENVELOPE_CHARS = 48000
REPAIR_RESERVE_CHARS = 3072
MAX_OUTPUT_CHARS = 12000
_OUTPUT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}\.json\Z")
_REJECTED = {400, 401, 403, 409, 413, 422}


def _path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise FrozenPlanningIntegrityError("Workflow frozen path must be relative")
    path = root / relative
    if ".." in Path(relative).parts or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise FrozenPlanningIntegrityError("Workflow frozen path escaped its local round")
    return path


def _instruction(policy: dict) -> str:
    return (
        "Map each input file independently as the workflow stage declared by its packet. "
        "Read its complete assigned controls, emit exactly packet.output_path and finalize those files. "
        "Never combine sibling packets, invent resource paths or generate another stage. "
        + output_language_instruction(policy)
    )


def _assignment(ledger: dict) -> dict:
    if (not isinstance(ledger, dict) or ledger.get("protocol") != WORKFLOW_BATCH_PROTOCOL
            or ledger.get("stage") not in WORKFLOW_STAGES or not isinstance(ledger.get("slots"), list)
            or not 1 <= len(ledger["slots"]) <= MAX_WORKFLOW_PACKETS):
        raise FrozenPlanningIntegrityError("Workflow ledger requires one to eight assigned stage packets")
    slots, outputs = [], set()
    for slot in ledger["slots"]:
        if (not isinstance(slot, dict) or not isinstance(slot.get("packet"), dict)
                or not isinstance(slot.get("context"), dict) or slot.get("status") not in {"pending", "valid"}
                or type(slot.get("format_attempts")) is not int
                or not 0 <= slot["format_attempts"] <= MAX_WORKFLOW_ATTEMPTS
                or not isinstance(slot.get("error"), str)):
            raise FrozenPlanningIntegrityError("Workflow slot identity or finite repair state changed")
        packet = slot["packet"]
        output = packet.get("output_path")
        if (packet.get("stage") != ledger["stage"] or not isinstance(packet.get("derived_controls"), dict)
                or not isinstance(output, str) or not _OUTPUT.fullmatch(output) or output in outputs):
            raise FrozenPlanningIntegrityError("Workflow packet changed its stage or unique output path")
        outputs.add(output)
        slots.append({"packet": packet, "context": slot["context"]})
    # Queue boundaries and every other top-level routing field are immutable.
    # Only each slot's status, diagnostic and acceptance receipt may advance.
    return {**copy.deepcopy({key: value for key, value in ledger.items() if key != "slots"}), "slots": slots}


def _repair(error: str) -> str:
    """Bound the serialized diagnostic only; complete diagnostics remain in the ledger."""
    _, _, json_text, *_ = _host()
    low, high = 0, len(error)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json_text(error[:middle])) <= 2000:
            low = middle
        else:
            high = middle - 1
    return error[:low]


def _packet(slot: dict) -> dict:
    packet = copy.deepcopy(slot["packet"])
    if slot["error"]:
        packet["derived_controls"]["repair_error"] = _repair(slot["error"])
        packet["derived_controls"]["repair_error_excerpted"] = (
            packet["derived_controls"]["repair_error"] != slot["error"]
        )
    return packet


def _verified(root: Path, current: dict):
    _, _, json_text, _, _, _, read, _, sha, _ = _host()
    try:
        if (current.get("stage") != WORKFLOW_BATCH_STAGE
                or current.get("input_protocol") != WORKFLOW_BATCH_PROTOCOL):
            raise FrozenPlanningIntegrityError("Workflow current task has a different frozen protocol")
        request = read(_path(root, current["request_file"]))
        body = _path(root, current["compiler_file"]).read_text(encoding="utf-8")
        text = _path(root, current["packet_file"]).read_text(encoding="utf-8")
        rows = json.loads(text)
        ledger = read(_path(root, current["ledger_file"]))
        assignment = read(_path(root, current["assignment_file"]))
        if (sha(json_text(request)) != current["request_sha256"] or sha(body) != current["compiler_sha256"]
                or sha(text) != current["packet_sha256"]
                or sha(json_text(assignment)) != current["assignment_sha256"]
                or _assignment(ledger) != assignment
                or set(request) != {"from", "to", "skill", "instruction"}
                or request["from"] != [current["input_uri"]] or request["to"] != current["output_uri"]
                or ledger["stage"] != current["context"]["workflow_stage"]
                or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_WORKFLOW_PACKETS):
            raise FrozenPlanningIntegrityError("Frozen workflow request, compiler or assignment SHA changed")
        # Repair batches omit valid siblings from Native input, but their local
        # receipts must remain verified before recovery or final commit.
        for slot_index, slot in enumerate(ledger["slots"]):
            if slot["status"] != "valid":
                continue
            outcome = read(_path(root, slot["outcome_file"]))
            raw = _path(root, outcome["raw_file"]).read_text(encoding="utf-8")
            if (not outcome.get("valid") or outcome["slot_index"] != slot_index
                    or outcome["context"] != slot["context"] or sha(raw) != outcome["raw_sha256"]
                    or sha(json_text(outcome)) != slot["outcome_sha256"]
                    or slot.get("acceptance") != outcome["acceptance"]):
                raise FrozenPlanningIntegrityError("Accepted workflow sibling raw or acceptance receipt changed")
        seen, indexes, texts = set(), set(), []
        for index, row in enumerate(rows):
            slot_index = row["slot_index"]
            if type(slot_index) is not int or not 0 <= slot_index < len(ledger["slots"]) or slot_index in indexes:
                raise FrozenPlanningIntegrityError("Workflow locator changed its assigned slot")
            slot = ledger["slots"][slot_index]
            expected_path = f"children/{current['child']}/packet-{index:02d}.json"
            if row["local_path"] != expected_path:
                raise FrozenPlanningIntegrityError("Workflow packet escaped its exact child path")
            payload = _path(root, expected_path).read_text(encoding="utf-8")
            packet = json.loads(payload)
            if (sha(payload) != row["sha256"] or len(payload) > MAX_PACKET_CHARS
                    or len(body) + len(payload) + len(request["instruction"]) > MAX_ENVELOPE_CHARS
                    or row["uri"] != current["input_uri"] + f"/packet-{index:02d}.json"
                    or row["output_path"] != slot["packet"]["output_path"]
                    or row["output_path"] in seen or row["context"] != slot["context"]
                    or packet != _packet({**slot, "error": row["error"],
                                          "format_attempts": row["format_attempts"]})):
                raise FrozenPlanningIntegrityError("Workflow packet changed its exact frozen slot assignment")
            seen.add(row["output_path"])
            indexes.add(slot_index)
            texts.append(payload)
        return request, body, rows, texts, ledger
    except (KeyError, TypeError, ValueError, OSError) as exc:
        if isinstance(exc, FrozenPlanningIntegrityError):
            raise
        raise FrozenPlanningIntegrityError("Frozen workflow batch is incomplete or damaged: " + str(exc)) from exc


async def _callback(value):
    return await value if inspect.isawaitable(value) else value


class OvWorkflowBatchTransportMixin:
    """Host callbacks own stage validation; this mixin owns finite file transport."""

    async def _validate_workflow_slots(self, root, state, ledger):
        validator = getattr(self, "_workflow_validate_slot", None)
        if validator is not None:
            for slot in ledger["slots"]:
                await _callback(validator(root, state, slot))

    async def _submit_workflow_batch(self, job, root, state, ledger_file, ledger):
        try:
            return await self._submit_checked_workflow_batch(job, root, state, ledger_file, ledger)
        except FrozenPlanningIntegrityError as exc:
            return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(), error=str(exc))

    async def _submit_checked_workflow_batch(self, job, root, state, ledger_file, ledger):
        _, _, json_text, _, package, _, read, save, sha, write = _host()
        repository = self._ov_skill_mining_repository()
        latest = await asyncio.to_thread(repository.get_job, job["job_id"])
        if self._staged_stopped(latest):
            return latest
        assignment = _assignment(ledger)
        await self._validate_workflow_slots(root, state, ledger)
        assignment_file = ledger_file + ".assignment.json"
        path = _path(root, assignment_file)
        if path.exists():
            if await asyncio.to_thread(read, path) != assignment:
                raise FrozenPlanningIntegrityError("Workflow batch cannot change its frozen complete assignment")
        else:
            await asyncio.to_thread(save, path, assignment)
        await asyncio.to_thread(save, _path(root, ledger_file), ledger)
        pending = [(index, slot) for index, slot in enumerate(ledger["slots"]) if slot["status"] == "pending"]
        if not pending:
            return await self._workflow_batch_finished(job, root, state, ledger)
        if any(slot["format_attempts"] >= MAX_WORKFLOW_ATTEMPTS for _, slot in pending):
            return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(),
                                           error="Workflow repair limit reached")
        body = await _callback(self._workflow_compiler(root, state, ledger["stage"]))
        if not isinstance(body, str) or not body.strip():
            raise FrozenPlanningIntegrityError("Workflow compiler callback returned no frozen body")
        compiler_name, published = package(body)
        instruction = _instruction(state["language_policy"])
        packets = []
        for _, slot in pending:
            base_chars = len(json_text(slot["packet"]))
            text = json_text(_packet(slot))
            if (base_chars + REPAIR_RESERVE_CHARS > MAX_PACKET_CHARS
                    or base_chars + len(published) + len(instruction) + REPAIR_RESERVE_CHARS > MAX_ENVELOPE_CHARS
                    or len(text) > MAX_PACKET_CHARS
                    or len(text) + len(published) + len(instruction) > MAX_ENVELOPE_CHARS):
                raise ValueError("Complete workflow packet and compiler exceed the bounded envelope; host must split")
            packets.append(text)
        digest = sha(json_text([WORKFLOW_BATCH_PROTOCOL, state["round"], assignment,
                               [(index, slot["format_attempts"]) for index, slot in pending],
                               [sha(text) for text in packets], sha(published)]))[:24]
        child = "workflow-batch-" + digest
        folder = "children/" + child
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{state['round']}/stages/{child}/input"
        output_uri, locators = input_uri.rsplit("/", 1)[0] + "/output", []
        for index, ((slot_index, slot), text) in enumerate(zip(pending, packets, strict=True)):
            filename = f"packet-{index:02d}.json"
            await asyncio.to_thread(write, _path(root, folder + "/" + filename), text)
            locators.append({"uri": input_uri + "/" + filename, "local_path": folder + "/" + filename,
                             "sha256": sha(text), "output_path": slot["packet"]["output_path"],
                             "slot_index": slot_index, "context": copy.deepcopy(slot["context"]),
                             "format_attempts": slot["format_attempts"], "error": slot["error"]})
        request = {"from": [input_uri], "to": output_uri,
                   "skill": "viking://agent/skills/" + compiler_name, "instruction": instruction}
        await asyncio.to_thread(save, _path(root, folder + "/packets.json"), locators)
        await asyncio.to_thread(save, _path(root, folder + "/request.json"), request)
        await asyncio.to_thread(write, _path(root, folder + "/compiler.md"), published)
        state["current"] = {"stage": WORKFLOW_BATCH_STAGE, "input_protocol": WORKFLOW_BATCH_PROTOCOL,
                            "context": {"workflow_stage": ledger["stage"]}, "child": child,
                            "ledger_file": ledger_file, "assignment_file": assignment_file,
                            "assignment_sha256": sha(json_text(assignment)), "task_id": "",
                            "key": f"te-staged-{job['job_id']}-r{state['round']}-{digest}",
                            "input_uri": input_uri, "output_uri": output_uri,
                            "request_file": folder + "/request.json", "packet_file": folder + "/packets.json",
                            "compiler_file": folder + "/compiler.md", "packet_sha256": sha(json_text(locators) + "\n"),
                            "request_sha256": sha(json_text(request)), "compiler_sha256": sha(published),
                            "compiler_source_sha256": sha(body), "attempt": 0}
        state["attempted_tasks"] = int(state.get("attempted_tasks", 0)) + 1
        await asyncio.to_thread(save, _path(root, folder + "/metadata.json"), state["current"])
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="",
                                      compile_request=request, submission_idempotency_key=state["current"]["key"])
        return await self._resume_workflow_batch(job, root, state)

    async def _resume_workflow_batch(self, job, root, state):
        *_, save, _, _ = _host()
        current, repository = state["current"], self._ov_skill_mining_repository()
        started = time.perf_counter()
        try:
            request, body, rows, texts, ledger = await asyncio.to_thread(_verified, root, current)
            await self._validate_workflow_slots(root, state, ledger)
            accepted = {"task_id": current["task_id"]} if current.get("task_id") else None
            if accepted is None:
                try:
                    accepted = await self._knowledge_request(
                        job["account"], "GET", f"/api/v1/compile/submissions/{current['key']}", timeout=20.0,
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
                await asyncio.to_thread(save, _path(root, "state.json"), state)
                accepted = await self._knowledge_request(job["account"], "POST", "/api/v1/compile", timeout=60.0,
                                                        extra_headers={"Idempotency-Key": current["key"]}, json=request)
            if not isinstance(accepted, dict) or not accepted.get("task_id"):
                return await self._round_submission_retry(repository, job,
                                                          ValueError("Workflow acceptance missing task identity"))
            current["task_id"] = str(accepted["task_id"])
            await self._mark_staged_compile_slot(job, current)
            await asyncio.to_thread(save, _path(root, f"children/{current['child']}/metadata.json"), current)
            job = await self._staged_save(job, root, state, compile_request=request,
                                          submission_idempotency_key=current["key"], compiler_uri=request["skill"],
                                          skill_uri=request["skill"], compiler_sha256=current["compiler_sha256"],
                                          compiler_source_sha256=current["compiler_source_sha256"])
            return await self._record_round_acceptance(repository, job, accepted, started)
        except FrozenPlanningIntegrityError as exc:
            return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(), error=str(exc))
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in _REJECTED:
                recovered = await self._staged_release_rejected(job, root, state, exc)
                if recovered is not None:
                    return recovered
                raise
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            await self._staged_release_rejected(job, root, state, exc)
            raise

    async def _advance_workflow_batch(self, job, root, state, task):
        _, _, json_text, _, _, _, read, save, sha, write = _host()
        current, repository = state["current"], self._ov_skill_mining_repository()
        try:
            _, _, rows, texts, ledger = await asyncio.to_thread(_verified, root, current)
            await self._validate_workflow_slots(root, state, ledger)
            await self._verify_case_batch_input_directory(job, current, rows)
            try:
                tree = await self._knowledge_tree(job["account"], current["output_uri"])
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                tree = {"entries": []}
            files = [row["uri"] for row in tree.get("entries") or [] if not row.get("is_dir")
                     and not row["uri"].rsplit("/", 1)[-1].startswith(".")]
            expected = {current["output_uri"] + "/" + row["output_path"] for row in rows}
            foreign = bool(set(files) - expected or len(files) != len(set(files)))
            exhausted = []
            for row in rows:
                slot = ledger["slots"][row["slot_index"]]
                outcome_file = f"children/{current['child']}/{row['output_path']}.outcome.json"
                raw_file = f"children/{current['child']}/{row['output_path']}.raw"
                if slot["status"] == "valid":
                    outcome = await asyncio.to_thread(read, _path(root, slot["outcome_file"]))
                    raw = await asyncio.to_thread(_path(root, outcome["raw_file"]).read_text, encoding="utf-8")
                    if (sha(raw) != outcome["raw_sha256"] or sha(json_text(outcome)) != slot["outcome_sha256"]
                            or outcome["slot_index"] != row["slot_index"] or outcome["context"] != slot["context"]
                            or slot.get("acceptance") != outcome["acceptance"]):
                        raise FrozenPlanningIntegrityError("Accepted workflow outcome or original raw changed")
                    continue
                if _path(root, outcome_file).exists():
                    outcome = await asyncio.to_thread(read, _path(root, outcome_file))
                    if (outcome["slot_index"] != row["slot_index"] or outcome["context"] != row["context"]
                            or outcome["packet_sha256"] != row["sha256"]):
                        raise FrozenPlanningIntegrityError("Workflow durable outcome changed its exact assignment")
                    if outcome["valid"]:
                        raw = await asyncio.to_thread(_path(root, outcome["raw_file"]).read_text, encoding="utf-8")
                        if sha(raw) != outcome["raw_sha256"]:
                            raise FrozenPlanningIntegrityError("Workflow durable raw output changed")
                else:
                    raw, error, receipt = "", "", None
                    try:
                        uri = current["output_uri"] + "/" + row["output_path"]
                        if uri not in files:
                            raise ValueError(_missing_output_error(task, stage=ledger["stage"]))
                        response = await self._knowledge_request(job["account"], "GET", "/api/v1/content/read",
                                                                params={"uri": uri, "offset": 0, "limit": -1,
                                                                        "raw": "true"}, timeout=20.0)
                        raw = (response if isinstance(response, str)
                               else response.get("content", response.get("text", ""))
                               if isinstance(response, dict) else None)
                        if isinstance(raw, str):
                            await asyncio.to_thread(write, _path(root, raw_file), raw)
                        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_OUTPUT_CHARS:
                            raise ValueError("Workflow output is empty or exceeds its 12K raw budget")
                        receipt = await self._workflow_accept_slot(job, root, state, slot, json.loads(raw))
                        # Receipts must be finite JSON, independent of the model output.
                        try:
                            json_text(receipt)
                        except (ValueError, TypeError) as exc:
                            raise FrozenPlanningIntegrityError("Workflow host returned a non-finite receipt") from exc
                    except FrozenPlanningIntegrityError:
                        raise
                    except (ValueError, KeyError, TypeError) as exc:
                        error = str(exc)
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        error = "Completed independent workflow output disappeared"
                    outcome = {"slot_index": row["slot_index"], "context": row["context"],
                               "packet_sha256": row["sha256"], "valid": not error, "error": error,
                               "raw_file": raw_file, "raw_sha256": sha(raw) if isinstance(raw, str) else sha(""),
                               "acceptance": receipt}
                    await asyncio.to_thread(save, _path(root, outcome_file), outcome)
                if outcome["valid"]:
                    slot.update(status="valid", error="", outcome_file=outcome_file,
                                outcome_sha256=sha(json_text(outcome)), acceptance=outcome["acceptance"])
                else:
                    slot.update(format_attempts=row["format_attempts"] + 1, error=outcome["error"])
                    if slot["format_attempts"] >= MAX_WORKFLOW_ATTEMPTS:
                        exhausted.append(row["slot_index"])
                await asyncio.to_thread(save, _path(root, current["ledger_file"]), ledger)
            if foreign:
                raise FrozenPlanningIntegrityError(
                    "Workflow batch returned unexpected output paths; accepted siblings retained"
                )
            if exhausted:
                return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(),
                                               error="Workflow repair limit reached: " + ", ".join(map(str, exhausted)))
        except FrozenPlanningIntegrityError as exc:
            return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(), error=str(exc))
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in _REJECTED:
                return await self._staged_save(job, root, state, status="failed", finished_at=utc_now(), error=str(exc))
            return await self._round_submission_retry(repository, job, exc)
        state["completed_tasks"] = int(state.get("completed_tasks", 0)) + 1
        state["current"] = None
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._submit_workflow_batch(job, root, state, current["ledger_file"], ledger)
