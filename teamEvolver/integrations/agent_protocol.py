"""Versioned wire contracts for Agent integrations."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any
from urllib.parse import urlparse

AGENT_PROTOCOL_VERSION = "1.0"
REGISTRATION_SCHEMA_V1 = "teamevolver.agent-registration.v1"
SESSION_SCHEMA_V1 = "teamevolver.agent-session.v1"
CONTEXT_RESULT_SCHEMA_V1 = "teamevolver.context-result.v1"
REPLAY_REQUEST_SCHEMA_V1 = "teamevolver.replay-branch-request.v1"
REPLAY_RESULT_SCHEMA_V1 = "teamevolver.replay-branch-result.v1"

CAP_SESSION_INGEST = "session.ingest.v1"
CAP_REPLAY_BRANCH = "replay.branch.v1"
CAP_SKILL_SYNC = "skill.sync.v1"
CAP_CONTEXT_OPENVIKING = "context.openviking.v1"
CAP_CONTEXT_WORKSPACE = "context.workspace.v1"
CAP_MEMORY_PERSONAL_READ = "memory.personal.read.v1"
CAP_MEMORY_PERSONAL_WRITE = "memory.personal.write.v1"
CAP_MEMORY_TEAM_READ = "memory.team.read.v1"
CAP_SKILL_PERSONAL_READ = "skill.personal.read.v1"
CAP_SKILL_TEAM_READ = "skill.team.read.v1"
CAP_SKILL_TEAM_EVOLVE = "skill.team.evolve.v1"
CAP_SKILL_BUNDLE = "skill.bundle.v1"

_CAPABILITY_ALIASES = {
    "session_ingest": CAP_SESSION_INGEST,
    "true_replay": CAP_REPLAY_BRANCH,
    "skill_sync": CAP_SKILL_SYNC,
    "openviking_context": CAP_CONTEXT_OPENVIKING,
}


class AgentProtocolError(ValueError):
    """Raised when a versioned Agent payload violates the wire contract."""


def _require_supported_version(value: Any) -> str:
    version = str(value or "").strip()
    if version and version.split(".", 1)[0] != "1":
        raise AgentProtocolError(
            f"PROTOCOL_VERSION_UNSUPPORTED: {version}"
        )
    return version


def is_v1_payload(payload: dict[str, Any]) -> bool:
    schema = str(payload.get("schema_version") or "").strip().lower()
    version = str(payload.get("protocol_version") or "").strip()
    return schema in {REGISTRATION_SCHEMA_V1, SESSION_SCHEMA_V1} or version.startswith(
        "1."
    )


def validate_endpoint_url(value: Any) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentProtocolError("Agent endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise AgentProtocolError("Agent endpoint cannot contain credentials")
    host = parsed.hostname.lower()
    if host in {"metadata.google.internal", "metadata.internal"}:
        raise AgentProtocolError("Agent endpoint targets a forbidden metadata host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or str(address) == "169.254.169.254"
    ):
        raise AgentProtocolError("Agent endpoint targets a forbidden IP address")
    return endpoint


def canonical_capability_id(value: Any) -> str:
    raw = str(value or "").strip()
    return _CAPABILITY_ALIASES.get(raw, raw)


def _capabilities(payload: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    raw = payload.get("capabilities")
    details = (
        dict(payload.get("capability_details"))
        if isinstance(payload.get("capability_details"), dict)
        else {}
    )
    names: list[str] = []
    if isinstance(raw, dict):
        for name, detail in raw.items():
            cleaned = str(name or "").strip()
            if not cleaned:
                continue
            names.append(cleaned)
            if isinstance(detail, dict):
                details.setdefault(canonical_capability_id(cleaned), dict(detail))
    elif isinstance(raw, list):
        names = [
            str(item or "").strip()
            for item in raw
            if str(item or "").strip()
        ]
    elif raw not in (None, ""):
        raise AgentProtocolError("capabilities must be a list or object")
    names = sorted(set(names))
    canonical = sorted({canonical_capability_id(name) for name in names})
    return names, canonical, details


def normalize_registration(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy and V1 registration payloads into one internal shape."""
    if not isinstance(payload, dict):
        raise AgentProtocolError("registration body must be an object")
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    agent_id = str(payload.get("agent_id") or runtime.get("integration_id") or "").strip()
    runtime_type = str(payload.get("runtime_type") or runtime.get("type") or "").strip()
    if not agent_id:
        raise AgentProtocolError("agent_id is required")
    if not runtime_type:
        runtime_type = agent_id.split(":", 1)[0]
    protocol_version = _require_supported_version(payload.get("protocol_version"))
    v1 = is_v1_payload(payload)
    schema = str(payload.get("schema_version") or "").strip().lower()
    if v1 and schema and schema != REGISTRATION_SCHEMA_V1:
        raise AgentProtocolError(f"unsupported registration schema: {schema}")
    if v1 and isinstance(payload.get("storage"), dict) and payload["storage"]:
        raise AgentProtocolError(
            "V1 registration cannot carry storage credentials; configure OpenViking in teamEvolver"
        )
    names, canonical, details = _capabilities(payload)
    raw_endpoints = (
        payload.get("endpoints")
        if isinstance(payload.get("endpoints"), dict)
        else {}
    )
    endpoints = (
        {
            str(key): validate_endpoint_url(value)
            for key, value in raw_endpoints.items()
            if str(value or "").strip()
        }
        if v1
        else dict(raw_endpoints)
    )
    return {
        **payload,
        "schema_version": (
            str(payload.get("schema_version") or REGISTRATION_SCHEMA_V1)
            if v1
            else ""
        ),
        "protocol_version": protocol_version or (AGENT_PROTOCOL_VERSION if v1 else ""),
        "runtime_version": str(
            payload.get("runtime_version") or runtime.get("version") or ""
        ).strip(),
        "agent_id": agent_id,
        "runtime_type": runtime_type.lower(),
        "runtime_class": str(
            payload.get("runtime_class") or runtime_type
        ).strip().lower(),
        "capabilities": names,
        "capability_ids": canonical,
        "capability_details": details,
        "endpoints": endpoints,
        "compatibility": "compatible" if v1 else "legacy",
    }


def normalize_session_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate V1 identity fields while preserving the existing session shape."""
    if not isinstance(payload, dict):
        raise AgentProtocolError("session payload must be an object")
    normalized = dict(payload)
    _require_supported_version(
        payload.get("protocol_version")
        or (
            payload.get("runtime", {}).get("protocol_version")
            if isinstance(payload.get("runtime"), dict)
            else ""
        )
    )
    if not is_v1_payload(payload):
        normalized.setdefault("protocol_compatibility", "legacy")
        return normalized
    schema = str(payload.get("schema_version") or SESSION_SCHEMA_V1).strip().lower()
    if schema != SESSION_SCHEMA_V1:
        raise AgentProtocolError(f"unsupported session schema: {schema}")
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
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
        payload.get("protocol_version") or runtime.get("protocol_version") or AGENT_PROTOCOL_VERSION
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


def replay_request_id(
    *,
    job_id: str,
    case_index: int,
    branch: str,
    candidate_revision: str,
) -> str:
    value = json.dumps(
        {
            "job_id": str(job_id),
            "case_index": int(case_index),
            "branch": str(branch),
            "candidate_revision": str(candidate_revision),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "replay_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def normalize_replay_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentProtocolError("replay request must be an object")
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_REQUEST_SCHEMA_V1:
        raise AgentProtocolError(f"unsupported replay request schema: {schema}")
    request_id = str(payload.get("request_id") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    branch = str(payload.get("branch") or "").strip().lower()
    if not request_id or not job_id:
        raise AgentProtocolError("replay request_id and job_id are required")
    if branch not in {"baseline", "candidate"}:
        raise AgentProtocolError("replay branch must be baseline or candidate")
    case = payload.get("case")
    if not isinstance(case, dict) or not str(
        case.get("query") or case.get("instruction") or ""
    ).strip():
        raise AgentProtocolError("replay case query is required")
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    try:
        timeout_seconds = int(limits.get("timeout_seconds") or 600)
        max_interactions = int(limits.get("max_interactions") or 1)
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError("replay limits must be integers") from exc
    if not 30 <= timeout_seconds <= 3600:
        raise AgentProtocolError("replay timeout_seconds must be between 30 and 3600")
    if not 1 <= max_interactions <= 20:
        raise AgentProtocolError("replay max_interactions must be between 1 and 20")
    return {
        **payload,
        "schema_version": REPLAY_REQUEST_SCHEMA_V1,
        "protocol_version": AGENT_PROTOCOL_VERSION,
        "request_id": request_id,
        "job_id": job_id,
        "branch": branch,
        "case": dict(case),
        "limits": {
            **limits,
            "timeout_seconds": timeout_seconds,
            "max_interactions": max_interactions,
        },
    }


def normalize_replay_result(
    payload: dict[str, Any],
    *,
    expected_request_id: str,
    expected_branch: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentProtocolError("INVALID_RESPONSE: replay result must be an object")
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_RESULT_SCHEMA_V1:
        raise AgentProtocolError(
            f"INVALID_RESPONSE: unsupported replay result schema: {schema}"
        )
    request_id = str(payload.get("request_id") or "")
    branch = str(payload.get("branch") or "").lower()
    if request_id != expected_request_id:
        raise AgentProtocolError("INVALID_RESPONSE: replay request_id mismatch")
    if branch != expected_branch:
        raise AgentProtocolError("INVALID_RESPONSE: replay branch mismatch")
    status = str(payload.get("status") or "").lower()
    if status not in {"succeeded", "failed", "unsupported"}:
        raise AgentProtocolError("INVALID_RESPONSE: invalid replay status")
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    if status == "succeeded":
        for key in ("interaction_turns", "tool_call_count", "total_tokens"):
            value = metrics.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AgentProtocolError(
                    f"INVALID_RESPONSE: metrics.{key} must be a non-negative integer"
                )
    error = payload.get("error")
    if status != "succeeded" and not isinstance(error, dict):
        error = {
            "code": "EXECUTION_FAILED",
            "message": str(error or "replay branch failed"),
            "retryable": False,
        }
    return {
        **payload,
        "schema_version": REPLAY_RESULT_SCHEMA_V1,
        "protocol_version": AGENT_PROTOCOL_VERSION,
        "request_id": request_id,
        "branch": branch,
        "status": status,
        "metrics": dict(metrics),
        "error": error,
    }
