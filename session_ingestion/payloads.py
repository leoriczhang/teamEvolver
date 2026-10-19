"""Immutable Session payloads in tenant-scoped PostgreSQL ObjectStore."""
from __future__ import annotations

import hashlib
import json

from teamEvolver.storage.postgres import PostgresObjectStore
from teamEvolver.tenants.registry import current_tenant_id

from .schemas import _json_default, session_payload_hash


class SessionPayloads:
    def __init__(self, tx):
        self.tx = tx

    def _store(self):
        return PostgresObjectStore(self.tx, current_tenant_id())

    def put_payload_json(self, ref, payload_json, session):
        self._put(ref, {"payload": payload_json, "session": session})

    def get_payload_json(self, ref):
        return self._read(ref)["payload"]

    def get_session(self, ref):
        value = self._read(ref)
        return value["session"]

    def put_session(self, ingestion_id, session, *, stage):
        digest = session_payload_hash(session)
        identity = hashlib.sha256(ingestion_id.encode()).hexdigest()
        ref = f"session/{identity}/{stage}/{digest}.json"
        self._put(ref, {"session": session})
        return ref, digest

    def _read(self, ref):
        return json.loads(self._store().get_object(ref).read())

    def _put(self, ref, document):
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, default=_json_default).encode()
        store = self._store()
        def put(conn):
            return self._put_once(conn, store, ref, raw)
        self.tx.run_tenant_tx(store.tenant_id, put)

    async def _put_once(self, conn, store, ref, raw):
        row = await conn.fetchrow(
            f"INSERT INTO {store.table}(tenant_id,key,content,content_hash) VALUES($1,$2,$3,$4) "
            "ON CONFLICT(tenant_id,key) DO UPDATE SET key=EXCLUDED.key RETURNING content",
            store.tenant_id, store.key(ref), raw, hashlib.sha256(raw).hexdigest(),
        )
        if bytes(row["content"]) != raw:
            raise ValueError("Session payload is immutable")
