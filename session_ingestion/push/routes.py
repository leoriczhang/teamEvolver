"""HTTP route for Agent-initiated Session ingestion."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import HTTPException, Request

from teamEvolver.integrations.context_workspace import verify_context_usage
from teamEvolver.integrations.agent_principal import resolve_agent_principal
from teamEvolver.tenants.registry import effective_config, get_current_tenant

from ..http import read_limited_json_body
from ..identifiers import InvalidSessionId, tenant_user_session_id
from ..service import SessionIngestionUnavailable, ingest
from .protocol import (
    AgentProtocolError,
    is_current_session_payload,
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
        if not is_current_session_payload(body):
            raise HTTPException(status_code=400, detail="PROTOCOL_VERSION_UNSUPPORTED: use agent-session.v2")
        principal = resolve_agent_principal(
            request, config,
            (body.get("runtime_context") or {}).get("user_id") if isinstance(body.get("runtime_context"), dict) else None,
        )
        try:
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
