"""Public durable Session acceptance used by HTTP and configured collectors."""

from __future__ import annotations

import asyncio
import uuid

from .config import load_session_config
from .repository import SessionIngestionRepository
from .schemas import AcceptedJob, IngestionTaskPayload, session_payload_hash


async def accept(services, *, tenant_id, source, external_id, user_id, account_id, session,
                 dedupe_key="", pull=None, adapter_id="", adapter_revision=0, business_time=None,
                 kind="session.normalize"):
    repo = SessionIngestionRepository(services.tx)
    ingestion_id = uuid.uuid4().hex
    payload_ref = f"ingest/{tenant_id}/{ingestion_id}.json"
    task_id = f"{ingestion_id}:{kind}"
    async with services.tx.tenant_tx(tenant_id) as conn:
        config = await load_session_config(services.extras["config_repo"], conn)
    payload = IngestionTaskPayload(
        schema_version="session-ingestion.v1", source=source, ingestion_id=ingestion_id, tenant_id=tenant_id,
        external_id=external_id, user_id=user_id, account_id=account_id, config_revision=config.revision,
        adapter_id=adapter_id, adapter_revision=adapter_revision, payload_ref=payload_ref,
        payload_hash=session_payload_hash(session), pull=pull or {},
    )
    await asyncio.to_thread(services.extras["session_payloads"].put_payload_json,
                            payload_ref, payload.to_json(), session)
    async with services.tx.tenant_tx(tenant_id) as conn:
        accepted_id = await repo.accept(
            conn, tenant_id=tenant_id, ingestion_id=ingestion_id, source=source, external_id=external_id,
            user_id=user_id, account_id=account_id, dedupe_key=dedupe_key, task_id=task_id,
            adapter_id=adapter_id, adapter_revision=adapter_revision, config_revision=config.revision,
            payload_ref=payload_ref, payload_hash=payload.payload_hash, business_time=business_time,
        )
        if accepted_id != ingestion_id:
            existing = await repo.get_ingestion(conn, ingestion_id=accepted_id) or {}
            return AcceptedJob(accepted_id, str(existing.get("task_id") or f"{accepted_id}:{kind}"),
                               str(existing.get("status") or "accepted"), True)
        await services.tasks.enqueue(
            conn, tenant_id=tenant_id, task_id=task_id, kind=kind, subject_type="ingestion",
            subject_id=ingestion_id, schema_version=payload.schema_version, dedupe_key=task_id,
            payload_ref=payload_ref, payload_hash=payload.payload_hash, config_revision=config.revision,
            adapter_revision=adapter_revision,
        )
    return AcceptedJob(ingestion_id, task_id, "accepted")
