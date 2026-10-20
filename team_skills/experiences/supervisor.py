"""A bounded dispatch pass owned by the shared worker lifecycle."""
from datetime import timedelta
from uuid import uuid4

from team_skills.config import load_skills_config
from team_skills.experiences.commands import ExperienceCommands
from team_skills.experiences.outbox import ExperienceOutbox
from team_skills.experiences.transport import target_config
from teamEvolver.runtime.leases import LeaseGuard


async def accept_scan(conn, services, *, tenant_id, command_id, operation, config):
    commands = ExperienceCommands(services)
    # Resolve idempotency before reading an ever-changing watermark.
    existing = await commands.get(conn, command_id)
    if existing:
        return await commands.accept(
            conn, tenant_id=tenant_id, command_id=command_id, operation=operation,
            request={"operation": operation}, payload=existing["payload"], config_revision=config.revision,
        )
    now = await conn.fetchval("SELECT clock_timestamp()")
    watermark = await conn.fetchval(
        f"SELECT watermark FROM {commands.schema}.experience_scan_state",
    )
    since = (watermark - timedelta(seconds=120)).isoformat() if watermark and operation == "sync" else None
    return await commands.accept(
        conn, tenant_id=tenant_id, command_id=command_id, operation=operation,
        request={"operation": operation}, payload={"until": now.isoformat(), "since": since},
        config_revision=config.revision,
    )


async def experience_tick(services, *, tenant_id, owner_id=""):
    commands, outbox = ExperienceCommands(services), ExperienceOutbox(services.tx)
    guard = LeaseGuard(services.leases)
    async with services.tx.tenant_tx(tenant_id) as conn:
        lease = await guard.acquire(
            conn, tenant_id=tenant_id, resource_key="experience:dispatch",
            owner_id=owner_id or uuid4().hex, lease_seconds=30,
        )
        if lease is None:
            return {"state": "standby", "enqueued": 0}
        try:
            config = await load_skills_config(services.extras["config_repo"], conn)
            latest = await commands.latest_scan(conn)
            now = await conn.fetchval("SELECT clock_timestamp()")
            interval = max(5, int((config.get("experience_sync") or {}).get("interval_seconds", 30)))
            enqueued = 0
            # Library projection remains independent of Skill upload, Evolution
            # and external sync enablement.
            if not latest or (latest["state"] not in {"queued", "running", "reconciling", "waiting_external"}
                              and now.timestamp() - latest["requested_at"] >= interval):
                await accept_scan(
                    conn, services, tenant_id=tenant_id, command_id="experience-scan:" + uuid4().hex,
                    operation="sync", config=config,
                )
                enqueued = 1
            if not (config.get("experience_sync") or {}).get("enabled", False):
                return {"state": "disabled", "enqueued": enqueued}
            target = target_config(config)
            for active in await outbox.active(conn):
                job = await services.tasks.get(conn, task_id=active["command_id"])
                if job and job["status"] not in {"cancelled", "failed"}:
                    return {"state": "active", "enqueued": enqueued}
                await outbox.receipt(
                    conn, active["event_id"], status="dead_letter", attempt=job["attempt"] if job else 0,
                    error_code="job_cancelled" if job and job["status"] == "cancelled" else "job_failed",
                )
            event_id = await outbox.next(conn)
            if not event_id:
                return {"state": "idle", "enqueued": enqueued}
            command_id = "experience-deliver:" + event_id
            await commands.accept(
                conn, tenant_id=tenant_id, command_id=command_id, operation="deliver",
                request={"event_id": event_id}, payload={"event_id": event_id, "target": target},
                config_revision=config.revision,
            )
            await outbox.dispatched(conn, event_id, command_id)
            return {"state": "enqueued", "enqueued": enqueued + 1, "job_id": command_id}
        finally:
            await guard.release(conn, lease)
