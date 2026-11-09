"""Source revisions and stage-cache records owned by Session Ingestion."""
from __future__ import annotations

import json

from .source_revisions import digest, plan_snapshot


class SourceRevisionRepository:
    def __init__(self, tx):
        self.schema = tx.pool.schema

    async def prepare(self, conn, *, tenant_id: str, external_id: str, session: dict):
        initial = plan_snapshot(session, tenant_id=tenant_id, external_id=external_id)
        p = initial["provenance"]
        if not p.get("identity_verified"):
            return initial, None, False
        key = p["source_session_key"]
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"{tenant_id}:{key}")
        prior = await conn.fetchval(
            f"SELECT document FROM {self.schema}.session_source_heads WHERE source_session_key=$1", key,
        )
        prior = json.loads(prior) if isinstance(prior, str) else prior
        planned = plan_snapshot(session, tenant_id=tenant_id, external_id=external_id, previous=prior)
        revision = planned["provenance"]["snapshot_revision"]
        existing = await conn.fetchval(
            f"SELECT ingestion_id FROM {self.schema}.session_source_snapshots "
            "WHERE source_session_key=$1 AND snapshot_revision=$2", key, revision,
        )
        identifier = "source-" + digest([key, revision])
        return planned, existing or identifier, bool(existing)

    async def remember(self, conn, *, tenant_id: str, session: dict, ingestion_id: str):
        p = session["provenance"]
        await conn.execute(
            f"INSERT INTO {self.schema}.session_source_snapshots "
            "(tenant_id,source_session_key,snapshot_revision,ingestion_id) VALUES($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING", tenant_id, p["source_session_key"], p["snapshot_revision"], ingestion_id,
        )
        await conn.execute(
            f"INSERT INTO {self.schema}.session_source_heads "
            "(tenant_id,source_session_key,snapshot_revision,document) VALUES($1,$2,$3,$4::jsonb) "
            "ON CONFLICT(tenant_id,source_session_key) DO UPDATE SET "
            "snapshot_revision=EXCLUDED.snapshot_revision,document=EXCLUDED.document,updated_at=now()",
            tenant_id, p["source_session_key"], p["snapshot_revision"], json.dumps(session, ensure_ascii=False),
        )

    async def cached(self, conn, key):
        return await conn.fetchval(
            f"SELECT result_ref FROM {self.schema}.session_analysis_cache WHERE cache_key=$1", key,
        )

    async def cache(self, conn, *, tenant_id, key, result_ref):
        await conn.execute(
            f"INSERT INTO {self.schema}.session_analysis_cache(tenant_id,cache_key,result_ref) VALUES($1,$2,$3) "
            "ON CONFLICT DO NOTHING", tenant_id, key, result_ref,
        )
