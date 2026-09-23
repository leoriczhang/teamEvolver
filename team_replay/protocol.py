"""Versioned wire contracts used by Replay-capable Agent runtimes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

AGENT_PROTOCOL_VERSION = "1.0"
REPLAY_REQUEST_SCHEMA_V1 = "teamevolver.replay-branch-request.v1"
REPLAY_RESULT_SCHEMA_V1 = "teamevolver.replay-branch-result.v1"
REPLAY_TURN_REQUEST_SCHEMA_V1 = "teamevolver.replay-turn-request.v1"
REPLAY_TURN_RESULT_SCHEMA_V1 = "teamevolver.replay-turn-result.v1"
CAP_REPLAY_BRANCH = "replay.branch.v1"
CAP_SKILL_BUNDLE = "skill.bundle.v1"


class ReplayProtocolError(ValueError):
    """Raised when a Replay wire payload violates its declared contract."""


# Internal compatibility name for the migrated transport adapters.
AgentProtocolError = ReplayProtocolError


def _require_supported_version(value: Any) -> str:
    version = str(value or "").strip()
    if version and version.split(".", 1)[0] != "1":
        raise ReplayProtocolError(f"PROTOCOL_VERSION_UNSUPPORTED: {version}")
    return version


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
        raise ReplayProtocolError("replay request must be an object")
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_REQUEST_SCHEMA_V1:
        raise ReplayProtocolError(f"unsupported replay request schema: {schema}")
    request_id = str(payload.get("request_id") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    branch = str(payload.get("branch") or "").strip().lower()
    if not request_id or not job_id:
        raise ReplayProtocolError("replay request_id and job_id are required")
    if branch not in {"baseline", "candidate"}:
        raise ReplayProtocolError("replay branch must be baseline or candidate")
    case = payload.get("case")
    if not isinstance(case, dict) or not str(
        case.get("query") or case.get("instruction") or ""
    ).strip():
        raise ReplayProtocolError("replay case query is required")
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    try:
        timeout_seconds = int(limits.get("timeout_seconds") or 600)
        max_interactions = int(limits.get("max_interactions") or 1)
    except (TypeError, ValueError) as exc:
        raise ReplayProtocolError("replay limits must be integers") from exc
    if not 30 <= timeout_seconds <= 3600:
        raise ReplayProtocolError(
            "replay timeout_seconds must be between 30 and 3600"
        )
    if not 1 <= max_interactions <= 20:
        raise ReplayProtocolError(
            "replay max_interactions must be between 1 and 20"
        )
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
        raise ReplayProtocolError(
            "INVALID_RESPONSE: replay result must be an object"
        )
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_RESULT_SCHEMA_V1:
        raise ReplayProtocolError(
            f"INVALID_RESPONSE: unsupported replay result schema: {schema}"
        )
    request_id = str(payload.get("request_id") or "")
    branch = str(payload.get("branch") or "").lower()
    if request_id != expected_request_id:
        raise ReplayProtocolError("INVALID_RESPONSE: replay request_id mismatch")
    if branch != expected_branch:
        raise ReplayProtocolError("INVALID_RESPONSE: replay branch mismatch")
    status = str(payload.get("status") or "").lower()
    if status not in {"succeeded", "failed", "unsupported"}:
        raise ReplayProtocolError("INVALID_RESPONSE: invalid replay status")
    metrics = (
        payload.get("metrics")
        if isinstance(payload.get("metrics"), dict)
        else {}
    )
    if status == "succeeded":
        for key, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReplayProtocolError(
                    f"INVALID_RESPONSE: metrics.{key} must be a non-negative integer"
                )
    required_metrics = {"tool_call_count", "total_tokens"}
    missing_metrics = sorted(required_metrics - set(metrics))
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
        "metrics_incomplete": bool(missing_metrics),
        "metrics_incomplete_reason": (
            "missing required metrics: " + ", ".join(missing_metrics)
            if status == "succeeded" and missing_metrics
            else ""
        ),
        "error": error,
    }


def normalize_replay_turn_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one turn in a server-driven Replay session."""

    if not isinstance(payload, dict):
        raise ReplayProtocolError("replay turn request must be an object")
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_TURN_REQUEST_SCHEMA_V1:
        raise ReplayProtocolError(
            f"unsupported replay turn request schema: {schema}"
        )
    request_id = str(payload.get("request_id") or "").strip()
    branch = str(payload.get("branch") or "").strip().lower()
    turn_num = payload.get("turn_num")
    prompt = str(payload.get("prompt") or "").strip()
    if not request_id:
        raise ReplayProtocolError("replay turn request_id is required")
    if branch not in {"baseline", "candidate"}:
        raise ReplayProtocolError(
            "replay turn branch must be baseline or candidate"
        )
    if isinstance(turn_num, bool) or not isinstance(turn_num, int) or turn_num < 1:
        raise ReplayProtocolError(
            "replay turn_num must be a positive integer"
        )
    if not prompt:
        raise ReplayProtocolError("replay turn prompt is required")
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    try:
        turn_timeout = int(limits.get("turn_timeout_seconds") or 600)
    except (TypeError, ValueError) as exc:
        raise ReplayProtocolError("replay turn limits must be integers") from exc
    if not 1 <= turn_timeout <= 3600:
        raise ReplayProtocolError(
            "replay turn_timeout_seconds must be between 1 and 3600"
        )
    return {
        **payload,
        "schema_version": REPLAY_TURN_REQUEST_SCHEMA_V1,
        "protocol_version": AGENT_PROTOCOL_VERSION,
        "request_id": request_id,
        "branch": branch,
        "turn_num": turn_num,
        "prompt": prompt,
        "limits": {
            **limits,
            "turn_timeout_seconds": turn_timeout,
        },
    }


def normalize_replay_turn_result(
    payload: dict[str, Any],
    *,
    expected_request_id: str,
    expected_turn_num: int,
) -> dict[str, Any]:
    """Validate present metrics; omitted metrics remain unavailable."""

    if not isinstance(payload, dict):
        raise ReplayProtocolError(
            "INVALID_RESPONSE: replay turn result must be an object"
        )
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_TURN_RESULT_SCHEMA_V1:
        raise ReplayProtocolError(
            f"INVALID_RESPONSE: unsupported replay turn result schema: {schema}"
        )
    request_id = str(payload.get("request_id") or "")
    turn_num = payload.get("turn_num")
    if request_id != expected_request_id:
        raise ReplayProtocolError(
            "INVALID_RESPONSE: replay turn request_id mismatch"
        )
    if (
        isinstance(turn_num, bool)
        or not isinstance(turn_num, int)
        or turn_num != expected_turn_num
    ):
        raise ReplayProtocolError("INVALID_RESPONSE: replay turn_num mismatch")
    status = str(payload.get("status") or "").lower()
    if status not in {"succeeded", "failed", "unsupported"}:
        raise ReplayProtocolError(
            "INVALID_RESPONSE: invalid replay turn status"
        )
    metrics = (
        payload.get("metrics")
        if isinstance(payload.get("metrics"), dict)
        else {}
    )
    if status == "succeeded":
        for key, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReplayProtocolError(
                    f"INVALID_RESPONSE: metrics.{key} must be a non-negative integer"
                )
    required_metrics = {"tool_call_count", "total_tokens"}
    missing_metrics = sorted(required_metrics - set(metrics))
    error = payload.get("error")
    if status != "succeeded" and not isinstance(error, dict):
        error = {
            "code": "EXECUTION_FAILED",
            "message": str(error or "replay turn failed"),
            "retryable": False,
        }
    return {
        **payload,
        "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
        "protocol_version": AGENT_PROTOCOL_VERSION,
        "request_id": request_id,
        "turn_num": turn_num,
        "status": status,
        "metrics": dict(metrics),
        "metrics_incomplete": bool(missing_metrics),
        "metrics_incomplete_reason": (
            "missing required metrics: " + ", ".join(missing_metrics)
            if status == "succeeded" and missing_metrics
            else ""
        ),
        "error": error,
    }
