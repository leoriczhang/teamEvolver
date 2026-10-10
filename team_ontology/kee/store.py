"""RLS-scoped JSON in the existing objects table. No parallel task or approval registry."""

import json
from contextlib import asynccontextmanager

from fastapi import HTTPException

from .contracts import canonical, digest


class Structures:
    def __init__(self, control):
        self.control = control
        self.table = f'"{control.schema}".objects'

    @asynccontextmanager
    async def transaction(self, tenant, lock=""):
        async with self.control.pool.acquire() as c, c.transaction():
            await c.execute("SELECT set_config('app.tenant_id',$1,true)", tenant)
            if lock:
                await c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", tenant + ":kee:" + lock)
            yield c

    async def get(self, tenant, key, *, connection=None, default=None):
        if connection is None:
            async with self.transaction(tenant) as c:
                return await self.get(tenant, key, connection=c, default=default)
        raw = await connection.fetchval(
            f"SELECT content FROM {self.table} WHERE tenant_id=$1 AND key=$2", tenant, "kee/r6/" + key
        )
        return json.loads(bytes(raw)) if raw is not None else default

    async def put(self, tenant, key, value, *, connection=None, immutable=False):
        if len(canonical(value).encode()) > 8 * 1024 * 1024:
            raise HTTPException(413, "STRUCTURE_SIZE_LIMIT")
        if connection is None:
            async with self.transaction(tenant, key) as c:
                return await self.put(tenant, key, value, connection=c, immutable=immutable)
        previous = await self.get(tenant, key, connection=connection)
        if immutable and previous is not None and digest(previous) != digest(value):
            raise HTTPException(409, "IMMUTABLE_VERSION_CONFLICT")
        await connection.execute(
            f"INSERT INTO {self.table}(tenant_id,key,content) VALUES($1,$2,$3) "
            "ON CONFLICT(tenant_id,key) DO UPDATE SET content=EXCLUDED.content,updated_at=now()",
            tenant,
            "kee/r6/" + key,
            canonical(value).encode(),
        )
        return value

    async def list(self, tenant, prefix, limit=100, after=""):
        async with self.transaction(tenant) as c:
            rows = await c.fetch(
                f"SELECT key,content FROM {self.table} WHERE tenant_id=$1 "
                "AND starts_with(key,$2) AND key>$3 ORDER BY key LIMIT $4",
                tenant,
                "kee/r6/" + prefix,
                "kee/r6/" + after,
                min(100, limit),
            )
            return [{"key": r["key"][7:], "value": json.loads(bytes(r["content"]))} for r in rows]
