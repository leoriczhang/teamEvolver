"""Atomic projection intents and bounded, safe receipts."""
from team_skills.evolution.repository import canonical, digest, unpack


class ExperienceOutbox:
    def __init__(self, tx):
        self.schema = tx.pool.schema

    async def append(self, conn, *, tenant_id, subject_type, subject_id, revision, document):
        event_id = digest([tenant_id, subject_type, subject_id, revision])
        await conn.execute(
            f"INSERT INTO {self.schema}.experience_sync_outbox "
            "(tenant_id,event_id,subject_type,subject_id,revision,document,content_hash) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7) ON CONFLICT DO NOTHING",
            tenant_id, event_id, subject_type, subject_id, revision, canonical(document), digest(document),
        )
        return event_id

    async def get(self, conn, event_id):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.experience_sync_outbox WHERE event_id=$1", event_id,
        )
        return {**dict(row), "document": unpack(row["document"])} if row else None

    async def active(self, conn):
        return await conn.fetch(
            f"SELECT event_id,command_id FROM {self.schema}.experience_sync_outbox WHERE status='queued'",
        )

    async def next(self, conn):
        return await conn.fetchval(
            f"SELECT event_id FROM {self.schema}.experience_sync_outbox o WHERE status='pending' "
            f"AND NOT EXISTS (SELECT 1 FROM {self.schema}.experience_sync_outbox earlier "
            "WHERE earlier.tenant_id=o.tenant_id AND earlier.subject_type=o.subject_type "
            "AND earlier.subject_id=o.subject_id AND earlier.revision<o.revision "
            "AND earlier.status IN ('pending','queued')) "
            "ORDER BY created_at,event_id LIMIT 1 FOR UPDATE OF o SKIP LOCKED",
        )

    async def dispatched(self, conn, event_id, command_id):
        await conn.execute(
            f"UPDATE {self.schema}.experience_sync_outbox SET status='queued',command_id=$2,"
            "updated_at=now() WHERE event_id=$1", event_id, command_id,
        )

    async def receipt(self, conn, event_id, *, status, attempt, error_code="", http_status=None,
                      content_written=False, semantic_status="unknown", vector_status="unknown"):
        await conn.execute(
            f"UPDATE {self.schema}.experience_sync_outbox SET status=$2,attempt=$3,error_code=$4,http_status=$5,"
            "content_written=$6,semantic_status=$7,vector_status=$8,updated_at=now() WHERE event_id=$1",
            event_id, status, attempt, error_code, http_status, content_written, semantic_status, vector_status,
        )

    async def status(self, conn):
        rows = await conn.fetch(
            f"SELECT status,count(*) AS n FROM {self.schema}.experience_sync_outbox GROUP BY status",
        )
        counts = {r["status"]: r["n"] for r in rows}
        retry = await conn.fetchrow(
            f"SELECT count(*) AS n, count(*) FILTER (WHERE t.available_at<=now()) AS due,"
            "min(extract(epoch FROM t.available_at))::double precision AS next_at "
            f"FROM {self.schema}.experience_sync_outbox o JOIN {self.schema}.worker_tasks t "
            "ON t.tenant_id=o.tenant_id AND t.task_id=o.command_id WHERE o.status='queued' AND o.attempt>0",
        )
        failure = await conn.fetchrow(
            f"SELECT error_code,updated_at FROM {self.schema}.experience_sync_outbox WHERE error_code<>'' "
            "ORDER BY updated_at DESC LIMIT 1",
        )
        pending_index = await conn.fetchval(
            f"SELECT count(*) FROM {self.schema}.experience_sync_outbox WHERE content_written "
            "AND (semantic_status<>'complete' OR vector_status<>'complete')",
        )
        return {
            "counts": {"pending": counts.get("pending", 0) + counts.get("queued", 0) - retry["n"],
                       "synced": counts.get("synced", 0), "retry": retry["n"],
                       "dead_letter": counts.get("dead_letter", 0)},
            "index_pending": pending_index,
            "retry": {"due": retry["due"], "deferred": retry["n"] - retry["due"],
                      "next_attempt_at": retry["next_at"], "last_attempt_at": None,
                      "error_counts": {}, "samples": []},
            "last_error": failure["error_code"] if failure else None,
            "last_error_at": failure["updated_at"].timestamp() if failure else None,
        }
