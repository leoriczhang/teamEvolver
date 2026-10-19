"""Persistent daily pull acceptance; executed by the shared Worker lifecycle."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .adapters.repository import AdapterRepository
from .config import load_session_config
from .repository import SessionIngestionRepository
from .schemas import IngestionTaskPayload, session_payload_hash


def validate_schedule(raw):
    if set(raw) != {"enabled", "time", "timezone", "window"} or type(raw["enabled"]) is not bool:
        raise ValueError("expected enabled, time, timezone and window")
    datetime.strptime(raw["time"], "%H:%M")
    ZoneInfo(raw["timezone"])
    if raw["window"] != "previous_day":
        raise ValueError("window must be previous_day")
    return raw


def yesterday_filters(schedule, now=None):
    schedule = validate_schedule(schedule)
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(schedule["timezone"])
    today = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"from_timestamp": (today - timedelta(days=1)).isoformat(), "to_timestamp": today.isoformat()}


async def enqueue_due(services, tenant):
    async with services.tx.tenant_tx(tenant) as conn:
        config = await load_session_config(services.extras["config_repo"], conn)
        schedule = config.get("datasource_schedule")
        if not schedule["enabled"]:
            return
        now = await conn.fetchval("SELECT clock_timestamp()")
        if now.astimezone(ZoneInfo(schedule["timezone"])).strftime("%H:%M") < schedule["time"]:
            return
        binding = await AdapterRepository(services.tx).binding(conn, binding_key="datasource")
        if binding is None:
            return
        filters = yesterday_filters(schedule, now)
        dedupe = "scheduled:" + json.dumps(filters, sort_keys=True)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"{tenant}:{dedupe}")
        if await conn.fetchval(
            f"SELECT 1 FROM {services.tx.pool.schema}.ingestions WHERE dedupe_key=$1", dedupe,
        ):
            return
        iid = uuid.uuid4().hex
        job = iid + ":session.pull"
        session = {"_pull": filters}
        digest = session_payload_hash(session)
        ref = f"ingest/{tenant}/{iid}.json"
        payload = IngestionTaskPayload(
            schema_version="session-ingestion.v1", source="pull", ingestion_id=iid, tenant_id=tenant,
            external_id="", user_id="", account_id="", config_revision=config.revision,
            adapter_id=binding["adapter_id"], adapter_revision=binding["revision"],
            payload_ref=ref, payload_hash=digest, pull=filters,
        )
        await asyncio.to_thread(services.extras["session_payloads"].put_payload_json, ref, payload.to_json(), session)
        await SessionIngestionRepository(services.tx).accept(
            conn, tenant_id=tenant, ingestion_id=iid, source="pull", dedupe_key=dedupe, task_id=job,
            adapter_id=binding["adapter_id"], adapter_revision=binding["revision"],
            config_revision=config.revision, payload_ref=ref, payload_hash=digest,
        )
        await services.tasks.enqueue(
            conn, tenant_id=tenant, task_id=job, kind="session.pull", subject_type="ingestion", subject_id=iid,
            schema_version=payload.schema_version, dedupe_key=job, payload_ref=ref, payload_hash=digest,
            config_revision=config.revision, adapter_revision=binding["revision"],
        )
