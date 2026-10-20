"""Tenant-scoped ingestion, stage checkpoint and formal Session persistence.

Acceptance is composed with payload metadata and Worker task writes by the
caller in a short transaction. Formal Session rows retain both business time
and database ingest time. Queries and writes touch this domain's tables only."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from teamEvolver.database.transaction import TransactionManager


def _loads(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _business_time(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("business_time requires timezone")
        return parsed
    return value


class SessionIngestionRepository:
    """Persistence for ingestion records, stage results and formal Sessions."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    # -- ingestion acceptance -------------------------------------------- #

    async def accept(
        self,
        conn: Any,
        *,
        tenant_id: str,
        ingestion_id: str,
        source: str,
        external_id: str = "",
        user_id: str = "",
        account_id: str = "",
        dedupe_key: str = "",
        task_id: str = "",
        adapter_id: str = "",
        adapter_revision: int = 0,
        config_revision: int = 0,
        payload_ref: str = "",
        payload_hash: str = "",
        business_time: Any = None,
    ) -> str:
        """Insert an acceptance record; dedupe on a live ``dedupe_key``.

        Returns the ``ingestion_id`` of the new record, or of the existing one
        when ``dedupe_key`` already maps to an acceptance. The caller composes
        the dedupe lookup, raw-payload artifact and ``worker_tasks`` row in one
        short transaction.
        """
        if dedupe_key:
            await conn.execute("-- te:op=ingest_lock\nSELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                               f"session:{tenant_id}:{dedupe_key}")
            existing = await conn.fetchrow(
                f"-- te:op=ingest_dedupe_lookup\n"
                f"SELECT ingestion_id, task_id FROM {self._schema}.ingestions "
                "WHERE dedupe_key = $1 LIMIT 1",
                dedupe_key,
            )
            if existing is not None:
                return str(existing["ingestion_id"])
        await conn.execute(
            f"-- te:op=ingest_accept\n"
            f"INSERT INTO {self._schema}.ingestions "
            "(tenant_id, ingestion_id, source, external_id, user_id, account_id, "
            " dedupe_key, task_id, adapter_id, adapter_revision, config_revision, "
            " payload_ref, payload_hash, status, business_time) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, "
            " 'accepted', $14)",
            tenant_id,
            ingestion_id,
            source,
            external_id,
            user_id,
            account_id,
            dedupe_key,
            task_id,
            adapter_id,
            int(adapter_revision),
            int(config_revision),
            payload_ref,
            payload_hash,
            _business_time(business_time),
        )
        return ingestion_id

    async def set_status(self, conn: Any, *, ingestion_id: str, status: str) -> bool:
        updated = await conn.fetchval(
            f"-- te:op=ingest_set_status\n"
            f"UPDATE {self._schema}.ingestions SET status = $2, updated_at = now() "
            "WHERE ingestion_id = $1 RETURNING ingestion_id",
            ingestion_id,
            status,
        )
        return updated is not None

    async def get_ingestion(self, conn: Any, *, ingestion_id: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"-- te:op=ingest_get\n"
            f"SELECT tenant_id, ingestion_id, source, external_id, user_id, account_id, "
            "  dedupe_key, task_id, adapter_id, adapter_revision, config_revision, "
            "  payload_ref, payload_hash, status, business_time, received_at, updated_at "
            f"FROM {self._schema}.ingestions WHERE ingestion_id = $1",
            ingestion_id,
        )
        return dict(row) if row is not None else None

    # -- stage results ---------------------------------------------------- #

    async def record_stage(
        self,
        conn: Any,
        *,
        tenant_id: str,
        ingestion_id: str,
        stage: str,
        status: str,
        result_ref: str = "",
        detail: dict[str, Any] | None = None,
        error_code: str = "",
    ) -> None:
        """Upsert one stage result row (idempotent per (ingestion, stage)).

        Upsert (not insert) so a retried attempt that re-runs a stage overwrites
        the prior row rather than violating the PK — the worker fence guards the
        task itself, this keeps the per-stage ledger single-valued.
        """
        await conn.execute(
            f"-- te:op=stage_record\n"
            f"INSERT INTO {self._schema}.session_stage_results "
            "(tenant_id, ingestion_id, stage, status, result_ref, detail, error_code) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) "
            "ON CONFLICT (tenant_id, ingestion_id, stage) DO UPDATE SET "
            "  status = EXCLUDED.status, result_ref = EXCLUDED.result_ref, "
            "  detail = EXCLUDED.detail, error_code = EXCLUDED.error_code, "
            "  updated_at = now()",
            tenant_id,
            ingestion_id,
            stage,
            status,
            result_ref,
            json.dumps(detail or {}),
            error_code,
        )

    async def stage_results(self, conn: Any, *, ingestion_id: str) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            f"-- te:op=stage_list\n"
            f"SELECT stage, status, result_ref, detail, error_code, created_at, updated_at "
            f"FROM {self._schema}.session_stage_results WHERE ingestion_id = $1 "
            "ORDER BY created_at",
            ingestion_id,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = _loads(item.get("detail"))
            out.append(item)
        return out

    async def succeeded_stages(self, conn: Any, *, ingestion_id: str) -> set[str]:
        rows = await conn.fetch(
            f"-- te:op=stage_succeeded\n"
            f"SELECT stage FROM {self._schema}.session_stage_results "
            "WHERE ingestion_id = $1 AND status = 'succeeded'",
            ingestion_id,
        )
        return {str(r["stage"]) for r in rows}

    # -- formal session (written only after mandatory stages succeed) ----- #

    async def write_session(
        self,
        conn: Any,
        *,
        tenant_id: str,
        session_id: str,
        ingestion_id: str = "",
        source: str = "",
        external_id: str = "",
        user_id: str = "",
        account_id: str = "",
        decision: str = "",
        content_ref: str = "",
        content_hash: str = "",
        judge: dict[str, Any] | None = None,
        filter_audit: dict[str, Any] | None = None,
        consumption: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        business_time: Any = None,
    ) -> None:
        """Insert/replace the formal Session.

        Carries both ``business_time`` (upstream occurrence time) and
        ``ingested_at`` (set by the DB clock) — plan "同时保存业务
        发生时间和入库时间".
        """
        await conn.execute(
            f"-- te:op=session_write\n"
            f"INSERT INTO {self._schema}.sessions "
            "(tenant_id, session_id, ingestion_id, source, external_id, user_id, "
            " account_id, decision, content_ref, content_hash, judge, filter_audit, "
            " consumption, meta, business_time) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb, "
            " $13::jsonb, $14::jsonb, $15) "
            "ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            "  ingestion_id = EXCLUDED.ingestion_id, source = EXCLUDED.source, "
            "  decision = EXCLUDED.decision, content_ref = EXCLUDED.content_ref, "
            "  content_hash = EXCLUDED.content_hash, judge = EXCLUDED.judge, "
            "  filter_audit = EXCLUDED.filter_audit, consumption = EXCLUDED.consumption, "
            "  meta = EXCLUDED.meta, business_time = EXCLUDED.business_time, "
            "  ingested_at = now()",
            tenant_id,
            session_id,
            ingestion_id,
            source,
            external_id,
            user_id,
            account_id,
            decision,
            content_ref,
            content_hash,
            json.dumps(judge or {}),
            json.dumps(filter_audit or {}),
            json.dumps(consumption or {}),
            json.dumps(meta or {}),
            _business_time(business_time),
        )
