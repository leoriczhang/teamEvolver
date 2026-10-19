"""Experience scan, semantic compilation and projection on the shared Worker."""
from __future__ import annotations

import asyncio
from datetime import datetime

from session_ingestion.queries import SessionQueries
from team_skills.evolution.repository import digest, lock_key
from team_skills.experiences.commands import ExperienceCommands
from team_skills.experiences.patterns import normalize_patterns, source_fingerprint
from team_skills.experiences.repository import ExperienceRepository
from team_skills.experiences.transport import SyncFailure, project
from teamEvolver.runtime import HandlerResult
from teamEvolver.runtime.leases import LeaseGuard


async def sync(task, services):
    commands = ExperienceCommands(services)
    repo = ExperienceRepository(services.tx)
    guard = LeaseGuard(services.leases)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        command = await commands.get(conn, task.payload_ref)
        if (not command or command["payload_hash"] != task.payload_hash
                or digest(command["payload"]) != task.payload_hash):
            return HandlerResult.fail("experience_payload_mismatch")
        if command["result"] is not None:
            return HandlerResult.succeed(task.task_id)
        operation, payload = command["operation"], command["payload"]
        resource = "compile:" + payload["skill"] if operation == "compile" else (
            "deliver" if operation == "deliver" else "scan")
        handle = await guard.acquire(
            conn, tenant_id=task.tenant_id, resource_key="experience:" + resource,
            owner_id=f"{task.task_id}:{task.fence}", lease_seconds=180,
        )
        if handle is None:
            return HandlerResult.retry("experience_busy")

    async def current(conn):
        await services.tasks.require_current(conn, task)
        if not await guard.heartbeat(conn, handle, lease_seconds=180):
            raise RuntimeError("experience resource lease lost")

    try:
        if operation in {"sync", "import_all"}:
            return await _scan(task, services, command, commands, repo, current)
        if operation == "compile":
            return await _compile(task, services, command, commands, repo, current)
        return await _deliver(task, services, command, commands, repo, current)
    finally:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard.release(conn, handle)


async def _scan(task, services, command, commands, repo, current):
    payload, checkpoint = command["payload"], command["checkpoint"]
    until = datetime.fromisoformat(payload["until"])
    since = datetime.fromisoformat(payload["since"]) if payload.get("since") else None
    cursor = checkpoint.get("cursor")
    cursor = (datetime.fromisoformat(cursor[0]), cursor[1]) if cursor else None
    processed = checkpoint.get("processed", 0)
    while True:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await current(conn)
            rows = await SessionQueries(services.tx).experience_page(conn, until=until, since=since, after=cursor)
            await repo.ingest(conn, tenant_id=task.tenant_id, sessions=rows)
            processed += len(rows)
            if rows:
                cursor = rows[-1]["ingested_at"], rows[-1]["session_id"]
            await commands.checkpoint(conn, task.task_id, {
                "processed": processed, "cursor": [cursor[0].isoformat(), cursor[1]] if cursor else None,
            })
            if len(rows) < 100:
                await conn.execute(
                    f"INSERT INTO {repo.schema}.experience_scan_state(tenant_id,watermark) VALUES ($1,$2) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET watermark=GREATEST(experience_scan_state.watermark,$2),"
                    "updated_at=now()", task.tenant_id, until,
                )
                await commands.finish(conn, task.task_id, {"processed_sessions": processed})
                return HandlerResult.succeed(task.task_id)


async def _compile(task, services, command, commands, repo, current):
    p = command["payload"]
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        existing = await repo.compilation(conn, p["skill"])
        previous = await repo.patterns(conn, p["skill"])
        if existing and existing["source_hash"] == p["source_hash"] and not p["force"]:
            await current(conn)
            await commands.finish(conn, task.task_id, {
                "compiled": False, "skill_name": p["skill"], "pattern_count": len(previous),
            })
            return HandlerResult.succeed(task.task_id)
    compiler = services.extras.get("experience_pattern_compiler")
    if compiler is None:
        return HandlerResult.fail("pattern_compiler_missing")
    try:
        raw = await asyncio.wait_for(compiler(skill=p["skill"], experiences=p["experiences"], previous=previous), 120)
        if not isinstance(raw, list):
            raise ValueError()
        patterns = normalize_patterns(p["skill"], raw, p["experiences"], previous)
    except (ValueError, TypeError):
        return HandlerResult.fail("pattern_compiler_invalid_output")
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await current(conn)
        # A delayed compile cannot replace a newer projection of the evidence.
        await lock_key(conn, task.tenant_id, f"patterns:{p['skill']}")
        if source_fingerprint(await repo.compile_inputs(conn, p["skill"])) != p["source_hash"]:
            return HandlerResult.fail("experience_source_changed")
        result = await repo.save_patterns(
            conn, tenant_id=task.tenant_id, skill=p["skill"], source_hash=p["source_hash"], patterns=patterns,
        )
        await commands.finish(conn, task.task_id, result)
    return HandlerResult.succeed(task.task_id)


async def _deliver(task, services, command, commands, repo, current):
    p = command["payload"]
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        event = await repo.outbox.get(conn, p["event_id"])
        if not event or event["command_id"] != task.task_id:
            return HandlerResult.fail("experience_event_missing")
        if event["status"] == "dead_letter":
            return HandlerResult.fail(event["error_code"] or "experience_dead_letter")
        if digest(event["document"]) != event["content_hash"]:
            return HandlerResult.fail("experience_document_hash_mismatch")

    async def before_send():
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await current(conn)

    try:
        secrets = services.extras.get("secret_repo")
        if secrets is None:
            raise SyncFailure("experience_secret_resolver_missing")
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            try:
                token = await secrets.resolve(conn, secret_ref=p["target"]["secret_ref"])
            except (ValueError, KeyError):
                raise SyncFailure("experience_secret_unavailable") from None
        receipt = await project(
            target=p["target"], token=token, tenant_id=task.tenant_id, event=event, before_send=before_send,
            transport=services.extras.get("experience_sync_transport"),
        )
    except SyncFailure as exc:
        terminal = not exc.retryable or task.attempt >= 3
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await current(conn)
            await repo.outbox.receipt(
                conn, event["event_id"], status="dead_letter" if terminal else "queued",
                attempt=task.attempt, error_code=exc.code, http_status=exc.status, **exc.receipt,
            )
        return HandlerResult.fail(exc.code) if terminal else HandlerResult.retry(exc.code)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await current(conn)
        await repo.outbox.receipt(conn, event["event_id"], status="synced", attempt=task.attempt, **receipt)
        await commands.finish(conn, task.task_id, {"event_id": event["event_id"], "status": "synced"})
    return HandlerResult.succeed(task.task_id)
