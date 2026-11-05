"""Tenant-isolated Miner receipts, watermarks and indexes in ObjectStore."""

from __future__ import annotations

import hashlib
import json

from teamEvolver.storage.postgres import PostgresObjectStore


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MinerState:
    def __init__(self, tx, tenant):
        self.tx, self.tenant = tx, tenant
        self.store = PostgresObjectStore(tx, tenant)

    def key(self, kind, identity):
        return "miner/" + kind + "/" + digest(identity) + ".json"

    async def get(self, conn, kind, identity):
        try:
            return json.loads(await self.store.get(conn, self.key(kind, identity)))
        except FileNotFoundError:
            return None

    async def put(self, conn, kind, identity, value):
        await self.store.put(conn, self.key(kind, identity), json.dumps(value, ensure_ascii=False))

    async def lock(self, conn, identity):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           self.tenant + ":miner:" + digest(identity))

    @staticmethod
    def scope(settings):
        return [settings[k] for k in ("account_id", "source_uri", "target_uri", "skill_uri")]

    async def finalize(self, conn, receipt, status):
        scope = self.scope(receipt["settings"])
        await self.lock(conn, scope)
        prior = await self.get(conn, "receipt", receipt["job_id"])
        if prior and prior.get("finalized"):
            return False
        receipt = {**receipt, "status": status, "finalized": True}
        if status == "succeeded":
            watermark = await self.get(conn, "watermark", scope) or {}
            if str(watermark.get("last_compile_time", "")) < receipt["started_at"]:
                await self.put(conn, "watermark", scope,
                               {"last_compile_time": receipt["started_at"], "task_id": receipt["upstream_task_id"]})
        await self.put(conn, "receipt", receipt["job_id"], receipt)
        await self.put(conn, "upstream", [receipt["settings"]["account_id"], receipt["upstream_task_id"]], receipt)
        return True
