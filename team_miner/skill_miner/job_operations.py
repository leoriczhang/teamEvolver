"""Worker-side job mutations preserving the original Skill mining rules."""

from __future__ import annotations

import asyncio
import copy
import hashlib

from .artifacts import save_artifact
from .cache import persist_cache
from .mining_contracts import RUNS_ROOT, SOURCE_TAG_SKILL, source_has_tag, validate_resource_uri
from .native_compiler import is_native
from .settings import LEGACY_COMPILER_WARNING, compiler_contract_version, compiler_prompt, pipeline_settings
from .task_io import enqueue_compile


async def public(repository, job):
    from .presentation import clean
    return clean(await asyncio.to_thread(repository.public_job, job, detail=True))


async def restore(engine, job_id):
    return await engine.restore(job_id)


async def create(engine, body, identifier):
    repository = engine.repository
    # The operation identity, rather than a process-local random value, binds
    # recovery after a lost acknowledgement to the same business job.
    job_id = "ovmine-" + hashlib.sha256(identifier.encode()).hexdigest()[:32]
    existing = await asyncio.to_thread(repository.backend.load_job, job_id)
    if existing is not None:
        job = await restore(engine, job_id)
        if not job.get("config_snapshot"):
            job = await asyncio.to_thread(repository.update_job, job_id, config_snapshot=engine.settings)
        await asyncio.to_thread(persist_cache, repository, job_id)
        if job.get("status") in {"preparing", "queued", "running", "stopping"}:
            await enqueue_compile(engine.services, engine.tenant, job, job["config_snapshot"])
        return {"ok": True, "jobs": [await public(repository, job)], "summary": repository.summary([job])}
    source_uri = validate_resource_uri(body.get("input_dir"))
    records = await asyncio.to_thread(repository.list_source_records)
    source_id = str(body.get("source_id") or "")
    record = next((row for row in records if (row["source_id"] == source_id if source_id
                                              else row["uri"] == source_uri)), None)
    if record is None or record["uri"] != source_uri:
        raise ValueError("请先将所选 OpenViking 目录登记为知识源")
    miner = engine.settings.get("miner", engine.settings.get("miner_config", {}))
    if record["account"] != miner["account_id"]:
        raise ValueError("TENANT_ACCOUNT_MISMATCH")
    if not source_has_tag(record, SOURCE_TAG_SKILL):
        raise ValueError("所选目录没有“Skill 知识源”标签")
    source = await engine._ov_source_detail(record)
    if not source.get("ready"):
        raise ValueError("知识源目录中没有可挖掘文件或目录清单不完整")
    name = str(body.get("name") or f"{source.get('name')} 挖掘任务").strip()
    if len(name) > 120:
        raise ValueError("任务名称不能超过 120 个字符")
    settings = engine.settings.get("settings", engine.settings.get("config", {}))
    defaults = pipeline_settings(settings)
    max_rounds = body.get("max_rounds", defaults["max_rounds"])
    timeout = body.get("runtime_timeout_seconds", defaults["runtime_timeout_seconds"])
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("运行期限必须为整数")
    compiler = compiler_prompt(settings)
    version = compiler_contract_version(compiler)
    # The very first metadata INSERT includes the frozen accepted configuration.
    # A crash before the file checkpoint can reconstruct the compiler and queue
    # a monitor using this same snapshot, even after settings change.
    repository.backend.transform_save = lambda value: {**value, "config_snapshot": copy.deepcopy(engine.settings)}
    try:
        job = await asyncio.to_thread(repository.create_job, job_id=job_id, name=name, account=record["account"],
        source_uri=source_uri, source_snapshot_uri=f"{RUNS_ROOT}/pending/source",
        output_uri=f"{RUNS_ROOT}/pending/output",
        document_count=int(source.get("document_count") or 0), max_rounds=max_rounds,
        runtime_timeout_seconds=timeout, human_checkpoints=bool(body.get("human_checkpoints", True)),
        skip_empty_checkpoints=True, consolidate_reviewed_candidates=True, candidate_contract_version=version,
        compiler_body=compiler, mining_mode=body.get("mining_mode", "auto"),
        mining_direction=body.get("mining_direction", ""), target_skill_count=body.get("target_skill_count"))
    finally:
        repository.backend.transform_save = None
    job = await asyncio.to_thread(repository.get_job, job_id)
    await asyncio.to_thread(persist_cache, repository, job_id)
    repository.backend.before_save = engine._checkpoint
    engine._loaded_job = job_id
    job = await asyncio.to_thread(repository.update_job, job_id, config_snapshot=engine.settings,
                                 options_warnings=[LEGACY_COMPILER_WARNING] if version == 1 else [])
    await enqueue_compile(engine.services, engine.tenant, job, engine.settings)
    return {"ok": True, "jobs": [await public(repository, job)], "summary": repository.summary([job])}


async def execute(operation, body, engine, job_id, *, identifier, submitted_by):
    if operation == "job.create":
        return await create(engine, body, identifier)
    repository = engine.repository
    if operation == "candidate.submit":
        from .candidate_bridge import submit
        return await submit(engine, body, job_id, submitted_by)
    if operation == "job.detail":
        from .cache import restore_cache
        from .presentation import publish
        job = await asyncio.to_thread(repository.backend.load_job, job_id)
        if not job or job.get("_deleted"):
            raise KeyError(job_id)
        engine._check_account(job.get("account"))
        await asyncio.to_thread(restore_cache, repository, job_id)
        await publish(engine, job)
        return {"ok": True, "job": await public(repository, job)}
    if operation == "job.delete":
        prior = await asyncio.to_thread(repository.backend.load_job, job_id)
        receipt = (prior or {}).get("delete_receipt") or {}
        if prior and prior.get("_deleted") and receipt.get("operation_id") == identifier:
            return dict(receipt["result"])
    job = await restore(engine, job_id)
    if operation == "artifact.save":
        result = await asyncio.to_thread(save_artifact, repository.project_root, body.get("path", ""),
                                        body.get("content"), job=job)
        job = await asyncio.to_thread(repository.refresh_edited_quality, job_id, body["path"])
        await asyncio.to_thread(persist_cache, repository, job_id)
        return {**result, "job": await public(repository, job)}
    if operation == "job.delete":
        root = repository.jobs_root / job_id
        count = await asyncio.to_thread(lambda: sum(path.is_file() for path in root.rglob("*")))
        receipt = {"operation_id": identifier, "result": {"ok": True, "job_id": job_id, "deleted_files": count}}
        repository.backend.transform_save = lambda value: {**value, "delete_receipt": receipt}
        try:
            return await asyncio.to_thread(repository.delete_job, job_id)
        finally:
            repository.backend.transform_save = None
    if operation == "job.stop":
        if job.get("status") != "stopped":
            job = await engine.stop(job)
    elif operation == "job.resume":
        authorization = None
        if body:
            if (not is_native(job) or body.get("action") != "retry_with_new_budget"
                    or set(body) != {"action", "request_id", "expected_task_id", "budget_seconds"}):
                raise ValueError("不支持的恢复动作或授权字段")
            authorization = {**body, "authorized_by": submitted_by}
        job = (await engine._resume_native_resource_failure(job_id, authorization) if is_native(job)
               else await asyncio.to_thread(repository.resume_identity_failure, job_id))
    elif operation == "job.supplements":
        job = await engine._stage_round_supplements(job, str(body.get("checkpoint_id") or ""), body.get("files"))
    elif operation in {"job.continue", "job.finish"}:
        fields = {"answers": body.get("answers", {}), "unresolved": body.get("unresolved", []),
                  "references": body.get("references", {}), "feedback": body.get("feedback", ""),
                  "request_id": body.get("request_id", ""), "submitted_by": submitted_by}
        checkpoint = str(body.get("checkpoint_id") or "")
        if job.get("status") == "waiting_human":
            validated = await asyncio.to_thread(
                repository._validate_submission, job, checkpoint, fields["answers"],
                fields["unresolved"], fields["references"], fields["feedback"], fields["request_id"],
                submitted_by, None)
            job = await engine._stage_round_references(job, validated["references"])
            await asyncio.to_thread(repository.finalize_materialized, job_id)
        action = repository.finish_job if operation == "job.finish" else repository.prepare_next_round
        job = await asyncio.to_thread(action, job_id, checkpoint, **fields)
    else:
        raise ValueError("unknown Skill mining job operation")
    await asyncio.to_thread(persist_cache, repository, job_id)
    if job.get("status") in {"preparing", "queued", "running", "stopping"} or job.get("remote_terminal_unconfirmed"):
        await enqueue_compile(engine.services, engine.tenant, job, job.get("config_snapshot") or engine.settings)
    value = await public(repository, job)
    return {"ok": True, "job": value, "saved": (value.get("pending_checkpoint") or {}).get("attachments", [])}
