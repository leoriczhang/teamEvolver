"""Wire contract for Agent-initiated Session ingestion."""

from __future__ import annotations

from typing import Any

AGENT_PROTOCOL_VERSION = "1.0"
REGISTRATION_SCHEMA_V1 = "teamevolver.agent-registration.v1"
SESSION_SCHEMA_V1 = "teamevolver.agent-session.v1"
SESSION_SCHEMA_V2 = "teamevolver.agent-session.v2"
CAP_SESSION_INGEST = "session.ingest.v1"


class AgentProtocolError(ValueError):
    """Raised when a versioned Agent payload violates its wire contract."""


def _require_supported_version(value: Any) -> str:
    version = str(value or "").strip()
    if version and version.split(".", 1)[0] not in {"1", "2"}:
        raise AgentProtocolError(f"PROTOCOL_VERSION_UNSUPPORTED: {version}")
    return version


def is_session_v1_payload(payload: dict[str, Any]) -> bool:
    schema = str(payload.get("schema_version") or "").strip().lower()
    version = str(payload.get("protocol_version") or "").strip()
    return schema in {REGISTRATION_SCHEMA_V1, SESSION_SCHEMA_V1} or version.startswith(
        "1."
    )


def is_session_v2_payload(payload: dict[str, Any]) -> bool:
    return (
        payload.get("schema_version") == SESSION_SCHEMA_V2
        or str(payload.get("protocol_version") or "").startswith("2.")
    )


def normalize_session_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate V1 identity fields while preserving the Session shape."""
    if not isinstance(payload, dict):
        raise AgentProtocolError("session payload must be an object")
    normalized = dict(payload)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    _require_supported_version(
        payload.get("protocol_version") or runtime.get("protocol_version")
    )
    if is_session_v2_payload(payload):
        return _normalize_v2(payload)
    if not is_session_v1_payload(payload):
        normalized.setdefault("protocol_compatibility", "legacy")
        return normalized
    schema = str(payload.get("schema_version") or SESSION_SCHEMA_V1).strip().lower()
    if schema != SESSION_SCHEMA_V1:
        raise AgentProtocolError(f"unsupported session schema: {schema}")
    runtime_type = str(runtime.get("type") or "").strip().lower()
    integration_id = str(runtime.get("integration_id") or "").strip()
    if not str(payload.get("session_id") or "").strip():
        raise AgentProtocolError("V1 session session_id is required")
    if not runtime_type:
        raise AgentProtocolError("V1 session runtime.type is required")
    if not integration_id:
        raise AgentProtocolError("V1 session runtime.integration_id is required")
    turns = payload.get("turns")
    if not isinstance(turns, list) or not turns:
        raise AgentProtocolError("V1 session turns must be a non-empty list")
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict):
            raise AgentProtocolError(f"V1 session turn #{index} must be an object")
        usage = turn.get("context_usage")
        if usage is not None and not isinstance(usage, dict):
            raise AgentProtocolError(
                f"V1 session turn #{index} context_usage must be an object"
            )
        if isinstance(usage, dict):
            for key in ("memory_refs", "skill_refs"):
                if key in usage and not isinstance(usage.get(key), list):
                    raise AgentProtocolError(
                        f"V1 session turn #{index} context_usage.{key} must be a list"
                    )
    normalized["schema_version"] = SESSION_SCHEMA_V1
    normalized["protocol_version"] = str(
        payload.get("protocol_version")
        or runtime.get("protocol_version")
        or AGENT_PROTOCOL_VERSION
    )
    normalized["runtime"] = {
        **runtime,
        "type": runtime_type,
        "integration_id": integration_id,
        "protocol_version": str(
            runtime.get("protocol_version")
            or payload.get("protocol_version")
            or AGENT_PROTOCOL_VERSION
        ),
    }
    normalized["protocol_compatibility"] = "compatible"
    return normalized


def _normalize_v2(payload: dict[str, Any]) -> dict[str, Any]:
    from teamEvolver.integrations.agent_principal import normalize_user_id

    if payload.get("schema_version", SESSION_SCHEMA_V2) != SESSION_SCHEMA_V2:
        raise AgentProtocolError("unsupported session schema")
    if str(payload.get("protocol_version", "2.0")).split(".", 1)[0] != "2":
        raise AgentProtocolError("PROTOCOL_VERSION_UNSUPPORTED")
    runtime = payload.get("runtime") or {}
    context = payload.get("runtime_context") or {}
    if not isinstance(runtime, dict) or not isinstance(context, dict):
        raise AgentProtocolError("runtime and runtime_context must be objects")
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
        **payload, "schema_version": SESSION_SCHEMA_V2, "protocol_version": "2.0",
        "runtime": {key: value for key, value in {**runtime, "type": runtime_type}.items() if key != "integration_id"},
        "runtime_context": {
            key: value for key, value in {**context, "user_id": user_id}.items() if key != "external_subject"
        },
        "protocol_compatibility": "tenant_user",
    }
