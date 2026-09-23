"""Tenant-bound Agent identity. No Agent or user registry is consulted."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from ..tenants.registry import current_tenant_id, effective_config, get_current_tenant
from .protocol_metrics import increment


@dataclass(frozen=True)
class AgentPrincipal:
    tenant_id: str
    account_id: str
    user_id: str


def normalize_user_id(value: Any) -> str:
    """One normalization rule for namespace segments on every Agent endpoint."""
    if value is None or value == "":
        raise ValueError("USER_ID_REQUIRED")
    if not isinstance(value, str):
        raise ValueError("USER_ID_INVALID")
    user_id = unicodedata.normalize("NFKC", value).strip()
    if not user_id:
        raise ValueError("USER_ID_REQUIRED")
    if (
        len(user_id) > 160
        or user_id in {".", ".."}
        or ".." in user_id
        or any(not (char.isalnum() or char in "_-.@") for char in user_id)
    ):
        raise ValueError("USER_ID_INVALID")
    return user_id


def resolve_agent_principal(request: Any, config: Any, user_id: Any) -> AgentPrincipal:
    """Accept only a tevt_ credential already verified by tenant middleware."""
    authorization = str(request.headers.get("authorization") or "").strip()
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    tenant = get_current_tenant()
    if (
        not token.startswith("tevt_")
        or getattr(request.state, "tenant_source", "") != "token"
        or tenant is None
        or tenant.status != "active"
    ):
        increment("agent_identity_rejected_total", reason="TENANT_TOKEN_REQUIRED")
        raise HTTPException(status_code=401, detail="TENANT_TOKEN_REQUIRED")
    try:
        normalized = normalize_user_id(user_id)
    except ValueError as exc:
        increment("agent_identity_rejected_total", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolved = effective_config(None, tenant, config)
    account = str(getattr(resolved, "sharing_viking_account", "") or "").strip()
    if not account:
        increment("agent_identity_rejected_total", reason="TENANT_ACCOUNT_REQUIRED")
        raise HTTPException(status_code=503, detail="TENANT_ACCOUNT_REQUIRED")
    if not getattr(request.state, "agent_identity_counted", False):
        mode = "legacy" if getattr(request.state, "agent_legacy_identity", False) else "tenant_user"
        increment("agent_identity_requests_total", mode=mode)
        request.state.agent_identity_counted = True
    return AgentPrincipal(current_tenant_id(), account, normalized)
