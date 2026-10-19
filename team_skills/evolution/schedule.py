"""Persistent automatic Skill evolution dispatch."""

from __future__ import annotations

from uuid import uuid4

from team_skills.config import load_skills_config

from .candidate_pipeline import enqueue_command

_ACTIVE = ("queued", "running", "retry_wait", "waiting_external", "reconciling")


async def evolution_tick(services, *, tenant_id: str) -> dict:
    """Enqueue one evolution cycle when the tenant's automatic policy is due."""
    async with services.tx.tenant_tx(tenant_id) as conn:
        config = await load_skills_config(services.extras["config_repo"], conn)
        if config.get("evolution_trigger_mode") != "automatic":
            return {"state": "manual"}
        interval = max(60, min(604800, int(config.get("evolution_interval_seconds") or 600)))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{tenant_id}:skill-evolution-schedule",
        )
        active = await conn.fetchval(
            f"SELECT 1 FROM {services.tx.pool.schema}.worker_tasks t "
            f"JOIN {services.tx.pool.schema}.skill_commands c "
            "ON c.tenant_id=t.tenant_id AND c.command_id=t.task_id "
            "WHERE t.kind='skill.evolve' AND t.status=ANY($1::text[]) "
            "AND c.payload->>'operation' IS NULL LIMIT 1",
            list(_ACTIVE),
        )
        if active:
            return {"state": "active"}
        now = await conn.fetchval("SELECT clock_timestamp()")
        latest = await conn.fetchval(
            f"SELECT max(created_at) FROM {services.tx.pool.schema}.skill_commands "
            "WHERE kind='skill.evolve' AND payload->>'operation' IS NULL",
        )
        if latest is not None and (now - latest).total_seconds() < interval:
            return {"state": "waiting"}
        counts = await conn.fetchrow(
            f"SELECT count(*) AS sessions,count(DISTINCT s.user_id) AS users "
            f"FROM {services.tx.pool.schema}.sessions s WHERE s.decision='valuable' AND NOT EXISTS ("
            f"SELECT 1 FROM {services.tx.pool.schema}.skill_evolution_sessions e "
            "WHERE e.session_id=s.session_id AND e.content_hash=s.content_hash)"
        )
        min_sessions = max(0, int(config.get("min_group_sessions") or 0))
        min_users = max(0, int(config.get("min_group_users") or 0))
        if (
            not int(counts["sessions"] or 0)
            or (min_sessions and int(counts["sessions"] or 0) < min_sessions)
            or (min_users and int(counts["users"] or 0) < min_users)
        ):
            return {"state": "insufficient_sessions"}
        command_id = "evolve:auto:" + uuid4().hex
        request = {"session_ids": []}
        command, _ = await enqueue_command(
            conn,
            services,
            tenant_id=tenant_id,
            command_id=command_id,
            kind="skill.evolve",
            request=request,
            payload={
                "session_ids": [],
                "actor": "scheduler",
                "config": config.config,
                "config_revision": config.revision,
                "accepted_at": now.isoformat(),
            },
        )
        return {"state": "enqueued", "job_id": command["command_id"]}
