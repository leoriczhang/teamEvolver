"""Migration-only v1 identity resolver. Delete after the zero-legacy release window."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .agent_principal import AgentPrincipal, resolve_agent_principal
from .protocol_metrics import increment


def resolve_request_principal(request: Any, config: Any, body: dict) -> AgentPrincipal:
    """Prefer declared user_id; isolate every registry dependency to the v1 path."""
    if "user_id" in body or getattr(config, "agent_protocol_identity_mode", "dual") != "dual":
        return resolve_agent_principal(request, config, body.get("user_id"))
    # Authenticate before looking up any legacy identity.
    request.state.agent_legacy_identity = True
    resolve_agent_principal(request, config, "_legacy_auth_probe")
    from .agent_registry import resolve_active_agent
    from ..proxy.users_admin import resolve_agent_subject_user_id, _load_registry, _registry_path, _find_user
    from .legacy_context_workspace import ContextStateStore

    integration_id = str(body.get("integration_id") or "")
    if not integration_id:
        increment("agent_identity_rejected_total", reason="INTEGRATION_ID_REQUIRED")
        raise HTTPException(status_code=400, detail="INTEGRATION_ID_REQUIRED")
    record, code = resolve_active_agent(config, agent_id=integration_id)
    if record is None:
        increment("agent_identity_rejected_total", reason=code)
        raise HTTPException(status_code=403, detail=code)
    user_id = ""
    state = ContextStateStore(config)
    if body.get("context_ref"):
        ref = state.resolve_ref(str(body["context_ref"]), agent_id=integration_id)
        user_id = str((ref or {}).get("user_id") or "")
        if not user_id:
            detail = "CONTEXT_SCOPE_FORBIDDEN" if request.url.path.endswith("/forget") else "CONTEXT_REF_INVALID"
            raise HTTPException(status_code=403 if detail == "CONTEXT_SCOPE_FORBIDDEN" else 404, detail=detail)
    elif body.get("context_session_id") and not body.get("external_subject"):
        session = state.get_session(str(body["context_session_id"]), agent_id=integration_id)
        user_id = str((session or {}).get("user_id") or "")
        if not user_id:
            raise HTTPException(status_code=404, detail="context session not found")
    if not user_id:
        user_id = resolve_agent_subject_user_id(
            config, integration_id=integration_id,
            runtime_type=str(record.get("runtime_type") or ""),
            external_subject=str(body.get("external_subject") or ""),
            allow_legacy_runtime_mapping=False,
        )
    if not user_id:
        increment("agent_identity_rejected_total", reason="SUBJECT_NOT_MAPPED")
        raise HTTPException(status_code=403, detail="SUBJECT_NOT_MAPPED")
    request.state.agent_legacy_record = record
    _, request.state.agent_legacy_user = _find_user(_load_registry(_registry_path(config), config), user_id)
    return resolve_agent_principal(request, config, user_id)
