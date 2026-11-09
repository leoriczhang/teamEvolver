"""Atomic cross-window acceptance of a factual source snapshot."""
from __future__ import annotations

import asyncio

from .repository import SessionIngestionRepository
from .schemas import IngestionTaskPayload, session_payload_hash
from .source_repository import SourceRevisionRepository


async def accept_source_item(services, task, parent, session, external_id, deps) -> bool:
    sources = SourceRevisionRepository(services.tx)
    repo = SessionIngestionRepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        planned, identifier, duplicate = await sources.prepare(
            conn, tenant_id=task.tenant_id, external_id=external_id, session=session,
        )
        if duplicate:
            return False
        if identifier is None:
            raise ValueError("source acceptance requires verified upstream identity")
        ref = f"ingest/{task.tenant_id}/{identifier}.json"
        planned["provenance"]["full_snapshot_ref"] = ref
        child = IngestionTaskPayload(
            schema_version=parent.schema_version, source=parent.source, ingestion_id=identifier,
            tenant_id=task.tenant_id, external_id=external_id, user_id=str(planned.get("user_id") or ""),
            account_id=parent.account_id, config_revision=parent.config_revision,
            adapter_id=parent.adapter_id, adapter_revision=parent.adapter_revision,
            payload_ref=ref, payload_hash=session_payload_hash(planned),
        )
        await asyncio.to_thread(deps.payloads.put_payload_json, ref, child.to_json(), planned)
        await repo.accept(
            conn, tenant_id=task.tenant_id, ingestion_id=identifier, source=child.source,
            external_id=external_id, user_id=child.user_id, account_id=child.account_id,
            dedupe_key=identifier, task_id=f"{identifier}:session.normalize", adapter_id=child.adapter_id,
            adapter_revision=child.adapter_revision, config_revision=child.config_revision,
            payload_ref=ref, payload_hash=child.payload_hash, business_time=planned.get("business_time"),
        )
        await services.tasks.enqueue(
            conn, tenant_id=task.tenant_id, task_id=f"{identifier}:session.normalize", kind="session.normalize",
            subject_type="ingestion", subject_id=identifier, schema_version=child.schema_version,
            dedupe_key=f"{identifier}:session.normalize", payload_ref=ref, payload_hash=child.payload_hash,
            config_revision=child.config_revision, adapter_revision=child.adapter_revision,
        )
        await sources.remember(conn, tenant_id=task.tenant_id, session=planned, ingestion_id=identifier)
    return True
