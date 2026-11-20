"""Per-tenant MA dispatch, serialized across Worker replicas in PostgreSQL."""

from .commands import enqueue
from .repository import MARepository


async def tick(services, *, tenant_id):
    repo = MARepository(services.tx)
    async with services.tx.tenant_tx(tenant_id) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"{tenant_id}:ma-schedule")
        now = await conn.fetchval("SELECT clock_timestamp()")
        for connection in await repo.connections(conn):
            if not connection["enabled"] or connection["status"] not in {"ready", "error", "drift"}:
                continue
            errors = await repo.operation_errors(conn, connection)
            if "ma.discover" in errors:
                continue
            if connection["status"] != "ready" and not await repo.applied(conn, connection["connection_id"]):
                continue
            ident, config = connection["connection_id"], connection["config"]
            for kind, interval in (("ma.collect", config["poll_interval_seconds"]),
                                   ("ma.reconcile", config["sync_interval_seconds"])):
                if kind in errors and not errors[kind].get("retryable"):
                    continue
                last = await conn.fetchval(
                    f"SELECT max(created_at) FROM {repo.schema}.ma_operations WHERE connection_id=$1 AND kind=$2",
                    ident, kind,
                )
                active = await conn.fetchval(
                    f"SELECT 1 FROM {repo.schema}.worker_tasks WHERE subject_id=$1 AND kind=$2 "
                    "AND status IN ('queued','running','retry_wait','waiting_external','reconciling') LIMIT 1",
                    ident, kind,
                )
                if active or (last and (now - last).total_seconds() < interval):
                    continue
                await enqueue(conn, services, tenant_id=tenant_id, connection_id=ident, kind=kind)
