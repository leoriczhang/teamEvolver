"""Immutable command acceptance shared by the API and scheduled dispatch."""
from team_skills.evolution.repository import canonical, digest, lock_key, unpack
from team_skills.library.repository import SkillLibraryError, SkillMutationConflict


class ExperienceCommands:
    def __init__(self, services):
        self.services = services
        self.schema = services.tx.pool.schema

    async def get(self, conn, command_id):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.experience_commands WHERE command_id=$1", command_id,
        )
        return {**dict(row), **{k: unpack(row[k]) for k in ("payload", "result", "checkpoint")}} if row else None

    async def accept(self, conn, *, tenant_id, command_id, operation, request, payload, config_revision):
        await lock_key(conn, tenant_id, f"experience-command:{command_id}")
        existing = await self.get(conn, command_id)
        if existing:
            if existing["request_hash"] != digest(request) or existing["operation"] != operation:
                raise SkillMutationConflict("Idempotency-Key was used for a different experience command")
            return existing
        raw = canonical(payload)
        if len(raw.encode()) > 8 * 1024 * 1024:
            raise SkillLibraryError("experience command exceeds 8 MiB")
        await conn.execute(
            f"INSERT INTO {self.schema}.experience_commands "
            "(tenant_id,command_id,operation,request_hash,payload,payload_hash) VALUES ($1,$2,$3,$4,$5::jsonb,$6)",
            tenant_id, command_id, operation, digest(request), raw, digest(payload),
        )
        await self.services.tasks.enqueue(
            conn, tenant_id=tenant_id, task_id=command_id, kind="experience.sync",
            subject_type="experience", subject_id=payload.get("skill") or operation,
            schema_version="1", dedupe_key=command_id, payload_ref=command_id, payload_hash=digest(payload),
            config_revision=config_revision,
        )
        return await self.get(conn, command_id)

    async def checkpoint(self, conn, command_id, checkpoint):
        await conn.execute(
            f"UPDATE {self.schema}.experience_commands SET checkpoint=$2::jsonb WHERE command_id=$1",
            command_id, canonical(checkpoint),
        )

    async def finish(self, conn, command_id, result):
        await conn.execute(
            f"UPDATE {self.schema}.experience_commands SET result=$2::jsonb,finished_at=now() "
            "WHERE command_id=$1 AND result IS NULL", command_id, canonical(result),
        )

    async def latest_scan(self, conn):
        row = await conn.fetchrow(
            f"SELECT c.command_id,c.operation,c.created_at,c.finished_at,c.checkpoint,t.status,t.error_code "
            f"FROM {self.schema}.experience_commands c JOIN {self.schema}.worker_tasks t "
            "ON t.tenant_id=c.tenant_id AND t.task_id=c.command_id "
            "WHERE c.operation IN ('sync','import_all') ORDER BY c.created_at DESC LIMIT 1",
        )
        if not row:
            return None
        return {
            "request_id": row["command_id"], "operation": row["operation"],
            "state": {"succeeded": "completed", "retry_wait": "queued"}.get(row["status"], row["status"]),
            "requested_at": row["created_at"].timestamp(),
            "finished_at": row["finished_at"].timestamp() if row["finished_at"] else None,
            "error_code": row["error_code"],
        }
