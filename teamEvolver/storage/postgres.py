"""Tenant-bound ObjectStore using the platform pool, with no runtime DDL."""
from __future__ import annotations

import hashlib

from .base import ObjectInfo, _BytesObject, read_bytes


class PostgresObjectStore:
    def __init__(self, tx, tenant_id):
        self.tx, self.tenant_id = tx, tenant_id
        self.table = f"{tx.pool.schema}.storage_objects"

    @staticmethod
    def key(value):
        if not value or value.startswith("/") or "\\" in value or any(
            part in {"", ".", ".."} for part in value.split("/")
        ):
            raise ValueError("invalid storage key")
        return value

    async def get(self, conn, key):
        row = await conn.fetchrow(f"SELECT content,content_hash FROM {self.table} WHERE tenant_id=$1 AND key=$2",
                                  self.tenant_id, self.key(key))
        if row is None:
            raise FileNotFoundError(key)
        raw = bytes(row["content"])
        if hashlib.sha256(raw).hexdigest() != row["content_hash"]:
            raise ValueError("ObjectStore content hash mismatch")
        return raw

    async def put(self, conn, key, data):
        raw = read_bytes(data)
        await conn.execute(
            f"INSERT INTO {self.table}(tenant_id,key,content,content_hash) VALUES($1,$2,$3,$4) "
            "ON CONFLICT(tenant_id,key) DO UPDATE SET content=EXCLUDED.content,"
            "content_hash=EXCLUDED.content_hash,updated_at=now()",
            self.tenant_id, self.key(key), raw, hashlib.sha256(raw).hexdigest(),
        )

    def get_object(self, key):
        raw = self.tx.run_tenant_tx(self.tenant_id, lambda conn: self.get(conn, key))
        return _BytesObject(raw, key)

    def put_object(self, key, data):
        raw = read_bytes(data)
        return self.tx.run_tenant_tx(self.tenant_id, lambda conn: self.put(conn, key, raw))

    def delete_object(self, key):
        return self.tx.run_tenant_tx(self.tenant_id, lambda conn: conn.execute(
            f"DELETE FROM {self.table} WHERE tenant_id=$1 AND key=$2", self.tenant_id, self.key(key)))

    def iter_objects(self, prefix=""):
        async def read(conn):
            return await conn.fetch(f"SELECT key FROM {self.table} WHERE tenant_id=$1 AND starts_with(key,$2) "
                                    "ORDER BY key LIMIT 10000", self.tenant_id, prefix)
        return iter(ObjectInfo(row["key"]) for row in self.tx.run_tenant_tx(self.tenant_id, read))
