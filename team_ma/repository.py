"""RLS-scoped connection revisions, checkpoints and external effect receipts."""

from __future__ import annotations

from .schemas import MAConflict, canonical, digest, target_fingerprint, unpack


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("config", "detail", "record", "input", "checkpoint", "result", "intent", "receipt"):
        if key in result and result[key] is not None:
            result[key] = unpack(result[key])
    return result


class MARepository:
    def __init__(self, tx):
        self.tx, self.schema = tx, tx.pool.schema

    async def connection(self, conn, connection_id, revision=None):
        row = await conn.fetchrow(
            f"SELECT c.*,r.config,r.config_hash FROM {self.schema}.ma_connections c "
            f"JOIN {self.schema}.ma_connection_revisions r USING(tenant_id,connection_id) "
            "WHERE c.connection_id=$1 AND r.revision=COALESCE($2,c.revision)",
            connection_id, revision,
        )
        value = _row(row)
        if value is not None and revision is not None:
            value["revision"] = revision
        return value

    async def connections(self, conn, *, limit=100, after=""):
        return [_row(r) for r in await conn.fetch(
            f"SELECT c.*,r.config,r.config_hash FROM {self.schema}.ma_connections c "
            f"JOIN {self.schema}.ma_connection_revisions r USING(tenant_id,connection_id) "
            "WHERE r.revision=c.revision AND c.connection_id>$1 ORDER BY c.connection_id LIMIT $2",
            after, min(max(1, limit), 100),
        )]

    async def save(self, conn, *, tenant_id, connection_id, config, expected_revision=0, actor=""):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           f"{tenant_id}:ma:{connection_id}")
        current = await self.connection(conn, connection_id)
        if current and current["revision"] != expected_revision:
            raise MAConflict("Connection revision changed; refresh before saving")
        if not current and expected_revision:
            raise MAConflict("Connection not found")
        if current and current["target_fingerprint"] != target_fingerprint(config):
            raise MAConflict("Entry Agent and origin are immutable; create another connection")
        revision = expected_revision + 1
        if not current:
            try:
                await conn.execute(
                    f"INSERT INTO {self.schema}.ma_connections"
                    "(tenant_id,connection_id,target_fingerprint,enabled) VALUES($1,$2,$3,$4)",
                    tenant_id, connection_id, target_fingerprint(config), config.get("enabled", True),
                )
            except Exception as exc:
                if getattr(exc, "sqlstate", "") == "23505":
                    raise MAConflict("This remote entry Agent is already managed") from None
                raise
        else:
            await conn.execute(
                f"UPDATE {self.schema}.ma_connections SET revision=$2,enabled=$3,updated_at=now() "
                "WHERE connection_id=$1", connection_id, revision, config.get("enabled", True),
            )
        await conn.execute(
            f"INSERT INTO {self.schema}.ma_connection_revisions"
            "(tenant_id,connection_id,revision,config,config_hash,actor) VALUES($1,$2,$3,$4::jsonb,$5,$6)",
            tenant_id, connection_id, revision, canonical(config), digest(config), actor,
        )
        return await self.connection(conn, connection_id)

    async def status(self, conn, connection_id, status, detail=None, *, collected=False, synced=False):
        detail = dict(detail or {})
        if status == "ready":
            detail.pop("error", None)
            detail.pop("job_id", None)
        await conn.execute(
            f"UPDATE {self.schema}.ma_connections SET status=$2,detail=$3::jsonb,updated_at=now(), "
            "last_collected_at=CASE WHEN $4 THEN now() ELSE last_collected_at END,"
            "last_synced_at=CASE WHEN $5 THEN now() ELSE last_synced_at END WHERE connection_id=$1",
            connection_id, status, canonical(detail or {}), collected, synced,
        )

    async def resources(self, conn, connection_id, *, limit=100, after="", kind=None):
        return [_row(r) for r in await conn.fetch(
            f"SELECT * FROM {self.schema}.ma_resource_bindings WHERE connection_id=$1 "
            "AND ($2::text IS NULL OR kind=$2) AND logical_name>$3 ORDER BY logical_name,kind LIMIT $4",
            connection_id, kind, after, min(max(1, limit), 100),
        )]

    async def all_resources(self, conn, connection_id, *, kind):
        output, after = [], ""
        for _ in range(20):
            rows = await self.resources(conn, connection_id, kind=kind, after=after)
            output.extend(rows)
            if len(rows) < 100:
                return output
            after = rows[-1]["logical_name"]
        raise ValueError("MA resource pagination limit exceeded")

    async def bind(self, conn, *, tenant_id, connection_id, kind, name, record, expected_revision=None):
        if expected_revision is not None:
            changed = await conn.fetchval(
                f"UPDATE {self.schema}.ma_resource_bindings SET record=$4::jsonb,revision=revision+1,updated_at=now() "
                "WHERE connection_id=$1 AND kind=$2 AND logical_name=$3 AND revision=$5 RETURNING revision",
                connection_id, kind, name, canonical(record), expected_revision,
            )
            if changed is None:
                raise MAConflict("Resource binding revision changed")
            return changed
        await conn.execute(
            f"INSERT INTO {self.schema}.ma_resource_bindings(tenant_id,connection_id,kind,logical_name,record) "
            "VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(tenant_id,connection_id,kind,logical_name) "
            "DO UPDATE SET record=EXCLUDED.record,revision="
            f"{self.schema}.ma_resource_bindings.revision+1,updated_at=now()",
            tenant_id, connection_id, kind, name, canonical(record),
        )

    async def create_binding(self, conn, *, tenant_id, connection_id, name, record):
        changed = await conn.fetchval(
            f"INSERT INTO {self.schema}.ma_resource_bindings(tenant_id,connection_id,kind,logical_name,record) "
            "VALUES($1,$2,'skill',$3,$4::jsonb) ON CONFLICT DO NOTHING RETURNING revision",
            tenant_id, connection_id, name, canonical(record),
        )
        if changed is None:
            raise MAConflict("Resource binding revision changed")
        return changed

    async def operation(self, conn, operation_id):
        return _row(await conn.fetchrow(
            f"SELECT * FROM {self.schema}.ma_operations WHERE operation_id=$1", operation_id,
        ))

    async def accept(self, conn, *, tenant_id, connection_id, operation_id, kind, payload):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           f"{tenant_id}:ma-op:{operation_id}")
        current = await self.operation(conn, operation_id)
        if current:
            if current["input_hash"] != digest(payload) or current["kind"] != kind:
                raise MAConflict("Idempotency-Key already used for another operation")
            return current, False
        await conn.execute(
            f"INSERT INTO {self.schema}.ma_operations"
            "(tenant_id,connection_id,operation_id,kind,input,input_hash) VALUES($1,$2,$3,$4,$5::jsonb,$6)",
            tenant_id, connection_id, operation_id, kind, canonical(payload), digest(payload),
        )
        return await self.operation(conn, operation_id), True

    async def checkpoint(self, conn, operation_id, checkpoint, result=None):
        await conn.execute(
            f"UPDATE {self.schema}.ma_operations SET checkpoint=$2::jsonb,result=$3::jsonb WHERE operation_id=$1",
            operation_id, canonical(checkpoint), canonical(result) if result is not None else None,
        )

    async def cursor(self, conn, connection_id, session_id):
        return _row(await conn.fetchrow(
            f"SELECT * FROM {self.schema}.ma_session_cursors WHERE connection_id=$1 AND session_id=$2",
            connection_id, session_id,
        ))

    async def cursors(self, conn, connection_id, *, limit=100, after=""):
        return [_row(r) for r in await conn.fetch(
            f"SELECT * FROM {self.schema}.ma_session_cursors WHERE connection_id=$1 AND session_id>$2 "
            "ORDER BY session_id LIMIT $3", connection_id, after, limit,
        )]

    async def save_cursor(self, conn, *, tenant_id, connection_id, session_id, record):
        await conn.execute(
            f"INSERT INTO {self.schema}.ma_session_cursors(tenant_id,connection_id,session_id,record) "
            "VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(tenant_id,connection_id,session_id) "
            "DO UPDATE SET record=EXCLUDED.record,updated_at=now()",
            tenant_id, connection_id, session_id, canonical(record),
        )

    async def release(self, conn, connection_id, release_id):
        return _row(await conn.fetchrow(
            f"SELECT * FROM {self.schema}.ma_releases WHERE connection_id=$1 AND release_id=$2",
            connection_id, release_id,
        ))

    async def releases(self, conn, connection_id, *, limit=100, before=None):
        return [_row(r) for r in await conn.fetch(
            f"SELECT * FROM {self.schema}.ma_releases WHERE connection_id=$1 "
            "AND ($2::timestamptz IS NULL OR created_at<$2) ORDER BY created_at DESC LIMIT $3",
            connection_id, before, min(max(limit, 1), 100),
        )]

    async def applied(self, conn, connection_id):
        return _row(await conn.fetchrow(
            f"SELECT * FROM {self.schema}.ma_releases WHERE connection_id=$1 AND status='applied' "
            "ORDER BY applied_at DESC LIMIT 1", connection_id,
        ))

    async def save_release(self, conn, *, tenant_id, connection_id, release_id, manifest_hash, record,
                           status="pending"):
        await conn.execute(
            f"INSERT INTO {self.schema}.ma_releases"
            "(tenant_id,connection_id,release_id,manifest_hash,record,status,applied_at) "
            "VALUES($1,$2,$3,$4,$5::jsonb,$6,CASE WHEN $6='applied' THEN now() ELSE NULL END) "
            "ON CONFLICT(tenant_id,connection_id,release_id) DO UPDATE SET "
            "record=EXCLUDED.record,status=EXCLUDED.status,applied_at=CASE WHEN EXCLUDED.status='applied' "
            f"THEN COALESCE({self.schema}.ma_releases.applied_at,now()) ELSE NULL END",
            tenant_id, connection_id, release_id, manifest_hash, canonical(record), status,
        )

    async def effect(self, conn, effect_id):
        return _row(await conn.fetchrow(
            f"SELECT * FROM {self.schema}.ma_external_effects WHERE effect_id=$1", effect_id,
        ))

    async def prepare_effect(self, conn, *, tenant_id, connection_id, effect_id, owner_id, kind, intent):
        existing = await self.effect(conn, effect_id)
        if existing:
            if existing["request_hash"] != digest(intent):
                raise MAConflict("External effect intent changed")
            return existing, False
        await conn.execute(
            f"INSERT INTO {self.schema}.ma_external_effects"
            "(tenant_id,connection_id,effect_id,owner_id,kind,request_hash,intent) "
            "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
            tenant_id, connection_id, effect_id, owner_id, kind, digest(intent), canonical(intent),
        )
        return await self.effect(conn, effect_id), True

    async def confirm_effect(self, conn, effect_id, remote_id, receipt, *, status="confirmed"):
        await conn.execute(
            f"UPDATE {self.schema}.ma_external_effects SET status=$2,remote_id=$3,receipt=$4::jsonb,updated_at=now() "
            "WHERE effect_id=$1", effect_id, status, remote_id, canonical(receipt),
        )

    async def effects(self, conn, connection_id, *, owner_id=None, status=None):
        return [_row(r) for r in await conn.fetch(
            f"SELECT * FROM {self.schema}.ma_external_effects WHERE connection_id=$1 "
            "AND ($2::text IS NULL OR owner_id=$2) AND ($3::text IS NULL OR status=$3) "
            "ORDER BY created_at LIMIT 1000", connection_id, owner_id, status,
        )]

    async def replay_session_ids(self, conn, connection_id):
        rows = await conn.fetch(
            f"SELECT remote_id FROM {self.schema}.ma_external_effects WHERE connection_id=$1 "
            "AND kind='replay-session' AND remote_id<>''", connection_id,
        )
        return {r["remote_id"] for r in rows}

    async def retire_session_events(self, conn, connection_id):
        """Deleted Sessions also retire message intents; events have no DELETE API."""
        await conn.execute(
            f"UPDATE {self.schema}.ma_external_effects e SET status='cleaned',updated_at=now() "
            "WHERE e.connection_id=$1 AND e.kind='replay-event' AND e.status<>'cleaned' "
            f"AND EXISTS(SELECT 1 FROM {self.schema}.ma_external_effects s "
            "WHERE s.tenant_id=e.tenant_id AND s.connection_id=e.connection_id AND s.owner_id=e.owner_id "
            "AND s.kind='replay-session' AND s.status='cleaned' AND s.remote_id<>'' "
            "AND s.remote_id=e.intent->>'session_id')",
            connection_id,
        )

    async def discovery_snapshot(self, conn, connection_id, revision):
        row = await conn.fetchrow(
            f"SELECT checkpoint FROM {self.schema}.ma_operations WHERE connection_id=$1 AND kind='ma.discover' "
            "AND input->>'revision'=$2 AND result IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            connection_id, str(revision),
        )
        return unpack(row["checkpoint"]) if row else None
