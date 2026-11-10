"""Independent Compile units with durable intent, reconciliation and finite compensation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from starlette.exceptions import HTTPException

from ..compile_admission import TERMINAL_NATIVE_STATES
from ..mining_contracts import RUNS_ROOT, utc_now
from ..native_compiler import frozen_assets
from ..ov_round_orchestration import _compiler_package
from . import PROTOCOL
from .assembly import assemble
from .compiler import instruction
from .input import canonical, case_key, parse_model, sha
from .partition import fits_window, window_text
from .state import Journal
from .transport import artifact, descendant, read, write
from .validation import ReceiptError, findings, merge


async def update(engine, job, **fields):
    return await asyncio.to_thread(engine.repository.update_job, job["job_id"], **fields)


async def frozen_read(engine, job, uri):
    try:
        return await read(engine, job, uri)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ValueError("frozen_dialogue_input_missing") from exc
        raise


async def prepare(engine, job, journal):
    """Verify each manifest page and each model file before recording a bounded unit."""
    raw = await frozen_read(engine, job, job["dataset_manifest_ref"])
    if sha(raw) != job["dataset_manifest_sha256"]:
        raise ValueError("dataset_manifest_changed")
    manifest = json.loads(raw)
    expected = manifest.pop("dataset_sha256", "")
    if expected != sha(canonical(manifest)) or manifest.get("schema_version") != "taikang.dialogue-dataset.v1":
        raise ValueError("invalid_dataset_manifest")
    root = job["dataset_manifest_ref"].rsplit("/", 1)[0]
    if job["input_dir"].rstrip("/") != root + "/model_cases":
        raise ValueError("model_source_must_be_manifest_model_cases")
    # Stream pages and cases. Only the current batch is retained in memory.
    pending, batch, chars, size, number, case_count = 0, [], 0, 0, 0, 0
    limits = job["dialogue_options"]

    async def flush():
        nonlocal batch, chars, size, number
        if batch:
            number += 1
            await journal.save(**{f"extract/{number}": {"number": number, "stage": "extract", "files": batch,
                                                      "attempt": 0, "status": "pending", "accepted": {}}})
        batch, chars, size = [], 0, 0

    for page in manifest["pages"]:
        entry = page["manifest"]
        text = await frozen_read(engine, job, descendant(root, entry["path"]))
        if sha(text) != entry["sha256"]:
            raise ValueError("dataset_page_changed")
        for line in text.splitlines():
            item = json.loads(line)
            uri = descendant(root, item["path"])
            if not uri.startswith(job["input_dir"] + "/"):
                raise ValueError("case_outside_model_source")
            content = await frozen_read(engine, job, uri)
            if sha(content) != item["sha256"]:
                raise ValueError("frozen_case_changed")
            case = parse_model(content)
            if any(case[key] != item[key] for key in ("case_ref", "case_version")):
                raise ValueError("manifest_case_identity_changed")
            case_count += 1
            identity_key = "planned/" + sha(canonical([case["case_ref"], case["case_version"]]))
            prior = await journal.get(identity_key)
            identity = {"ordinal": case_count, "sha256": item["sha256"]}
            if prior is not None and prior != identity:
                raise ValueError("duplicate_or_reordered_frozen_case")
            await journal.save(**{identity_key: identity})
            length, encoded = len(content), len(content.encode("utf-8"))
            if (len(case["dialogue_text"]) > limits["max_case_chars"] or length > limits["extract_input_chars"]
                    or encoded > limits["extract_input_bytes"]):
                pending += 1
                await artifact(engine, job, f"coverage/capacity-{pending:06d}.jsonl", canonical({
                    "case_ref": case["case_ref"], "case_version": case["case_version"],
                    "status": "pending", "reason": "capacity_pending"}) + "\n")
                continue
            if batch and (len(batch) >= limits["max_cases_per_compile"]
                          or chars + length > limits["extract_input_chars"]
                          or size + encoded > limits["extract_input_bytes"]):
                await flush()
            batch.append({"uri": uri, "sha256": item["sha256"], "case": case})
            chars += length
            size += encoded
    await flush()
    if case_count != manifest["case_count"]:
        raise ValueError("dataset_case_count_mismatch")
    # Invalid source rows remain pending; they cannot silently disappear from delivery.
    input_pending = 0
    for page in manifest["pages"]:
        entry = page["issues"]
        text = await frozen_read(engine, job, descendant(root, entry["path"]))
        if sha(text) != entry["sha256"]:
            raise ValueError("dataset_issue_page_changed")
        rows = [json.loads(line) for line in text.splitlines()]
        input_pending += sum(row.get("status") == "input_pending" for row in rows)
        if rows:
            await artifact(engine, job, "issues/input-" + entry["path"].rsplit("/", 1)[-1], text)
    state = {"stage": "extract", "cursor": 1, "extract_count": number, "merge_count": 0, "case_count": case_count,
             "processed": 0, "pending": pending + input_pending, "usable": 0, "isolated": 0, "warnings": 0,
             "dimensions": 0, "blocked_units": 0, "merge_planning_cursor": 1, "merge_buffer": None,
             "page_count": 0, "projection_count": 0}
    await journal.save(summary=state)
    return state


async def provision(engine, job):
    body = job["compiler_body_snapshot"]
    if PROTOCOL not in body:
        raise ValueError("dialogue_compiler_protocol_changed")
    assets = await asyncio.to_thread(frozen_assets, engine.repository.jobs_root / job["job_id"], body)
    name, published, _ = _compiler_package(body)
    await engine._round_provision_compiler(engine._round_client(job), name, published, assets)
    return "viking://agent/skills/" + name


async def submit(engine, job, journal, unit):
    """An uncertain POST is always reconciled with the persisted original key."""
    limits = job["dialogue_options"]
    if not unit.get("request"):
        for item in unit["files"]:
            if sha(await frozen_read(engine, job, item["uri"])) != item["sha256"]:
                raise ValueError("frozen_unit_input_changed")
        skill = await provision(engine, job)
        attempt = unit["attempt"]
        key = "te-dd-" + sha(canonical([engine.tenant, job["job_id"], job["current_round"], unit["stage"],
                                     unit["number"], [f["sha256"] for f in unit["files"]],
                                     sha(job["compiler_body_snapshot"]),
                                     job.get("dialogue_recovery_epoch", 0), attempt]))
        epoch = job.get("dialogue_recovery_epoch", 0)
        target = (f"{RUNS_ROOT}/{job['job_id']}/dialogue/recovery-{epoch}/units/"
                  f"{unit['stage']}-{unit['number']}/attempt-{attempt}")
        selected = [f for f in unit["files"] if not f.get("case") or case_key(f["case"]) not in unit["accepted"]]
        unit.update(key=key, submitted_at=utc_now(), request={
            "from": [f["uri"] for f in selected], "to": target, "skill": skill,
            "instruction": instruction(unit["stage"], limits["quote_mode"])}, status="submitting")
        await journal.save(**{f"{unit['stage']}/{unit['number']}": unit})
    if not await engine.admission.acquire(job, {"key": unit["key"]}):
        return unit
    await update(engine, job, compile_request=unit["request"], status="preparing", ov_task_id="",
                 round_started_at=unit["submitted_at"], submission_idempotency_key=unit["key"],
                 compiler_uri=unit["request"]["skill"], output_uri=unit["request"]["to"])
    try:
        accepted = await engine._knowledge_request(
            job["account"], "GET", "/api/v1/compile/submissions/" + unit["key"], timeout=10)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        if job.get("ov_cancel_reason"):
            # A POST may still be in flight. Reuse its key to settle acceptance, then cancel;
            # never release an unknown reservation based solely on an absent lookup.
            pass
        try:
            accepted = await engine._knowledge_request(
                job["account"], "POST", "/api/v1/compile", timeout=60,
                extra_headers={"Idempotency-Key": unit["key"]}, json=unit["request"])
        except HTTPException as rejection:
            if rejection.status_code in {400, 401, 403, 413, 422}:
                await engine.admission.release(key=unit["key"])
                unit.update(status="rejected", error="compile_request_rejected", definitive_failure=True)
                await journal.save(**{f"{unit['stage']}/{unit['number']}": unit})
                return unit
            raise
    task_id = str(accepted.get("task_id") or "")
    if not task_id:
        raise ValueError("compile_acceptance_missing_task_id")
    unit.update(task_id=task_id, status="running")
    await journal.save(**{f"{unit['stage']}/{unit['number']}": unit})
    await engine.admission.mark({"key": unit["key"], "task_id": task_id})
    await update(engine, job, ov_task_id=task_id, status="stopping" if job.get("ov_cancel_reason") else "running",
                 started_at=job.get("started_at") or utc_now())
    return unit


async def collect(engine, job, journal, unit):
    """Keep valid case receipts when another child failed or returned malformed output."""
    target = unit["request"]["to"]
    for item in unit["files"]:
        if item.get("uri") and sha(await frozen_read(engine, job, item["uri"])) != item["sha256"]:
            raise ValueError("frozen_unit_input_changed_before_acceptance")
    errors = []
    if unit["stage"] == "extract":
        for item in unit["files"]:
            case = item["case"]
            if case_key(case) in unit["accepted"]:
                continue
            path = f"analysis/dialogue_dimensions/receipts/{case['case_ref']}-{case['case_version']}.json"
            try:
                text = await read(engine, job, descendant(target, path))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                errors.append({"case_ref": case["case_ref"], "reason": "missing_receipt"})
                continue
            await artifact(engine, job,
                           f"raw/extract-{unit['number']}/attempt-{unit['attempt']}/{case['case_ref']}.json", text)
            try:
                accepted = findings(text, case)
            except ReceiptError as exc:
                errors.append({"case_ref": case["case_ref"], "reason": str(exc)})
                continue
            accepted["raw_receipt_uri"] = descendant(target, path)
            unit["accepted"][case_key(case)] = accepted
            # Journal each accepted child separately before moving to the next child.
            await journal.save(**{f"extract/{unit['number']}": unit})
        done = len(unit["accepted"]) == len(unit["files"])
    else:
        window = unit["window"]
        try:
            text = await read(engine, job, descendant(target, window["output_path"]))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            errors.append({"reason": "missing_merge_receipt"})
            done = False
        else:
            await artifact(engine, job, f"raw/merge-{unit['number']}/attempt-{unit['attempt']}.json", text)
            try:
                unit["receipt"] = merge(text, window)
                done = True
            except ReceiptError as exc:
                errors.append({"reason": str(exc)})
                done = False
    unit.update(errors=errors, status="accepted" if done else "invalid")
    await journal.save(**{f"{unit['stage']}/{unit['number']}": unit})
    return unit


async def project(engine, job, unit):
    """One bounded leaf per unit; no aggregate 128-file/16-MiB task ceiling."""
    entries = []

    async def emit(path, value, jsonl=False):
        # Keep complete records and paginate their containers; never trim business content.
        pages, page, length = [], [], 0
        for row in value:
            size = len(canonical(row).encode("utf-8")) + 1
            if page and length + size > 1024 * 1024:
                pages.append(page)
                page, length = [], 0
            page.append(row)
            length += size
        pages.append(page)
        for number, rows in enumerate(pages, 1):
            leaf = path if number == 1 else path.rsplit(".", 1)[0] + f"-page{number:06d}." + path.rsplit(".", 1)[1]
            text = "".join(canonical(v) + "\n" for v in rows) if jsonl else canonical(rows) + "\n"
            await artifact(engine, job, leaf, text)
            entries.append({"path": leaf, "sha256": sha(text), "bytes": len(text.encode("utf-8"))})

    if unit["stage"] == "extract":
        accepted = list(unit["accepted"].values())
        coverage = [{"case_ref": item["case"]["case_ref"], "case_version": item["case"]["case_version"],
                     "status": unit["accepted"].get(case_key(item["case"]), {}).get("analysis_status", "pending"),
                     "receipt_ref": unit["accepted"].get(case_key(item["case"]), {}).get("raw_receipt_uri", ""),
                     "reason": "" if case_key(item["case"]) in unit["accepted"] else "processing_incomplete"}
                    for item in unit["files"]]
        issues = [{"case_ref": row["case_ref"], **issue} for row in accepted for issue in row["issues"]]
        issues.extend({"case_ref": row["case_ref"], "reason": item["reason"], "submitted": item["submitted"]}
                      for row in accepted for item in row["isolated"])
        issues.extend(unit.get("errors", []))
        issues.extend({"case_ref": row["case_ref"], "reason": "deterministic_repair", "repair": repair}
                      for row in accepted for repair in row["repairs"])
        await emit(f"case_findings/part-{unit['number']:06d}.jsonl", accepted, True)
        await emit(f"coverage/part-{unit['number']:06d}.jsonl", coverage, True)
        await emit(f"issues/extract-{unit['number']:06d}.jsonl", issues, True)
    elif unit.get("receipt"):
        dimensions, links = assemble(unit["window"], unit["receipt"])
        await emit(f"dimensions/part-{unit['number']:06d}.json", dimensions)
        await emit(f"case_dimension_links/{unit['window']['window_id']}.jsonl", links, True)
        await emit(f"issues/merge-{unit['number']:06d}.jsonl",
                   [*unit["receipt"]["deferred"], *unit.get("errors", [])], True)
    else:
        await emit(f"issues/merge-{unit['number']:06d}.jsonl",
                   [{"window_id": unit["window"]["window_id"], "reason": "processing_incomplete",
                     "members": [m["member_ref"] for m in unit["window"]["members"]]}], True)
    return entries


async def advance_unit(engine, job, journal, state, unit):
    if not unit.get("projected"):
        entries = await project(engine, job, unit)
        unit["projected"] = True
        state["page_count"] += 1
        await artifact(engine, job, f"manifest_pages/part-{state['page_count']:06d}.jsonl",
                       "".join(canonical(e) + "\n" for e in entries))
        if unit["stage"] == "extract":
            values = list(unit["accepted"].values())
            state["processed"] += len(values)
            state["pending"] += len(unit["files"]) - len(values)
            state["usable"] += sum(len(v["observations"]) for v in values)
            state["isolated"] += sum(len(v["isolated"]) for v in values)
            state["warnings"] += sum(len(v["issues"]) + len(v["repairs"]) + len(v["isolated"]) for v in values)
        elif unit.get("receipt"):
            dimensions, _ = assemble(unit["window"], unit["receipt"])
            state["dimensions"] += len(dimensions)
            state["warnings"] += len(unit["receipt"]["deferred"]) + len(unit["receipt"]["repairs"])
        else:
            state["blocked_units"] += 1
        state["cursor"] += 1
        await journal.save(summary=state, **{f"{unit['stage']}/{unit['number']}": unit})
    await update(engine, job, ov_task_id="", compile_request=None, status="preparing",
                 ov_cancel_sent_at="", round_started_at="")


async def make_window(engine, job, journal, state, window):
    number = state["merge_count"] + 1
    window["window_id"] = f"window-{number:06d}"
    window["output_path"] = f"analysis/dialogue_dimensions/receipts/{window['window_id']}.json"
    content = window_text(window)
    epoch = job.get("dialogue_recovery_epoch", 0)
    uri = f"{RUNS_ROOT}/{job['job_id']}/dialogue/recovery-{epoch}/windows/{window['window_id']}.md"
    await write(engine, job, uri, content)
    unit = {"number": number, "stage": "merge", "window": window,
            "files": [{"uri": uri, "sha256": sha(content)}], "attempt": 0, "status": "pending", "accepted": {}}
    reusable = await journal.get("reusable_merge/" + sha(content))
    if reusable:
        unit.update(status="accepted", receipt=reusable["receipt"])
    state["merge_count"] = number
    await journal.save(**{f"merge/{number}": unit})


async def plan_merge(engine, job, journal, state):
    """One extracted unit per tick, finite within-point windows, no global Reduce."""
    cursor = state["merge_planning_cursor"]
    unit = await journal.get(f"extract/{cursor}") if cursor <= state["extract_count"] else None
    buffer = state["merge_buffer"]
    members = []
    if unit:
        for file in unit["files"]:
            case = file["case"]
            partition = {key: case[key] for key in ("course", "chapter", "knowledge_point")}
            for observation in unit["accepted"].get(case_key(case), {}).get("observations", []):
                members.append((partition, {**observation, "member_ref": observation["observation_ref"]}))
    for partition, member in members:
        if not buffer:
            buffer = {"schema_version": "taikang.dimension-window.v1", "window_id": "window-000000",
                      "output_path": "analysis/dialogue_dimensions/receipts/window-000000.json",
                      "partition": partition, "members": [], "existing_dimensions": []}
        candidate = {**buffer, "members": [*buffer["members"], member]}
        if buffer["members"] and (partition != buffer["partition"]
                                  or not fits_window(candidate, job["dialogue_options"])):
            await make_window(engine, job, journal, state, buffer)
            buffer = {**buffer, "partition": partition, "members": []}
            candidate = {**buffer, "members": [member]}
        if not fits_window(candidate, job["dialogue_options"]):
            state["blocked_units"] += 1
            await artifact(engine, job, "issues/capacity-" + member["member_ref"] + ".jsonl",
                           canonical({"member_ref": member["member_ref"], "reason": "merge_capacity_pending"}) + "\n")
        else:
            buffer = candidate
    if unit is None:
        if buffer and buffer["members"]:
            await make_window(engine, job, journal, state, buffer)
        state.update(stage="merge", cursor=1, merge_buffer=None)
    else:
        state.update(merge_buffer=buffer, merge_planning_cursor=cursor + 1)
    await journal.save(summary=state)


async def finish(engine, job, journal, state):
    if sha(await frozen_read(engine, job, job["dataset_manifest_ref"])) != job["dataset_manifest_sha256"]:
        raise ValueError("dataset_manifest_changed_before_delivery")
    partial = bool(state["pending"] or state["blocked_units"])
    warnings = state["warnings"] + state["dimensions"]  # Single support/cross-window review is permitted.
    status = "interrupted" if partial else "succeeded"
    validation = {"delivery_status": "partial" if partial else "complete",
                  "validation_status": "blocked" if partial else "passed_with_warnings" if warnings else "passed",
                  "review_status": "recommended" if warnings else "not_required",
                  "processed_case_count": state["processed"], "pending_case_count": state["pending"],
                  "usable_observation_count": state["usable"], "isolated_observation_count": state["isolated"],
                  "dimension_count": state["dimensions"], "blocked_merge_unit_count": state["blocked_units"],
                  "structural_validation": "checked_per_unit", "citation_validation": "exact_frozen_unicode_spans",
                  "content_validated": False,
                  "warnings": ["Cross-window near synonyms are retained; no host similarity merge."]
                  if warnings else []}
    await artifact(engine, job, "validation.json", canonical(validation) + "\n")
    manifest = {"schema_version": "taikang.dialogue-delivery.v1", **validation,
                "dataset_manifest_ref": job["dataset_manifest_ref"],
                "dataset_manifest_sha256": job["dataset_manifest_sha256"],
                "compiler_sha256": sha(job["compiler_body_snapshot"]),
                "config_revision": job.get("config_snapshot", {}).get("miner_revision"),
                "dialogue_options": job["dialogue_options"],
                "extract_unit_count": state["extract_count"], "merge_unit_count": state["merge_count"],
                "manifest_page_count": state["page_count"],
                "manifest_page_template": "manifest_pages/part-{page:06d}.jsonl"}
    await artifact(engine, job, "manifest.json", canonical(manifest) + "\n")
    await artifact(engine, job, "index.md",
                   "# 优秀对话维度\n\n" + f"已分析 {state['processed']} 条；待处理 {state['pending']} 条；"
                   f"维度 {state['dimensions']} 个。\n\n"
                   "查看 manifest.json、validation.json 及 manifest_pages 各页。"
                   "结构与定位检查不代表业务结论已独立验证。\n")
    state["stage"] = "finished"
    await journal.save(summary=state)
    return await update(engine, job, **validation, status=status, materialized=True, ov_task_id="",
                        compile_request=None, finished_at=utc_now(),
                        phase={"step1": "done", "step2": "done", "step3": "done"})


async def tick(engine, job, action="poll"):
    journal = Journal(engine, job)
    state = await journal.get("summary")
    if not state:
        if job.get("ov_cancel_reason") or action == "stop":
            return await update(engine, job, status="stopped", finished_at=utc_now())
        state = await prepare(engine, job, journal)
    if state["stage"] == "finished":
        return await finish(engine, job, journal, state)
    if state["stage"] == "plan_merge":
        if job.get("ov_cancel_reason") or action == "stop":
            return await update(engine, job, status="stopped", delivery_status="partial", finished_at=utc_now())
        await plan_merge(engine, job, journal, state)
        return await update(engine, job, status="preparing", dialogue_stage=state["stage"])
    stage, cursor = state["stage"], state["cursor"]
    if cursor > state[f"{stage}_count"]:
        if stage == "extract":
            state.update(stage="plan_merge", cursor=1)
            await journal.save(summary=state)
            return await update(engine, job, status="preparing", ov_task_id="", compile_request=None)
        return await finish(engine, job, journal, state)
    unit = await journal.get(f"{stage}/{cursor}")
    if unit.get("projected"):
        raise ValueError("projected_unit_cursor_not_advanced")
    await update(engine, job, dialogue_stage=stage, dialogue_unit=cursor,
                 processed_case_count=state["processed"], pending_case_count=state["pending"])
    if action == "stop":
        job = await update(engine, job, status="stopping", ov_cancel_reason="用户手动停止")
    cancelling = bool(job.get("ov_cancel_reason"))
    if not unit.get("request") and cancelling:
        return await update(engine, job, status="stopped", delivery_status="partial", finished_at=utc_now())
    if unit["status"] == "accepted":
        await advance_unit(engine, job, journal, state, unit)
        return await asyncio.to_thread(engine.repository.get_job, job["job_id"])
    if unit["status"] in {"invalid", "rejected"}:
        if unit["attempt"] < job["dialogue_options"]["repair_attempts_per_unit"] and not cancelling:
            unit.update(attempt=unit["attempt"] + 1, status="pending")
            for key in ("request", "key", "task_id", "cancel_sent", "submitted_at"):
                unit.pop(key, None)
            await journal.save(**{f"{stage}/{cursor}": unit})
        else:
            await advance_unit(engine, job, journal, state, unit)
            return await asyncio.to_thread(engine.repository.get_job, job["job_id"])
    if not unit.get("task_id"):
        unit = await submit(engine, job, journal, unit)
        return await asyncio.to_thread(engine.repository.get_job, job["job_id"])
    task = await engine._knowledge_request(job["account"], "GET", "/api/v1/tasks/" + unit["task_id"], timeout=10)
    if str(task.get("task_id", unit["task_id"])) != unit["task_id"]:
        raise ValueError("foreign_remote_task")
    status = str(task.get("status") or "").lower()
    if status in TERMINAL_NATIVE_STATES:
        await engine.admission.release(task_id=unit["task_id"])
        if cancelling:
            return await update(engine, job, status="stopped", remote_terminal_unconfirmed=False,
                                delivery_status="partial", finished_at=utc_now())
        # Even failed tasks may contain valid child receipts. Missing children alone are retried.
        unit = await collect(engine, job, journal, unit)
        if unit["status"] == "accepted":
            await advance_unit(engine, job, journal, state, unit)
        return await asyncio.to_thread(engine.repository.get_job, job["job_id"])
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(unit["submitted_at"])).total_seconds()
    if (cancelling or elapsed >= job["runtime_timeout_seconds"]) and not unit.get("cancel_sent"):
        await engine._knowledge_request(job["account"], "POST", f"/api/v1/tasks/{unit['task_id']}/cancel", timeout=10)
        unit["cancel_sent"] = utc_now()
        await journal.save(**{f"{stage}/{cursor}": unit})
    if cancelling and unit.get("cancel_sent"):
        waiting = (datetime.now(timezone.utc) - datetime.fromisoformat(unit["cancel_sent"])).total_seconds()
        if waiting >= 120:
            return await update(engine, job, status="interrupted", remote_terminal_unconfirmed=True,
                                delivery_status="partial", error="远端取消终态未确认，保留槽位并继续对账")
    return await update(engine, job, ov_task_id=unit["task_id"],
                        status="stopping" if cancelling else "running", ov_status=status)
