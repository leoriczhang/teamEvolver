"""Public durable Session acceptance used by HTTP and configured collectors."""

from __future__ import annotations

import asyncio
import uuid

from .config import load_session_config
from .repository import SessionIngestionRepository
from .schemas import AcceptedJob, IngestionTaskPayload, session_payload_hash


async def correct_ma_submission(services, *, tenant_id, connection_id, parent_session_id,
                                metadata, ingestion_ids=()):
    """Repair attribution/display without accepting or reanalyzing the Session."""
    from .projections import session_list_meta
    from .submission import apply_submission
    repo = SessionIngestionRepository(services.tx)
    payloads = services.extras["session_payloads"]
    async with services.tx.tenant_tx(tenant_id) as conn:
        rows = await repo.managed_sessions(
            conn, connection_id=connection_id, parent_session_id=parent_session_id,
        )
        ingestions = [await repo.get_ingestion(conn, ingestion_id=i) for i in ingestion_ids if i]
    validated = []
    for item in ingestions:
        if not item:
            continue
        original = await asyncio.to_thread(payloads.get_session, item["payload_ref"])
        runtime, context = original.get("runtime") or {}, original.get("runtime_context") or {}
        if (runtime.get("type") != "managed-agents" or runtime.get("connection_id") != connection_id
                or context.get("source_session_id") != parent_session_id):
            raise ValueError("Submission correction source does not match")
        validated.append(item["ingestion_id"])
    corrections = []
    for row in rows:
        original = await asyncio.to_thread(payloads.get_session, row["content_ref"])
        projected = apply_submission(original, metadata)
        meta = session_list_meta(projected, {**row["meta"], **projected["meta"]})
        if row["user_id"] == metadata["user_id"] and row["meta"] == meta and original == projected:
            continue
        ref, sha = await asyncio.to_thread(
            payloads.put_session, row["ingestion_id"], projected, stage="submission",
        )
        corrections.append((row, meta, ref, sha))
    async with services.tx.tenant_tx(tenant_id) as conn:
        for row, meta, ref, sha in corrections:
            if not await repo.correct_submission(
                conn, row=row, user_id=metadata["user_id"], meta=meta, content_ref=ref, content_hash=sha,
            ):
                raise ValueError("Session changed during submission correction")
        for ident in set(validated + [r["ingestion_id"] for r in rows]):
            await repo.correct_ingestion_user(conn, ingestion_id=ident, user_id=metadata["user_id"])
    return {"sessions_corrected": len(corrections), "ingestions_checked": len(validated)}


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
