"""HTTP route for Agent-initiated Session ingestion."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from fastapi import HTTPException, Request

from teamEvolver.integrations.agent_registry import resolve_active_agent
from teamEvolver.integrations.context_workspace import verify_context_usage
from teamEvolver.integrations.agent_principal import resolve_agent_principal
from teamEvolver.integrations.protocol_metrics import increment
from teamEvolver.tenants.registry import effective_config, get_current_tenant
from teamEvolver.proxy.tenant_routes import get_tenant_registry
from teamEvolver.proxy.users_admin import (
    resolve_agent_subject_user_id,
    resolve_registered_user_id,
)

from ..adapters._shared.session_meta import (
    DEFAULT_TRACE_ID_KEYS,
    find_meta_value,
)
from ..http import read_limited_json_body
from ..identifiers import InvalidSessionId, sanitize_session_id, tenant_user_session_id
from ..service import SessionIngestionUnavailable, ingest
from .auth import check_legacy_ingest_key
from .protocol import (
    AgentProtocolError,
    is_session_v1_payload,
    is_session_v2_payload,
    normalize_session_envelope,
)


def register_push_routes(
    owner: Any,
    app: Any,
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> None:
    @app.post("/ingest_session")
    async def ingest_session(request: Request):
        body = await read_limited_json_body(request)
        config = effective_config(None, get_current_tenant(), owner.config)
        v2_payload = is_session_v2_payload(body)
        strict = getattr(config, "agent_protocol_identity_mode", "dual") == "tenant_user"
        if strict and not v2_payload:
            increment("agent_identity_rejected_total", reason="PROTOCOL_VERSION_UNSUPPORTED")
            raise HTTPException(status_code=400, detail="PROTOCOL_VERSION_UNSUPPORTED: use agent-session.v2")
        direct_user = isinstance(body.get("runtime_context"), dict) and "user_id" in body["runtime_context"]
        if v2_payload or direct_user:
            principal = resolve_agent_principal(
                request, config,
                (body.get("runtime_context") or {}).get("user_id") if isinstance(body.get("runtime_context"), dict) else None,
            )
            try:
                if not v2_payload:
                    body = {**body, "schema_version": "teamevolver.agent-session.v2", "protocol_version": "2.0"}
                session = normalize_session_envelope(body)
                external_id = session.get("session_id")
                session_id = tenant_user_session_id(principal.tenant_id, principal.user_id, external_id)
                session["session_id"] = session_id
                session["runtime_context"].update(
                    user_id=principal.user_id, team_evolver_user_id=principal.user_id,
                    source_session_id=external_id,
                )
                # Canonical ownership metadata cannot be overridden by the client.
                session["meta"] = {
                    **(session.get("meta") if isinstance(session.get("meta"), dict) else {}),
                    "user_id": principal.user_id, "session_id": external_id,
                }
                session["tenant_id"] = principal.tenant_id
                session["account_id"] = principal.account_id
                session["turns"] = await asyncio.to_thread(
                    verify_context_usage, config, principal=principal, turns=session["turns"],
                )
            except (AgentProtocolError, InvalidSessionId, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                return await ingest(owner, session, invalidate_cache=invalidate_cache)
            except SessionIngestionUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        request.state.agent_legacy_identity = True
        increment("agent_identity_requests_total", mode="legacy")
        agent_record: dict[str, Any] | None = None
        tenant_machine = getattr(request.state, "tenant_source", "") == "token"
        root_ingest = bool(
            getattr(request.state, "service_root_authenticated", False)
        )
        v1_payload = is_session_v1_payload(body)
        if v1_payload and not root_ingest:
            if not tenant_machine:
                # V1 ingest requires the tenant machine credential; the envelope
                # then declares which registered Agent of that tenant is reporting.
                raise HTTPException(status_code=401, detail="TENANT_TOKEN_REQUIRED")
        else:
            if (
                not root_ingest
                and getattr(request.state, "tenant_source", "") != "token"
            ):
                check_legacy_ingest_key(request)
            registry = get_tenant_registry(owner)
            if (
                registry.mode == "postgres"
                and not str(os.environ.get("EVOLVE_INGEST_API_KEY") or "").strip()
                and not root_ingest
                and getattr(request.state, "tenant_source", "") != "token"
            ):
                raise HTTPException(
                    status_code=401,
                    detail="valid tenant agent token required",
                )
        try:
            body = normalize_session_envelope(body)
        except AgentProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            session_id = sanitize_session_id(body.get("session_id"))
        except InvalidSessionId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        session = dict(body)
        session["session_id"] = session_id
        session.setdefault(
            "user_alias",
            str(
                getattr(owner.config, "sharing_user_alias", "")
                or "anonymous"
            ),
        )
        runtime = (
            session.get("runtime")
            if isinstance(session.get("runtime"), dict)
            else {}
        )
        if agent_record is None and v1_payload and tenant_machine:
            # Tenant machine credential (tevt_): the envelope declares which
            # registered Agent of this tenant is reporting. Everything below
            # (subject mapping, context usage) then runs against that resolved
            # record, so ownership semantics are unchanged.
            agent_record, code = resolve_active_agent(
                owner.config,
                agent_id=str(runtime.get("integration_id") or ""),
            )
            if agent_record is None:
                raise HTTPException(status_code=403, detail=code)
        runtime_context = (
            dict(session.get("runtime_context"))
            if isinstance(session.get("runtime_context"), dict)
            else {}
        )
        runtime_type = str(runtime.get("type") or session.get("source") or "")
        external_username = str(
            runtime_context.get("username") or session.get("user_alias") or ""
        )
        if agent_record is not None:
            local_user_id = resolve_agent_subject_user_id(
                owner.config,
                integration_id=str(agent_record.get("agent_id") or ""),
                runtime_type=runtime_type,
                external_subject=str(
                    runtime_context.get("external_subject")
                    or external_username
                ),
                allow_legacy_runtime_mapping=False,
            )
            if not local_user_id:
                raise HTTPException(
                    status_code=403,
                    detail="SUBJECT_NOT_MAPPED",
                )
        else:
            local_user_id = resolve_registered_user_id(
                owner.config,
                runtime_type=runtime_type,
                external_username=external_username,
                preferred_user_id=str(
                    runtime_context.get("team_evolver_user_id") or ""
                ),
            )
        if local_user_id:
            runtime_context["team_evolver_user_id"] = local_user_id
            session["runtime_context"] = runtime_context
        # Push-path sessions have no adapter ``extract_meta`` hook (pull-only),
        # so derive the same canonical display meta (user_id / session_id /
        # trace_id) from the Agent envelope; it feeds the console list/detail
        # identity columns. Display-only: absent values simply render as "—".
        meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
        meta.setdefault(
            "user_id",
            str(runtime_context.get("external_subject") or external_username or "").strip(),
        )
        meta.setdefault("session_id", session_id)
        trace_id = find_meta_value(runtime_context, DEFAULT_TRACE_ID_KEYS)
        if trace_id:
            meta.setdefault("trace_id", trace_id)
        session["meta"] = {
            key: value
            for key, value in meta.items()
            if not isinstance(value, (dict, list, tuple, set)) and str(value).strip()
        }
        if agent_record is not None:
            try:
                from teamEvolver.integrations.legacy_context_workspace import verify_context_usage as verify_legacy_usage

                session["turns"] = verify_legacy_usage(
                    owner.config,
                    agent_id=str(agent_record.get("agent_id") or ""),
                    user_id=local_user_id,
                    turns=[
                        dict(turn)
                        for turn in session.get("turns") or []
                        if isinstance(turn, dict)
                    ],
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid context_usage: {exc}",
                ) from exc
        try:
            return await ingest(
                owner,
                session,
                invalidate_cache=invalidate_cache,
            )
        except SessionIngestionUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
