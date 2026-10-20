"""Memory run input, resumable stages and incremental compilation checkpoints."""

from __future__ import annotations

import json

from teamEvolver.context.repository import stable_hash


class MemoryRepository:
    def __init__(self, tx):
        self.tx, self.schema = tx, tx.pool.schema

    async def accept(self, conn, tenant, run_id, request, payload):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", tenant + ":memory:" + run_id)
        row = await conn.fetchrow(f"SELECT * FROM {self.schema}.memory_runs WHERE run_id=$1", run_id)
        if row:
            if row["request_hash"] != stable_hash(request):
                raise ValueError("Idempotency-Key reused with different request")
            return await self.get(conn, run_id), False
        await conn.execute(
            f"INSERT INTO {self.schema}.memory_runs"
            "(tenant_id,run_id,target_uri,request_hash,input,input_hash) VALUES($1,$2,$3,$4,$5::jsonb,$6)",
            tenant, run_id, payload["target_uri"], stable_hash(request), json.dumps(payload), stable_hash(payload),
        )
        return await self.get(conn, run_id), True

    async def get(self, conn, run_id):
        row = await conn.fetchrow(f"SELECT * FROM {self.schema}.memory_runs WHERE run_id=$1", run_id)
        if not row:
            raise KeyError("Memory run not found")
        result = dict(row)
        result["input"] = json.loads(row["input"])
        result["result"] = json.loads(row["result"]) if row["result"] is not None else None
        if stable_hash(result["input"]) != result["input_hash"]:
            raise ValueError("Memory input hash mismatch")
        return result

    async def list(self, conn):
        return [await self.get(conn, row["run_id"]) for row in await conn.fetch(
            f"SELECT run_id FROM {self.schema}.memory_runs ORDER BY created_at DESC LIMIT 100")]

    async def stage(self, conn, run_id, key):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.memory_stages WHERE run_id=$1 AND stage_key=$2", run_id, key)
        return {**dict(row), "result": json.loads(row["result"])} if row else None

    async def save_stage(self, conn, task, key, payload, *, status, upstream="", result=None, result_ref=""):
        await conn.execute(
            f"INSERT INTO {self.schema}.memory_stages"
            "(tenant_id,run_id,stage_key,input_hash,status,upstream_task_id,result,result_ref) "
            "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8) ON CONFLICT(tenant_id,run_id,stage_key) DO UPDATE "
            "SET status=EXCLUDED.status,upstream_task_id=EXCLUDED.upstream_task_id,"
            "result=EXCLUDED.result,result_ref=EXCLUDED.result_ref,updated_at=now()",
            task.tenant_id, task.task_id, key, stable_hash(payload), status, upstream, json.dumps(result or {}),
            result_ref,
        )

    async def checkpoint(self, conn, account, target, key):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.memory_checkpoints WHERE account_id=$1 AND target_uri=$2 AND group_key=$3",
            account, target, key,
        )
        return dict(row) if row else None

    async def save_checkpoint(self, conn, task, account, target, key, source_hash, skill, ref):
        await conn.execute(
            f"INSERT INTO {self.schema}.memory_checkpoints"
            "(tenant_id,account_id,target_uri,group_key,source_hash,skill_revision,result_ref) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(tenant_id,account_id,target_uri,group_key) "
            "DO UPDATE SET source_hash=EXCLUDED.source_hash,skill_revision=EXCLUDED.skill_revision,"
            "result_ref=EXCLUDED.result_ref,updated_at=now()",
            task.tenant_id, account, target, key, source_hash, skill, ref,
        )

    async def finish(self, conn, task, payload, result, before_ref=""):
        await conn.execute(
            f"UPDATE {self.schema}.memory_runs SET result=$2::jsonb,finished_at=now() "
            "WHERE run_id=$1 AND result IS NULL",
            task.task_id, json.dumps(result),
        )
        await conn.execute(
            f"INSERT INTO {self.schema}.memory_changes"
            "(tenant_id,change_id,run_id,target_uri,before_ref,after_ref,detail) VALUES($1,$2,$2,$3,$4,$5,$6::jsonb) "
            "ON CONFLICT DO NOTHING", task.tenant_id, task.task_id, payload["target_uri"], before_ref,
            result.get("after_ref", ""), json.dumps(result),
        )

    async def changes(self, conn, limit=100):
        rows = await conn.fetch(
            f"SELECT * FROM {self.schema}.memory_changes ORDER BY created_at DESC LIMIT $1", limit)
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]
