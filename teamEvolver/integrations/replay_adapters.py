"""Transport-neutral branch replay adapters."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

from .agent_protocol import (
    AgentProtocolError,
    REPLAY_RESULT_SCHEMA_V1,
    normalize_replay_request,
    normalize_replay_result,
)
from .context_workspace import stable_hash


class ReplayAdapter(Protocol):
    def execute_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute one baseline or candidate branch."""


def resolve_replay_api_key(
    auth_profile: str,
    *,
    legacy_agentshub: bool = False,
) -> str:
    profile = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        str(auth_profile or "").strip(),
    ).strip("_").upper()
    if profile:
        key = str(
            os.environ.get(
                f"TEAMEVOLVER_AGENT_{profile}_REPLAY_API_KEY",
                "",
            )
            or ""
        ).strip()
        if key:
            return key
    if legacy_agentshub:
        return str(os.environ.get("AGENTSHUB_REPLAY_API_KEY") or "").strip()
    return ""


def _failed_result(
    request: dict[str, Any],
    *,
    runtime: str,
    code: str,
    message: str,
    retryable: bool,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": REPLAY_RESULT_SCHEMA_V1,
        "protocol_version": "1.0",
        "request_id": request["request_id"],
        "branch": request["branch"],
        "runtime": {"type": runtime},
        "status": "failed",
        "metrics": {},
        "output": {"final_response": ""},
        "trace": {"messages": [], "events": [], "interactions": []},
        "artifacts": [],
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
    }


@dataclass
class HttpReplayAdapter:
    endpoint: str
    runtime_type: str
    auth_profile: str = ""
    api_key: str = ""
    post: Callable[..., httpx.Response] | None = None

    def execute_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_replay_request(request)
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        api_key = self.api_key or resolve_replay_api_key(self.auth_profile)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = int(normalized["limits"]["timeout_seconds"])
        try:
            response = (self.post or httpx.post)(
                self.endpoint,
                json=normalized,
                headers=headers,
                timeout=max(60, timeout + 30),
            )
            response.raise_for_status()
            payload = response.json()
            return normalize_replay_result(
                payload,
                expected_request_id=normalized["request_id"],
                expected_branch=normalized["branch"],
            )
        except AgentProtocolError as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="INVALID_RESPONSE",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except httpx.TimeoutException as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="TIMEOUT",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="HTTP_ERROR",
                message=f"{type(exc).__name__}: {exc}",
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )


@dataclass
class LegacyAgentsHubHttpAdapter(HttpReplayAdapter):
    """One-cycle compatibility adapter for the existing AgentsHub endpoint."""

    def execute_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_replay_request(request)
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        api_key = self.api_key or resolve_replay_api_key(
            self.auth_profile,
            legacy_agentshub=True,
        )
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = int(normalized["limits"]["timeout_seconds"])
        case = normalized["case"]
        try:
            response = (self.post or httpx.post)(
                self.endpoint,
                json={
                    "branch": normalized["branch"],
                    "instruction": str(
                        case.get("query") or case.get("instruction") or ""
                    ),
                    "target_skill_name": normalized.get("target_skill_name") or "",
                    "skill": normalized.get("skill"),
                    "current_skill": normalized.get("current_skill"),
                    "source_session": normalized.get("source_session") or {},
                    "case": case,
                    "timeout_seconds": timeout,
                    "max_interactions": int(
                        normalized["limits"]["max_interactions"]
                    ),
                },
                headers=headers,
                timeout=max(60, timeout + 30),
            )
            response.raise_for_status()
            legacy = response.json()
            if not isinstance(legacy, dict):
                raise AgentProtocolError("legacy replay returned a non-object")
            status = "succeeded" if legacy.get("ok") else "failed"
            payload = {
                "schema_version": REPLAY_RESULT_SCHEMA_V1,
                "protocol_version": "1.0",
                "request_id": normalized["request_id"],
                "branch": normalized["branch"],
                "runtime": {
                    "type": str(
                        legacy.get("runtime") or self.runtime_type or "agentshub"
                    )
                },
                "status": status,
                "metrics": {
                    key: legacy.get(key)
                    for key in (
                        "interaction_turns",
                        "tool_call_count",
                        "total_tokens",
                        "api_calls",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "reasoning_tokens",
                    )
                    if legacy.get(key) is not None
                },
                "output": {
                    "final_response": str(legacy.get("final_response") or "")
                },
                "trace": {
                    "messages": list(legacy.get("messages") or []),
                    "events": list(legacy.get("events") or []),
                    "interactions": list(legacy.get("interactions") or []),
                },
                "artifacts": list(legacy.get("artifacts") or []),
                "runtime_checklist_report": legacy.get("checklist_report") or {},
                "checklist_evidence": legacy.get("checklist_evidence") or {},
                "context_input_hash": str(
                    legacy.get("context_input_hash")
                    or stable_hash(normalized.get("context_snapshot") or {})
                ),
                "context_usage": legacy.get("context_usage") or {},
                "execution_manifest_hash": str(
                    legacy.get("execution_manifest_hash")
                    or stable_hash(normalized.get("execution_manifest") or {})
                ),
                "error": (
                    None
                    if status == "succeeded"
                    else {
                        "code": "EXECUTION_FAILED",
                        "message": str(
                            legacy.get("error") or "legacy replay branch failed"
                        ),
                        "retryable": False,
                    }
                ),
                "elapsed_seconds": legacy.get(
                    "elapsed_seconds",
                    round(time.monotonic() - started, 3),
                ),
            }
            return normalize_replay_result(
                payload,
                expected_request_id=normalized["request_id"],
                expected_branch=normalized["branch"],
            )
        except AgentProtocolError as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="INVALID_RESPONSE",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except httpx.TimeoutException as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="TIMEOUT",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="HTTP_ERROR",
                message=f"{type(exc).__name__}: {exc}",
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )


@dataclass
class LocalReplayAdapter:
    runtime_type: str
    runner: Callable[[dict[str, Any]], dict[str, Any]]

    def execute_branch(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_replay_request(request)
        started = time.monotonic()
        try:
            legacy = self.runner(normalized)
            payload = {
                "schema_version": REPLAY_RESULT_SCHEMA_V1,
                "protocol_version": "1.0",
                "request_id": normalized["request_id"],
                "branch": normalized["branch"],
                "runtime": {"type": self.runtime_type},
                "status": "succeeded" if legacy.get("ok") else "failed",
                "metrics": {
                    key: legacy.get(key)
                    for key in (
                        "interaction_turns",
                        "tool_call_count",
                        "total_tokens",
                        "api_calls",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "reasoning_tokens",
                    )
                    if legacy.get(key) is not None
                },
                "output": {
                    "final_response": str(legacy.get("final_response") or "")
                },
                "trace": {
                    "messages": list(legacy.get("messages") or []),
                    "events": list(legacy.get("events") or []),
                    "interactions": list(legacy.get("interactions") or []),
                },
                "artifacts": list(legacy.get("artifacts") or []),
                "runtime_checklist_report": legacy.get("checklist_report") or {},
                "error": (
                    None
                    if legacy.get("ok")
                    else {
                        "code": "EXECUTION_FAILED",
                        "message": str(legacy.get("error") or "local replay failed"),
                        "retryable": False,
                    }
                ),
                "elapsed_seconds": legacy.get(
                    "elapsed_seconds",
                    round(time.monotonic() - started, 3),
                ),
            }
            return normalize_replay_result(
                payload,
                expected_request_id=normalized["request_id"],
                expected_branch=normalized["branch"],
            )
        except AgentProtocolError as exc:
            return _failed_result(
                normalized,
                runtime=self.runtime_type,
                code="INVALID_RESPONSE",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )


def legacy_branch_projection(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    runtime = result.get("runtime")
    runtime_type = (
        str(runtime.get("type") or "")
        if isinstance(runtime, dict)
        else str(runtime or "")
    )
    return {
        "branch": result.get("branch"),
        "runtime": runtime_type,
        "ok": result.get("status") == "succeeded",
        "error": str(error.get("message") or ""),
        "error_code": str(error.get("code") or ""),
        "final_response": str(output.get("final_response") or ""),
        "messages": list(trace.get("messages") or []),
        "events": list(trace.get("events") or []),
        "interactions": list(trace.get("interactions") or []),
        "artifacts": list(result.get("artifacts") or []),
        "checklist_report": result.get("runtime_checklist_report") or {},
        "checklist_evidence": result.get("checklist_evidence") or {},
        "context_input_hash": result.get("context_input_hash"),
        "context_usage": result.get("context_usage") or {},
        "execution_manifest_hash": result.get("execution_manifest_hash"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        **metrics,
    }
