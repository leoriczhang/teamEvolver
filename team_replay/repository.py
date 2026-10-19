"""Independent PG Replay histories; immutable input and case checkpoints."""

from __future__ import annotations

import json

from ._util import stable_hash
from .datasets.repository import DatasetConflict, DatasetError, DatasetNotFound, lock, safe_id, unpack

DOMAINS = {"skill", "memory", "dataset"}


class ReplayRepository:
    def __init__(self, tx) -> None:
        self.tx = tx
        self.schema = tx.pool.schema

    def table(self, domain, suffix="runs"):
        if domain not in DOMAINS:
            raise DatasetError("invalid Replay domain")
        return f"{self.schema}.{domain}_replay_{suffix}"

    async def get(self, conn, domain, run_id):
        row = await conn.fetchrow(f"SELECT * FROM {self.table(domain)} WHERE run_id=$1", safe_id(run_id))
        if row is None:
            raise DatasetNotFound("Replay run not found")
        result = dict(row)
        result["input"] = unpack(result["input"])
        result["result"] = unpack(result["result"])
        if stable_hash(result["input"]) != result["input_hash"]:
            raise DatasetError("Replay input hash mismatch")
        return result

    async def accept(self, conn, *, tenant_id, domain, run_id, subject_id, request, payload, dataset_id=""):
        safe_id(run_id)
        await lock(conn, tenant_id, "run:" + run_id)
        existing = await conn.fetchrow(f"SELECT * FROM {self.table(domain)} WHERE run_id=$1", run_id)
        if existing:
            if existing["request_hash"] != stable_hash(request):
                raise DatasetConflict("Idempotency-Key used for another Replay request")
            return await self.get(conn, domain, run_id), False
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode()) > 96 * 1024 * 1024:
            raise DatasetError("Replay input exceeds 96 MiB")
        await conn.execute(
            f"INSERT INTO {self.table(domain)} "
            "(tenant_id,run_id,subject_id,dataset_id,request_hash,input_hash,input) "
            "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
            tenant_id, run_id, subject_id, dataset_id, stable_hash(request), stable_hash(payload), raw,
        )
        return await self.get(conn, domain, run_id), True

    async def cases(self, conn, domain, run_id):
        rows = await conn.fetch(f"SELECT * FROM {self.table(domain, 'cases')} WHERE run_id=$1 ORDER BY case_id",
                                safe_id(run_id))
        results = {}
        for row in rows:
            result = unpack(row["result"])
            if stable_hash(result) != row["result_hash"]:
                raise DatasetError("Replay result hash mismatch")
            results[row["case_id"]] = result
        return results

    async def progress(self, conn, domain, run_id):
        """Count checkpoints without loading full Agent trajectories."""
        row = await conn.fetchrow(
            f"SELECT count(*) AS completed, count(*) FILTER (WHERE "
            "result #>> '{cases,0,baseline,completed}' = 'true' AND "
            "result #>> '{cases,0,candidate,completed}' = 'true') AS succeeded "
            f"FROM {self.table(domain, 'cases')} WHERE run_id=$1",
            safe_id(run_id),
        )
        completed, succeeded = int(row["completed"]), int(row["succeeded"])
        return {"completed": completed, "succeeded": succeeded, "failed": completed - succeeded}

    async def save_case(self, conn, *, tenant_id, domain, run_id, case, result):
        await conn.execute(
            f"INSERT INTO {self.table(domain, 'cases')} "
            "(tenant_id,run_id,case_id,input_hash,result,result_hash) VALUES($1,$2,$3,$4,$5::jsonb,$6) "
            "ON CONFLICT DO NOTHING",
            tenant_id, run_id, safe_id(case["case_id"]), stable_hash(case), json.dumps(result), stable_hash(result),
        )

    async def finish(self, conn, domain, run_id, result):
        await conn.execute(
            f"UPDATE {self.table(domain)} SET result=$2::jsonb,finished_at=now() WHERE run_id=$1 AND result IS NULL",
            run_id, json.dumps(result),
        )

    async def list(self, conn, domain, *, subject_id="", dataset_id="", limit=100, offset=0):
        rows = await conn.fetch(
            f"SELECT run_id FROM {self.table(domain)} WHERE ($1='' OR subject_id=$1) AND ($2='' OR dataset_id=$2) "
            "ORDER BY created_at DESC,run_id LIMIT $3 OFFSET $4",
            subject_id, dataset_id, min(500, max(1, limit)), max(0, offset),
        )
        return [await self.get(conn, domain, row["run_id"]) for row in rows]

    async def count(self, conn, domain, *, dataset_id=""):
        return await conn.fetchval(f"SELECT count(*) FROM {self.table(domain)} WHERE ($1='' OR dataset_id=$1)",
                                   dataset_id)
