"""Versioned wire contracts used by Replay-capable Agent runtimes."""

from __future__ import annotations

from typing import Any

REPLAY_PROTOCOL_VERSION = "1.0"
REPLAY_TURN_REQUEST_SCHEMA = "teamevolver.replay-turn-request.v1"
REPLAY_TURN_RESULT_SCHEMA = "teamevolver.replay-turn-result.v1"


class ReplayProtocolError(ValueError):
    """Raised when a Replay wire payload violates its declared contract."""


def _require_supported_version(value: Any) -> str:
    version = str(value or "").strip()
    if version and version.split(".", 1)[0] != "1":
        raise ReplayProtocolError(f"PROTOCOL_VERSION_UNSUPPORTED: {version}")
    return version


def normalize_replay_turn_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one turn in a server-driven Replay session."""

    if not isinstance(payload, dict):
        raise ReplayProtocolError("replay turn request must be an object")
    _require_supported_version(payload.get("protocol_version") or "1.0")
    schema = str(payload.get("schema_version") or "").strip().lower()
    if schema != REPLAY_TURN_REQUEST_SCHEMA:
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
        "schema_version": REPLAY_TURN_REQUEST_SCHEMA,
        "protocol_version": REPLAY_PROTOCOL_VERSION,
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
    if schema != REPLAY_TURN_RESULT_SCHEMA:
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
        "schema_version": REPLAY_TURN_RESULT_SCHEMA,
        "protocol_version": REPLAY_PROTOCOL_VERSION,
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
