"""Wire contract for Agent-initiated Session ingestion."""

from __future__ import annotations

from typing import Any

SESSION_SCHEMA = "teamevolver.agent-session.v2"


class AgentProtocolError(ValueError):
    """Raised when a versioned Agent payload violates its wire contract."""


def is_current_session_payload(payload: dict[str, Any]) -> bool:
    return (
        payload.get("schema_version") == SESSION_SCHEMA
        or str(payload.get("protocol_version") or "").startswith("2.")
    )


def normalize_session_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    from teamEvolver.security.agent_principal import normalize_user_id

    if not isinstance(payload, dict):
        raise AgentProtocolError("session payload must be an object")
    if payload.get("schema_version") != SESSION_SCHEMA:
        raise AgentProtocolError("unsupported session schema")
    if payload.get("protocol_version") != "2.0":
        raise AgentProtocolError("PROTOCOL_VERSION_UNSUPPORTED")
    runtime = payload.get("runtime") or {}
    context = payload.get("runtime_context") or {}
    if not isinstance(runtime, dict) or not isinstance(context, dict):
        raise AgentProtocolError("runtime and runtime_context must be objects")
    if "integration_id" in runtime or "external_subject" in context:
        raise AgentProtocolError("retired Agent identity fields are not accepted")
    runtime_type = str(runtime.get("type") or "").strip().lower()
    if not runtime_type:
        raise AgentProtocolError("V2 session runtime.type is required")
    if not str(payload.get("session_id") or "").strip():
        raise AgentProtocolError("V2 session session_id is required")
    try:
        user_id = normalize_user_id(context.get("user_id"))
    except ValueError as exc:
        raise AgentProtocolError(str(exc)) from exc
    turns = payload.get("turns")
    if not isinstance(turns, list) or not turns or any(not isinstance(turn, dict) for turn in turns):
        raise AgentProtocolError("V2 session turns must be a non-empty list of objects")
    for turn in turns:
        usage = turn.get("context_usage")
        if usage is not None and not isinstance(usage, dict):
            raise AgentProtocolError("context_usage must be an object")
        for key in ("memory_refs", "skill_refs"):
            if isinstance(usage, dict) and key in usage and not isinstance(usage[key], list):
                raise AgentProtocolError(f"context_usage.{key} must be a list")
    return {
        **payload, "schema_version": SESSION_SCHEMA, "protocol_version": "2.0",
        "runtime": {key: value for key, value in {**runtime, "type": runtime_type}.items() if key != "integration_id"},
        "runtime_context": {
            key: value for key, value in {**context, "user_id": user_id}.items() if key != "external_subject"
        },
    }
