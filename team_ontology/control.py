"""TE authoritative jobs/outbox/audit; independent from the OV asset database."""

import json
import logging
import secrets

import asyncpg
from fastapi import HTTPException

from teamEvolver.logging_runtime import event, safe_code
from teamEvolver.storage.pg_pool import validate_schema_name

from .diagnostics import diagnostic


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def row_value(row):
    if row is None:
        return None
    result = dict(row)
    for name in ("request", "result"):
        result[name] = json.loads(result[name]) if isinstance(result.get(name), str) else result.get(name)
    return result


class ControlStore:
    def __init__(self, dsn, queue_limit=32, *, schema="teamevolver"):
        if not dsn:
            raise ValueError("Ontology requires TE PostgreSQL; no production file fallback")
        self.schema = validate_schema_name(schema)
        if len(self.schema.encode("utf-8")) > 63:
            raise ValueError("PostgreSQL schema name must not exceed 63 bytes")
        self.jobs = f'"{self.schema}".ontology_jobs'
        self.outbox = f'"{self.schema}".ontology_outbox'
        self.audit_table = f'"{self.schema}".ontology_audit'
        self.migrations = f'"{self.schema}".ontology_migrations'
        self.dsn, self.limit, self.pool = dsn, queue_limit, None

    async def start(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4, command_timeout=10)
        try:
            async with self.pool.acquire() as c, c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(730211845)")
                # Managed PG roles may create tables in a DBA-provisioned schema,
                # but have no database-level CREATE privilege. Do not issue CREATE
                # SCHEMA (even IF NOT EXISTS) when the namespace already exists.
                exists = await c.fetchval("SELECT 1 FROM pg_namespace WHERE nspname=$1", self.schema)
                if not exists:
                    try:
                        await c.execute(f'CREATE SCHEMA "{self.schema}"')
                    except asyncpg.InsufficientPrivilegeError as exc:
                        raise RuntimeError(
                            f"Ontology schema {self.schema!r} does not exist; configure storage_pg.schema "
                            "to an existing schema with USAGE and CREATE, or ask the DBA to provision it"
                        ) from exc
                await self._migrate_legacy(c)
                await c.execute(f"""CREATE TABLE IF NOT EXISTS {self.migrations}(
                  id text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
                INSERT INTO {self.migrations}(id) VALUES('v5-001'),('v5-002-configurable-schema')
                  ON CONFLICT DO NOTHING;
                CREATE TABLE IF NOT EXISTS {self.jobs}(
                  id text PRIMARY KEY, tenant text NOT NULL, subject text NOT NULL, key text NOT NULL,
                  request jsonb NOT NULL, result jsonb NOT NULL DEFAULT '{{}}', state text NOT NULL DEFAULT 'queued',
                  attempt integer NOT NULL DEFAULT 0, lease_until timestamptz, error text,
                  updated timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant,subject,key));
                CREATE TABLE IF NOT EXISTS {self.outbox}(
                  job_id text PRIMARY KEY REFERENCES {self.jobs}(id), pending boolean NOT NULL DEFAULT true);
                CREATE TABLE IF NOT EXISTS {self.audit_table}(
                  id bigserial PRIMARY KEY, tenant text NOT NULL, subject text NOT NULL, event text NOT NULL,
                  body jsonb NOT NULL, created timestamptz NOT NULL DEFAULT now());""")
        except BaseException:
            await self.close()
            raise

    async def _migrate_legacy(self, conn):
        # Preserve task IDs, receipts, audit rows, foreign keys and serial sequences.
        # Stop all old workers before upgrade; the startup lock only coordinates
        # new instances, not an old worker that still uses hard-coded table names.
        names = ["migrations", "jobs", "outbox", "audit"]
        old = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='te_ontology' AND tablename=ANY($1::text[])",
            names,
        )
        if not old:
            return
        if {r["tablename"] for r in old} != set(names):
            raise RuntimeError("Incomplete legacy te_ontology tables; manual migration required")
        target = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname=$1 AND tablename=ANY($2::text[]))",
            self.schema,
            ["ontology_" + name for name in names],
        )
        if target:
            raise RuntimeError("Legacy and target Ontology tables both exist; refusing to overwrite or merge history")
        if not await conn.fetchval("SELECT 1 FROM te_ontology.migrations WHERE id='v5-001'"):
            raise RuntimeError("Unrecognized legacy te_ontology schema; manual migration required")
        for name in names:
            await conn.execute(f"ALTER TABLE te_ontology.{name} RENAME TO ontology_{name}")
            if self.schema != "te_ontology":
                await conn.execute(f'ALTER TABLE te_ontology.ontology_{name} SET SCHEMA "{self.schema}"')
        await conn.execute(
            f"INSERT INTO {self.migrations}(id) VALUES('v5-002-configurable-schema') ON CONFLICT DO NOTHING"
        )

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    @diagnostic("store.audit")
    async def audit(self, principal, event, body, conn=None):
        sql = f"INSERT INTO {self.audit_table}(tenant,subject,event,body) VALUES($1,$2,$3,$4::jsonb)"
        args = (principal["tenant"], principal["subject"], event, canonical(body))
        if conn:
            await conn.execute(sql, *args)
        else:
            async with self.pool.acquire() as c:
                await c.execute(sql, *args)

    @diagnostic("store.submit")
    async def submit(self, principal, key, body, *, imported=None):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", principal["tenant"])
            old = await c.fetchrow(
                f"SELECT * FROM {self.jobs} WHERE tenant=$1 AND subject=$2 AND key=$3",
                principal["tenant"],
                principal["subject"],
                key,
            )
            if old:
                old = row_value(old)
                if canonical(old["request"]) != canonical(body):
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                return old
            active = await c.fetchval(
                f"SELECT count(*) FROM {self.jobs} WHERE tenant=$1 "
                "AND state IN ('queued','running','cancelling','compile_unknown')",
                principal["tenant"],
            )
            if active >= self.limit:
                raise HTTPException(429, "ONTOLOGY_QUEUE_FULL")
            identifier = "ont_" + secrets.token_hex(12)
            row = await c.fetchrow(
                f"INSERT INTO {self.jobs}(id,tenant,subject,key,request) VALUES($1,$2,$3,$4,$5::jsonb) RETURNING *",
                identifier,
                principal["tenant"],
                principal["subject"],
                key,
                canonical(body),
            )
            await c.execute(f"INSERT INTO {self.outbox}(job_id) VALUES($1)", identifier)
            if imported is not None:
                row = await c.fetchrow(
                    f"UPDATE {self.jobs} SET state='imported',result=$2::jsonb WHERE id=$1 RETURNING *",
                    identifier,
                    canonical(imported),
                )
                await c.execute(f"UPDATE {self.outbox} SET pending=false WHERE job_id=$1", identifier)
            await self.audit(principal, "job.accepted", {"job_id": identifier}, c)
            return row_value(row)

    async def get(self, principal, job_id):
        row = await self.pool.fetchrow(
            f"SELECT * FROM {self.jobs} WHERE tenant=$1 AND subject=$2 AND id=$3",
            principal["tenant"],
            principal["subject"],
            job_id,
        )
        if not row:
            raise HTTPException(404, "JOB_NOT_FOUND")
        return row_value(row)

    async def list(self, principal):
        return [
            row_value(r)
            for r in await self.pool.fetch(
                f"SELECT * FROM {self.jobs} WHERE tenant=$1 AND subject=$2 ORDER BY updated DESC LIMIT 100",
                principal["tenant"],
                principal["subject"],
            )
        ]

    async def claim(self):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(730211846)")
            if await c.fetchval(
                f"SELECT EXISTS(SELECT 1 FROM {self.jobs} WHERE state='running' AND lease_until>now())"
            ):
                return None
            row = await c.fetchrow(
                f"SELECT j.* FROM {self.jobs} j JOIN {self.outbox} o ON o.job_id=j.id "
                "WHERE o.pending AND ((j.state='queued' AND (j.updated<now()-interval '1 seconds' "
                "OR COALESCE(j.request->>'contract','') NOT LIKE 'sf.te.ontology.%')) "
                "OR (j.state='running' AND j.lease_until<now())) "
                "ORDER BY j.updated FOR UPDATE OF j SKIP LOCKED LIMIT 1"
            )
            if not row:
                return None
            if row["attempt"] >= 3 and not row_value(row)["request"].get("contract", "").startswith("sf.te.ontology."):
                await c.execute(f"UPDATE {self.jobs} SET state='failed',error='RETRY_EXHAUSTED' WHERE id=$1", row["id"])
                await c.execute(f"UPDATE {self.outbox} SET pending=false WHERE job_id=$1", row["id"])
                return None
            row = await c.fetchrow(
                f"UPDATE {self.jobs} SET state='running',attempt=attempt+1, "
                "lease_until=now()+interval '660 seconds',updated=now() WHERE id=$1 RETURNING *",
                row["id"],
            )
            return row_value(row)

    async def checkpoint(self, job, result, state="queued", error=None):
        """Fence every resumable step against cancellation and expired workers."""
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                f"UPDATE {self.jobs} SET result=$4::jsonb,state=$5,error=$6,updated=now() "
                "WHERE id=$1 AND tenant=$2 AND attempt=$3 AND state='running' RETURNING *",
                job["id"],
                job["tenant"],
                job["attempt"],
                canonical(result),
                state,
                error,
            )
            if not row:
                raise HTTPException(409, "JOB_STATE_CHANGED")
            await c.execute(f"UPDATE {self.outbox} SET pending=$2 WHERE job_id=$1", job["id"], state == "queued")
            return row_value(row)

    async def resume(self, principal, job_id, allowed, result):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", principal["tenant"])
            active = await c.fetchval(
                f"SELECT count(*) FROM {self.jobs} WHERE tenant=$1 AND id<>$2 "
                "AND state IN ('queued','running','cancelling','compile_unknown')",
                principal["tenant"],
                job_id,
            )
            if active >= self.limit:
                raise HTTPException(429, "ONTOLOGY_QUEUE_FULL")
            row = await c.fetchrow(
                f"UPDATE {self.jobs} SET state='queued',result=$5::jsonb,error=NULL,updated=now() "
                "WHERE tenant=$1 AND subject=$2 AND id=$3 AND state=ANY($4::text[]) RETURNING *",
                principal["tenant"],
                principal["subject"],
                job_id,
                allowed,
                canonical(result),
            )
            if not row:
                raise HTTPException(409, "JOB_STATE_CHANGED")
            await c.execute(f"UPDATE {self.outbox} SET pending=true WHERE job_id=$1", job_id)
            return row_value(row)

    @diagnostic("store.finish")
    async def finish(self, job, result=None, error=None, retry=False):
        retry = retry and job["attempt"] < 3
        async with self.pool.acquire() as c, c.transaction():
            changed = await c.fetchval(
                f"UPDATE {self.jobs} SET state=$3,result=$4::jsonb,error=$5,updated=now() "
                "WHERE id=$1 AND attempt=$2 AND state='running' RETURNING id",
                job["id"],
                job["attempt"],
                ("queued" if retry else "failed") if error else "review_ready",
                canonical(result or {}),
                error,
            )
            if changed and not retry:
                await c.execute(f"UPDATE {self.outbox} SET pending=false WHERE job_id=$1", job["id"])
        event(
            logging.getLogger(__name__),
            "ontology.attempt_finished",
            job_id=job["id"],
            attempt=job["attempt"],
            tenant=job["tenant"],
            user=job["subject"],
            changed=bool(changed),
            retry=retry,
            state=(("queued" if retry else "failed") if error else "review_ready") if changed else "fenced",
            code=safe_code(error, ""),
        )
        return bool(changed)

    @diagnostic("store.change")
    async def change(self, principal, job_id, allowed, state, result=None):
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                f"UPDATE {self.jobs} SET state=$5,result=COALESCE($6::jsonb,result),updated=now() "
                "WHERE tenant=$1 AND subject=$2 AND id=$3 AND state=ANY($4::text[]) RETURNING *",
                principal["tenant"],
                principal["subject"],
                job_id,
                allowed,
                state,
                canonical(result) if result is not None else None,
            )
            if not row:
                raise HTTPException(409, "JOB_STATE_CHANGED")
            if state in {"cancelling", "cancelled"}:
                await c.execute(f"UPDATE {self.outbox} SET pending=false WHERE job_id=$1", job_id)
            await self.audit(
                principal,
                "job." + state,
                {
                    "job_id": job_id,
                    **(
                        {
                            k: result[k]
                            for k in (
                                "artifact",
                                "approval",
                                "receipt",
                                "commit_key",
                                "migration",
                                "coverage_acknowledgement",
                            )
                            if k in result
                        }
                        if result
                        else {}
                    ),
                },
                c,
            )
            return row_value(row)

    async def recoverable(self):
        # Remote operations have 30-second I/O deadlines. Leave live requests alone.
        return [
            row_value(r)
            for r in await self.pool.fetch(
                f"SELECT * FROM {self.jobs} WHERE (state=ANY($1::text[]) "
                "OR (state='approved' AND (result ? 'grant' "
                "OR NOT COALESCE(result->'approval' ? 'approval_id', false)))) "
                "AND updated<now()-interval '120 seconds' ORDER BY updated LIMIT 8",
                ["editing", "preparing", "publishing", "commit_unknown", "cancelling"],
            )
        ]
