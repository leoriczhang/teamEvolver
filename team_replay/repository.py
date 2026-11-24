"""Independent PG Replay histories; immutable input and case checkpoints."""

from __future__ import annotations

import json

from teamEvolver.storage.json_snapshots import JsonSnapshots

from ._util import stable_hash
from .datasets.repository import DatasetConflict, DatasetError, DatasetNotFound, lock, safe_id, unpack

DOMAINS = {"skill", "memory", "dataset"}
MAX_REPLAY_INPUT_BYTES = 96 * 1024 * 1024


class ReplayRepository:
    def __init__(self, tx) -> None:
        self.tx = tx
        self.schema = tx.pool.schema

    async def preparation(self, conn, tenant_id, run_id, input_hash):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.replay_preparations WHERE run_id=$1", run_id,
        )
        if row is None:
            return None
        if row["input_hash"] != input_hash:
            raise DatasetError("Replay preparation input hash mismatch")
        result = await JsonSnapshots(self.tx, tenant_id).hydrate(conn, unpack(row["result"]))
        if stable_hash(result) != row["result_hash"]:
            raise DatasetError("Replay preparation result hash mismatch")
        return result

    async def save_preparation(self, conn, *, tenant_id, run_id, input_hash, result):
        packed = await JsonSnapshots(self.tx, tenant_id).pack(conn, result)
        await conn.execute(
            f"INSERT INTO {self.schema}.replay_preparations "
            "(tenant_id,run_id,input_hash,result,result_hash) VALUES($1,$2,$3,$4::jsonb,$5) "
            "ON CONFLICT DO NOTHING",
            tenant_id, run_id, input_hash, json.dumps(packed), stable_hash(result),
        )
        return await self.preparation(conn, tenant_id, run_id, input_hash)

    def table(self, domain, suffix="runs"):
        if domain not in DOMAINS:
            raise DatasetError("invalid Replay domain")
        return f"{self.schema}.{domain}_replay_{suffix}"

    async def get(self, conn, domain, run_id):
        row = await conn.fetchrow(f"SELECT * FROM {self.table(domain)} WHERE run_id=$1", safe_id(run_id))
        if row is None:
            raise DatasetNotFound("Replay run not found")
        result = dict(row)
        result["input"] = await JsonSnapshots(self.tx, result["tenant_id"]).hydrate(
            conn, unpack(result["input"]),
        )
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
        packed = await JsonSnapshots(self.tx, tenant_id).pack(conn, payload)
        raw = json.dumps(packed, ensure_ascii=False)
        if len(raw.encode()) > MAX_REPLAY_INPUT_BYTES:
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

    async def reusable_candidate_cases(self, conn, job):
        """Reuse only matching Candidate revision, materials, scope and execution."""
        from .completion import candidate_complete

        rows = await conn.fetch(
            f"SELECT run_id FROM {self.table('skill')} WHERE subject_id=$1 "
            "ORDER BY created_at DESC LIMIT 30", job["skill_ids"][0],
        )
        reusable = {}
        for row in rows:
            run = await self.get(conn, "skill", row["run_id"])
            previous = run["input"].get("job") or {}
            if job.get("validation_method") == "full_ab" and previous.get("validation_method", "full_ab") != "full_ab":
                continue
            if any(previous.get(k) != job.get(k) for k in (
                "job_id", "candidate_revision", "candidate_skill", "current_skill",
                "replay_config", "replay_adapter", "ma_records",
            )):
                continue
            old_cases = {c["case_id"]: c for c in previous.get("replay_cases") or []}
            matching = {c["case_id"] for c in job.get("replay_cases") or []
                        if old_cases.get(c["case_id"]) == c}
            if not matching:
                continue
            for case_id, result in (await self.cases(conn, "skill", row["run_id"])).items():
                pending = any(p.get("candidate", {}).get("status") == "assessment_pending"
                              for p in result.get("cases") or [])
                if case_id in matching and case_id not in reusable and (candidate_complete(result) or pending):
                    reusable[case_id] = {**result, "reused_from": row["run_id"]}
        return reusable

    async def progress(self, conn, domain, run_id):
        """Count checkpoints without loading full Agent trajectories."""
        row = await conn.fetchrow(
            f"SELECT count(*) AS completed, count(*) FILTER (WHERE "
            "result #>> '{cases,0,baseline,completed}' = 'true' AND "
            "result #>> '{cases,0,candidate,completed}' = 'true') AS succeeded, "
            "count(*) FILTER (WHERE result #>> '{cases,0,candidate,completed}' = 'true') AS candidate_completed "
            f"FROM {self.table(domain, 'cases')} WHERE run_id=$1",
            safe_id(run_id),
        )
        completed, succeeded = int(row["completed"]), int(row["succeeded"])
        return {"completed": completed, "succeeded": succeeded, "failed": completed - succeeded,
                "candidate_completed": int(row["candidate_completed"])}

    async def save_case(self, conn, *, tenant_id, domain, run_id, case, result):
        await conn.execute(
            f"INSERT INTO {self.table(domain, 'cases')} "
            "(tenant_id,run_id,case_id,input_hash,result,result_hash) VALUES($1,$2,$3,$4,$5::jsonb,$6) "
            "ON CONFLICT (tenant_id,run_id,case_id) DO UPDATE SET "
            "result=EXCLUDED.result,result_hash=EXCLUDED.result_hash "
            f"WHERE {self.table(domain, 'cases')}.input_hash=EXCLUDED.input_hash AND "
            f"({self.table(domain, 'cases')}.result #>> '{{cases,0,candidate,status}}'='assessment_pending' OR "
            f"{self.table(domain, 'cases')}.result #>> '{{cases,0,baseline,status}}'='assessment_pending')",
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

    async def summaries(self, conn, domain, *, subject_id="", limit=100):
        """List metadata without hydrating Session inputs or Agent trajectories."""
        rows = await conn.fetch(
            f"SELECT run_id,subject_id,dataset_id,created_at,finished_at,"
            "CASE WHEN pg_column_size(input)<=65536 THEN input #>> '{dataset,document,name}' END AS dataset_name,"
            "CASE WHEN pg_column_size(input)<=65536 THEN COALESCE(input #> '{spec,metadata,skill_versions}',"
            "CASE WHEN input #>> '{job,baseline_ref,revision}' IS NOT NULL THEN "
            "jsonb_build_object(subject_id,input #> '{job,baseline_ref,revision}') END) END AS skill_versions,"
            "CASE WHEN pg_column_size(result)<=65536 THEN "
            "jsonb_build_object('verdict',COALESCE(result->'verdict',result #> '{replay,verdict}'),"
            "'reason',COALESCE(result->'reason',result #> '{replay,reason}'),"
            "'efficiency',COALESCE(result->'efficiency',result #> '{replay,efficiency}')) END AS result_summary "
            f"FROM {self.table(domain)} WHERE ($1='' OR subject_id=$1) "
            "ORDER BY created_at DESC,run_id LIMIT $2",
            subject_id, min(100, max(1, limit)),
        )
        return [{**dict(row), "skill_versions": unpack(row["skill_versions"]) or {},
                 "result_summary": unpack(row["result_summary"])} for row in rows]

    async def count(self, conn, domain, *, dataset_id=""):
        return await conn.fetchval(f"SELECT count(*) FROM {self.table(domain)} WHERE ($1='' OR dataset_id=$1)",
                                   dataset_id)
