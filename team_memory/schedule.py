"""Persistent automatic Team Memory maintenance dispatch."""

from __future__ import annotations

import json
from uuid import uuid4

from .config import load_memory_config
from .runs import accept
from .schemas import run_payload

_ACTIVE = ("queued", "running", "retry_wait", "waiting_external", "reconciling")


async def memory_tick(services, *, tenant_id: str) -> dict:
    """Enqueue one maintenance run when the tenant's automatic policy is due."""
    async with services.tx.tenant_tx(tenant_id) as conn:
        config = await load_memory_config(services.extras["config_repo"], conn)
        maintenance = config.maintenance
        if maintenance.get("trigger_mode") != "automatic":
            return {"state": "manual"}
        interval = max(300, min(2592000, int(maintenance.get("interval_seconds") or 86400)))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{tenant_id}:memory-maintenance-schedule",
        )
        active = await conn.fetchval(
            f"SELECT 1 FROM {services.tx.pool.schema}.worker_tasks t "
            f"JOIN {services.tx.pool.schema}.memory_runs r "
            "ON r.tenant_id=t.tenant_id AND r.run_id=t.task_id "
            "WHERE t.kind IN ('memory.aggregate','memory.compile') AND t.status=ANY($1::text[]) "
            "AND r.input->>'pipeline'='maintenance' LIMIT 1",
            list(_ACTIVE),
        )
        if active:
            return {"state": "active"}
        now = await conn.fetchval("SELECT clock_timestamp()")
        latest = await conn.fetchval(
            f"SELECT max(created_at) FROM {services.tx.pool.schema}.memory_runs "
            "WHERE input->>'pipeline'='maintenance'",
        )
        if latest is not None and (now - latest).total_seconds() < interval:
            return {"state": "waiting"}
        tenant_raw = await conn.fetchval(
            f"SELECT config FROM {services.tx.pool.schema}.tenants WHERE tenant_id=$1",
            tenant_id,
        )
        if isinstance(tenant_raw, str):
            tenant_raw = json.loads(tenant_raw)
        overrides = tenant_raw if isinstance(tenant_raw, dict) else {}
        base = services.extras["config"]
        account = str(overrides.get("sharing_viking_account") or base.sharing_viking_account)
        user = str(overrides.get("sharing_viking_user") or base.sharing_viking_user or "team")
        raw = {"pipeline": "maintenance", "mode": "incremental"}
        payload = run_payload(raw, config, account, user)
        run_id = "memory:auto:" + uuid4().hex
        await accept(conn, services, tenant_id, run_id, raw, payload)
        return {"state": "enqueued", "job_id": run_id}
