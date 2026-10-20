"""Tenant scoped Replay adapter definitions, versions and bindings in PG."""
from __future__ import annotations

import hashlib

from ..datasets.repository import DatasetConflict, DatasetNotFound, lock
from .loader import validate_content


class ReplayAdapterRepository:
    def __init__(self, tx):
        self.schema = tx.pool.schema

    async def source(self, conn, filename, revision=None):
        row = await conn.fetchrow(
            f"SELECT v.* FROM {self.schema}.adapter_versions v JOIN {self.schema}.adapters a "
            "ON a.tenant_id=v.tenant_id AND a.adapter_id=v.adapter_id "
            "WHERE a.kind='replay' AND a.adapter_id=$1 AND ($2::bigint IS NULL OR v.revision=$2) "
            "ORDER BY v.revision DESC LIMIT 1", filename, revision,
        )
        if row is None:
            raise DatasetNotFound("Replay adapter not found")
        return dict(row)

    async def save(self, conn, *, tenant_id, filename, source, expected_revision):
        meta = validate_content(source, filename)
        await lock(conn, tenant_id, "adapter:" + filename)
        row = await conn.fetchrow(
            f"SELECT revision FROM {self.schema}.adapter_versions WHERE adapter_id=$1 "
            "ORDER BY revision DESC LIMIT 1", filename,
        )
        current = row["revision"] if row else 0
        if (expected_revision or 0) != current:
            raise DatasetConflict("Replay adapter revision changed")
        await conn.execute(
            f"INSERT INTO {self.schema}.adapters(tenant_id,adapter_id,kind) VALUES($1,$2,'replay') "
            "ON CONFLICT DO NOTHING", tenant_id, filename,
        )
        await conn.execute(
            f"INSERT INTO {self.schema}.adapter_versions(tenant_id,adapter_id,revision,source_hash,source) "
            "VALUES($1,$2,$3,$4,$5)", tenant_id, filename, current + 1,
            hashlib.sha256(source.encode()).hexdigest(), source,
        )
        return {"file": filename, "revision": current + 1, **meta}

    async def binding(self, conn):
        row = await conn.fetchrow(
            f"SELECT adapter_id,revision FROM {self.schema}.adapter_bindings WHERE binding_key='replay'",
        )
        return await self.source(conn, row["adapter_id"], row["revision"]) if row else None

    async def bind(self, conn, *, tenant_id, filename, revision=None):
        if not filename:
            await conn.execute(f"DELETE FROM {self.schema}.adapter_bindings WHERE binding_key='replay'")
            return None
        source = await self.source(conn, filename, revision)
        if not validate_content(source["source"], filename)["enabled"]:
            raise ValueError("Replay adapter is disabled")
        await conn.execute(
            f"INSERT INTO {self.schema}.adapter_bindings(tenant_id,binding_key,adapter_id,revision) "
            "VALUES($1,'replay',$2,$3) ON CONFLICT(tenant_id,binding_key) DO UPDATE "
            "SET adapter_id=EXCLUDED.adapter_id,revision=EXCLUDED.revision,updated_at=now()",
            tenant_id, filename, source["revision"],
        )
        return source

    async def list(self, conn):
        rows = await conn.fetch(
            f"SELECT adapter_id FROM {self.schema}.adapters WHERE kind='replay' ORDER BY adapter_id",
        )
        result = []
        for row in rows:
            source = await self.source(conn, row["adapter_id"])
            result.append({"file": source["adapter_id"], "revision": source["revision"],
                           **validate_content(source["source"], source["adapter_id"])})
        return result
