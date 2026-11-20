"""Persistent daily pull acceptance; executed by the shared Worker lifecycle."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters.repository import AdapterRepository
from .config import load_session_config
from .pull_runs import PullRunRepository
from .repository import SessionIngestionRepository
from .schemas import IngestionTaskPayload, session_payload_hash

logger = logging.getLogger(__name__)


def validate_schedule(raw):
    if (not isinstance(raw, dict) or set(raw) != {"enabled", "time", "timezone", "window"}
            or type(raw["enabled"]) is not bool):
        raise ValueError("expected enabled, time, timezone and window")
    if any(not isinstance(raw[key], str) or not raw[key].strip() for key in ("time", "timezone")):
        raise ValueError("schedule time and timezone must be nonempty text")
    try:
        datetime.strptime(raw["time"], "%H:%M")
        ZoneInfo(raw["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid schedule time or timezone") from exc
    if raw["window"] != "previous_day":
        raise ValueError("window must be previous_day")
    return raw


def yesterday_filters(schedule, now=None):
    schedule = validate_schedule(schedule)
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(schedule["timezone"])
    today = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"from_timestamp": (today - timedelta(days=1)).isoformat(), "to_timestamp": today.isoformat()}


def _due(schedule, now):
    if not schedule["enabled"]:
        return False
    local_time = now.astimezone(ZoneInfo(schedule["timezone"])).time()
    return local_time >= datetime.strptime(schedule["time"], "%H:%M").time()


async def _accept_scheduled(services, conn, tenant, config, *, pull, binding, dedupe):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"{tenant}:{dedupe}")
    if await conn.fetchval(
        f"SELECT 1 FROM {services.tx.pool.schema}.ingestions WHERE dedupe_key=$1", dedupe,
    ):
        return
    iid = uuid.uuid4().hex
    job = iid + ":session.pull"
    session = {"_pull": pull}
    digest = session_payload_hash(session)
    ref = f"ingest/{tenant}/{iid}.json"
    payload = IngestionTaskPayload(
        schema_version="session-ingestion.v1", source="pull", ingestion_id=iid, tenant_id=tenant,
        external_id="", user_id="", account_id="", config_revision=config.revision,
        adapter_id=binding["adapter_id"], adapter_revision=binding["revision"],
        payload_ref=ref, payload_hash=digest, pull=pull,
    )
    await asyncio.to_thread(services.extras["session_payloads"].put_payload_json, ref, payload.to_json(), session)
    await SessionIngestionRepository(services.tx).accept(
        conn, tenant_id=tenant, ingestion_id=iid, source="pull", dedupe_key=dedupe, task_id=job,
        adapter_id=binding["adapter_id"], adapter_revision=binding["revision"],
        config_revision=config.revision, payload_ref=ref, payload_hash=digest,
    )
    await PullRunRepository(services.tx).start(conn, tenant_id=tenant, run_id=iid)
    await services.tasks.enqueue(
        conn, tenant_id=tenant, task_id=job, kind="session.pull", subject_type="ingestion", subject_id=iid,
        schema_version=payload.schema_version, dedupe_key=job, payload_ref=ref, payload_hash=digest,
        config_revision=config.revision, adapter_revision=binding["revision"],
    )


async def enqueue_due(services, tenant, *, now=None):
    # Preserve the legacy default binding and its historical dedupe namespace.
    # Connection work is accepted in separate short transactions: one invalid
    # connection cannot roll back another connection's already accepted run.
    async with services.tx.tenant_tx(tenant) as conn:
        config = await load_session_config(services.extras["config_repo"], conn)
        now = now or await conn.fetchval("SELECT clock_timestamp()")
        schedule = config.get("datasource_schedule")
        if _due(schedule, now):
            binding = await AdapterRepository(services.tx).binding(conn, binding_key="datasource")
            if binding:
                window = yesterday_filters(schedule, now)
                await _accept_scheduled(services, conn, tenant, config, pull=window, binding=binding,
                                        dedupe="scheduled:" + json.dumps(window, sort_keys=True))

    from .connections import ConnectionRepository

    repository = ConnectionRepository(services.tx)
    async with services.tx.tenant_tx(tenant) as conn:
        connections = await repository.list(conn)
    for row in connections:
        document = row["document"]
        if not document["enabled"] or not _due(document["schedule"], now):
            continue
        try:
            async with services.tx.tenant_tx(tenant) as conn:
                frozen = await repository.freeze(conn, row["connection_id"], revision=row["revision"])
                window = yesterday_filters(frozen["schedule"], now)
                pull = {**window, "filters": frozen["default_filters"], "connection": frozen}
                # Reconfiguring a connection does not schedule the same business
                # window twice. A new day/window creates a new immutable run.
                dedupe = f"scheduled:connection:{row['connection_id']}:" + json.dumps(window, sort_keys=True)
                await _accept_scheduled(
                    services, conn, tenant, config, pull=pull,
                    binding={"adapter_id": frozen["adapter_id"], "revision": frozen["adapter_revision"]},
                    dedupe=dedupe,
                )
        except ValueError as exc:
            logger.warning("datasource.connection_schedule_invalid tenant=%s connection=%s error=%s",
                           tenant, row["connection_id"], type(exc).__name__)
