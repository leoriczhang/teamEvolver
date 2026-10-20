"""Session push, datasource pull and bounded Session query APIs.

Acceptance validates identity, protocol and limits, then atomically persists the
ingestion, payload metadata and Worker task before returning 202. Repeated
submissions reuse the first acceptance. Normalization, analysis and archive run
on Worker dependencies injected by the application root."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id, get_current_tenant

from ..config import load_session_config
from ..handlers import KIND_NORMALIZE, KIND_PULL
from ..http import read_limited_json_body
from ..identifiers import InvalidSessionId, tenant_user_session_id
from ..queries import SessionQueries
from ..repository import SessionIngestionRepository
from ..schemas import (
    PAYLOAD_SCHEMA_VERSION,
    SOURCE_PULL,
    SOURCE_PUSH,
    AcceptedJob,
    IngestionTaskPayload,
    session_payload_hash,
)


def create_router(services=None, **kwargs) -> APIRouter:
    """Return this module's ``APIRouter`` for the composition root to mount."""
    router = APIRouter(tags=["session-ingestion"])
    extras = getattr(services, "extras", None) or {}
    config_repo = extras.get("config_repo")
    payloads = extras.get("session_payloads")
    if services is None or config_repo is None or payloads is None:
        return router

    tx = services.tx
    tasks = services.tasks
    repo = SessionIngestionRepository(tx)
    queries = SessionQueries(tx)

    async def _accept(
        *, source: str, external_id: str, user_id: str, account_id: str,
        session: dict[str, Any], pull: dict[str, Any] | None = None,
        adapter_id: str = "", adapter_revision: int = 0, business_time: Any = None,
        kind: str = KIND_NORMALIZE,
    ) -> AcceptedJob:
        """Atomic acceptance: ingestion + raw payload + worker task, then 202."""
        tenant_id = current_tenant_id()
        ingestion_id = uuid.uuid4().hex
        payload_hash = session_payload_hash(session)
        payload_ref = f"ingest/{tenant_id}/{ingestion_id}.json"
        task_id = f"{ingestion_id}:{kind}"
        task_payload = IngestionTaskPayload(
            schema_version=PAYLOAD_SCHEMA_VERSION,
            source=source,
            ingestion_id=ingestion_id,
            tenant_id=tenant_id,
            external_id=external_id,
            user_id=user_id,
            account_id=account_id,
            adapter_id=adapter_id,
            adapter_revision=adapter_revision,
            payload_ref=payload_ref,
            payload_hash=payload_hash,
            pull=pull or {},
        )
        # Pin the effective session config revision into the task payload so a
        # later config edit cannot alter this run.
        async with tx.tenant_tx(tenant_id) as conn:
            module_config = await load_session_config(config_repo, conn)
        task_payload.config_revision = module_config.revision

        # Raw payload goes to the injected store first (it is the pinned source
        # the worker reads); then the ingestion + task rows commit together.
        await asyncio.to_thread(payloads.put_payload_json, payload_ref, task_payload.to_json(), session)

        dedupe_key = _dedupe_key(source, external_id, pull)
        async with tx.tenant_tx(tenant_id) as conn:
            accepted_id = await repo.accept(
                conn,
                tenant_id=tenant_id,
                ingestion_id=ingestion_id,
                source=source,
                external_id=external_id,
                user_id=user_id,
                account_id=account_id,
                dedupe_key=dedupe_key,
                task_id=task_id,
                adapter_id=adapter_id,
                adapter_revision=adapter_revision,
                config_revision=task_payload.config_revision,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                business_time=business_time,
            )
            if accepted_id != ingestion_id:
                existing = await repo.get_ingestion(conn, ingestion_id=accepted_id)
                job_id = str((existing or {}).get("task_id") or f"{accepted_id}:{kind}")
                return AcceptedJob(
                    ingestion_id=accepted_id, job_id=job_id,
                    status=str((existing or {}).get("status") or "accepted"), dedupe=True,
                )
            await tasks.enqueue(
                conn,
                tenant_id=tenant_id,
                task_id=task_id,
                kind=kind,
                subject_type="ingestion",
                subject_id=ingestion_id,
                schema_version=PAYLOAD_SCHEMA_VERSION,
                dedupe_key=task_id,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                config_revision=task_payload.config_revision,
                adapter_revision=adapter_revision,
                trace_id="",
            )
        return AcceptedJob(ingestion_id=ingestion_id, job_id=task_id, status="accepted")

    _register_push(router, _accept, services)
    _register_pull(router, _accept, tx)
    from .console import create_router as console_router
    router.include_router(console_router(services))
    _register_reads(router, tx, queries)
    from .datasource import create_router as datasource_router
    router.include_router(datasource_router(services, _accept))
    return router


def _dedupe_key(source: str, external_id: str, pull: dict[str, Any] | None) -> str:
    """Idempotency key: a repeated push of the same session, or an identical
    pull request window, maps back to the first acceptance."""
    if source == SOURCE_PUSH:
        return f"push:{external_id}" if external_id else ""
    window = json.dumps(pull or {}, sort_keys=True, separators=(",", ":"))
    return f"pull:{window}"


def _register_push(router: APIRouter, accept, services) -> None:
    @router.post("/ingest_session")
    async def ingest_session(request: Request):  # noqa: ANN202
        body = await read_limited_json_body(request)
        from ..push.protocol import AgentProtocolError, normalize_session_envelope
        try:
            body = normalize_session_envelope(body)
        except AgentProtocolError as exc:
            raise HTTPException(400, str(exc)) from exc
        from teamEvolver.security.agent_principal import resolve_agent_principal

        runtime_context = body.get("runtime_context") if isinstance(body.get("runtime_context"), dict) else {}
        tenant = get_current_tenant()
        config = services.extras.get("config") or getattr(tenant, "config", None)
        principal = resolve_agent_principal(request, config, (runtime_context or {}).get("user_id"))
        external_id = body.get("session_id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise HTTPException(status_code=400, detail="session_id is required")
        try:
            # The canonical storage id is opaque + collision-resistant.
            tenant_user_session_id(principal.tenant_id, principal.user_id, external_id)
        except InvalidSessionId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = {
            **body,
            "tenant_id": principal.tenant_id,
            "account_id": principal.account_id,
            "meta": {
                **(body.get("meta") if isinstance(body.get("meta"), dict) else {}),
                "user_id": principal.user_id, "session_id": external_id,
            },
        }
        verifier = services.extras.get("context_usage_verifier")
        if verifier:
            try:
                session["turns"] = await verifier(principal, body.get("turns") or [])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        job = await accept(
            source=SOURCE_PUSH,
            external_id=external_id,
            user_id=principal.user_id,
            account_id=principal.account_id,
            session=session,
            business_time=body.get("business_time"),
        )
        return _accepted_response(job)


def _register_pull(router: APIRouter, accept, tx) -> None:
    @router.post("/api/datasource/pull")
    async def pull(request: Request):  # noqa: ANN202
        _require_tenant_admin(request)
        body = await read_limited_json_body(request)
        tenant = get_current_tenant()
        if set(body) - {"adapter_id", "from_timestamp", "to_timestamp", "filters"}:
            raise HTTPException(400, "expected adapter_id, timestamps and filters")
        adapter_id = str(body.get("adapter_id") or "datasource")
        # The adapter must be bound to a pinned, immutable revision before a pull
        # can be enqueued.
        tenant_id = current_tenant_id()
        async with tx.tenant_tx(tenant_id) as conn:
            from ..adapters.repository import AdapterRepository

            binding = await AdapterRepository(tx).binding(conn, binding_key=adapter_id)
        if binding is None:
            raise HTTPException(status_code=409, detail=f"adapter {adapter_id!r} is not bound to a revision")
        pull_filters = {k: body[k] for k in ("from_timestamp", "to_timestamp", "filters") if k in body}
        job = await accept(
            source=SOURCE_PULL,
            external_id="",
            user_id="",
            account_id=getattr(tenant, "account_id", "") or "",
            session={"_pull": pull_filters},
            pull=pull_filters,
            adapter_id=binding["adapter_id"],
            adapter_revision=int(binding["revision"]),
            kind=KIND_PULL,
        )
        return _accepted_response(job)


def _register_reads(router: APIRouter, tx, queries: SessionQueries) -> None:
    @router.get("/api/sessions")
    async def list_sessions(request: Request):  # noqa: ANN202
        _require_tenant_admin(request)
        tenant_id = current_tenant_id()
        try:
            limit = int(request.query_params.get("limit") or 0)
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer") from None
        before = request.query_params.get("before") or None
        async with tx.tenant_tx(tenant_id) as conn:
            rows = await queries.list_sessions(conn, limit=limit, before=before)
        return {"sessions": rows, "count": len(rows)}

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):  # noqa: ANN202
        _require_tenant_admin(request)
        tenant_id = current_tenant_id()
        async with tx.tenant_tx(tenant_id) as conn:
            row = await queries.get_session(conn, session_id=session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row


def _require_tenant_admin(request: Request) -> None:
    from teamEvolver.admin.authorization import require_tenant_admin

    # A tenant-token caller (agent data plane) is pre-authorized for its own
    # tenant; the console path requires a tenant admin.
    if getattr(request.state, "tenant_source", "") == "token":
        return
    require_tenant_admin(getattr(request.state, "console_user", None))


def _accepted_response(job: AcceptedJob):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=202, content=job.to_response())
