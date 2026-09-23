"""Transport-neutral branch replay adapters."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

from ._util import stable_hash
from .artifacts import skill_treatment_content, skill_treatment_members
from .protocol import (
    AGENT_PROTOCOL_VERSION,
    REPLAY_RESULT_SCHEMA_V1,
    REPLAY_TURN_RESULT_SCHEMA_V1,
    AgentProtocolError,
    normalize_replay_request,
    normalize_replay_result,
    normalize_replay_turn_request,
    normalize_replay_turn_result,
)


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


def _failed_turn_result(
    request: dict[str, Any],
    *,
    runtime: str,
    code: str,
    message: str,
    retryable: bool,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
        "protocol_version": AGENT_PROTOCOL_VERSION,
        "request_id": request["request_id"],
        "turn_num": request["turn_num"],
        "branch": request["branch"],
        "runtime": {"type": runtime},
        "status": "failed",
        "metrics": {},
        "final_response": "",
        "messages": [],
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
                timeout=max(1, timeout),
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
class TurnBasedReplayAdapter:
    """Server-driven replay transport: one HTTP call per interaction turn.

    teamEvolver owns the multi-turn loop, checklist judging, progressive
    disclosure, and metric aggregation. The Agent runtime executes exactly
    one turn per call and reports its single-turn trace and usage; it never
    sees the checklist or aggregates metrics itself."""

    endpoint: str
    runtime_type: str
    auth_profile: str = ""
    api_key: str = ""
    post: Callable[..., httpx.Response] | None = None

    def call_turn(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_replay_turn_request(request)
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        api_key = self.api_key or resolve_replay_api_key(self.auth_profile)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = int(normalized["limits"]["turn_timeout_seconds"])
        try:
            response = (self.post or httpx.post)(
                self.endpoint,
                json=normalized,
                headers=headers,
                timeout=max(1, timeout),
            )
            response.raise_for_status()
            payload = response.json()
            return normalize_replay_turn_result(
                payload,
                expected_request_id=normalized["request_id"],
                expected_turn_num=normalized["turn_num"],
            )
        except AgentProtocolError as exc:
            return _failed_turn_result(
                normalized,
                runtime=self.runtime_type,
                code="INVALID_RESPONSE",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except httpx.TimeoutException as exc:
            return _failed_turn_result(
                normalized,
                runtime=self.runtime_type,
                code="TIMEOUT",
                message=str(exc),
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            return _failed_turn_result(
                normalized,
                runtime=self.runtime_type,
                code="HTTP_ERROR",
                message=f"{type(exc).__name__}: {exc}",
                retryable=False,
                elapsed_seconds=time.monotonic() - started,
            )


def render_template(template: Any, values: dict[str, Any]) -> Any:
    """Render a customer request template against per-turn values.

    ``"{{key}}"`` as a WHOLE string value is replaced by the value itself
    (keeping JSON types — lists/objects pass through); ``{{key}}`` inside a
    larger string is interpolated with its string form. Unknown keys render
    as empty string / empty containers so a template never crashes a turn."""
    if isinstance(template, dict):
        return {key: render_template(item, values) for key, item in template.items()}
    if isinstance(template, list):
        return [render_template(item, values) for item in template]
    if isinstance(template, str):
        whole = template.strip()
        if whole.startswith("{{") and whole.endswith("}}") and whole[2:-2].strip():
            value = values.get(whole[2:-2].strip())
            return value if value is not None else ("" if isinstance(value, str) else value)
        for key, value in values.items():
            template = template.replace(
                "{{" + key + "}}",
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            )
        return template
    return template


def extract_path(data: Any, path: str) -> Any:
    """Extract a value via a dotted path (``usage.total_tokens``, ``items.0``)."""
    current = data
    for part in str(path or "").strip().split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


_SUCCESS_STATUS_VALUES = {"succeeded", "success", "ok", "done", "true", "completed"}
_UNSUPPORTED_STATUS_VALUES = {"unsupported", "not_supported", "skipped"}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class MappedHttpAdapter:
    """Adapter for a customer's PLAIN HTTP agent endpoint (zero agent-side
    awareness of teamEvolver).

    The customer factory supplies a request template and response field mapping;
    this adapter renders each turn into the customer's own request shape and
    extracts the trace/metrics from the customer's own response shape. The
    Agent never sees the checklist, registration, or replay protocol."""

    endpoint: str
    runtime_type: str
    request_template: dict[str, Any]
    response_mapping: dict[str, Any]
    auth_profile: str = ""
    api_key: str = ""
    post: Callable[..., httpx.Response] | None = None

    _TURN_VALUES = (
        "request_id",
        "turn_num",
        "branch",
        "prompt",
        "history",
        "context_snapshot",
        "materials",
        "tool_policy",
    )

    def _render_body(self, request: dict[str, Any]) -> dict[str, Any]:
        skill = request.get("skill") if isinstance(request.get("skill"), dict) else {}
        values: dict[str, Any] = {
            key: request.get(key)
            for key in self._TURN_VALUES
        }
        values["history"] = request.get("history") if request.get("history") is not None else []
        values["context_snapshot"] = (
            request.get("context_snapshot")
            if request.get("context_snapshot") is not None
            else {}
        )
        values["materials"] = request.get("materials") if request.get("materials") is not None else []
        values["tool_policy"] = (
            request.get("tool_policy") if request.get("tool_policy") is not None else {}
        )
        skills = (
            [dict(item) for item in request.get("skills") or []]
            if isinstance(request.get("skills"), list)
            else skill_treatment_members(skill)
        )
        values["skill"] = skill
        values["skills"] = skills
        values["skill_content"] = skill_treatment_content(skill)
        values["skill_name"] = (
            str(skills[0].get("name") or "")
            if len(skills) == 1
            else ""
        )
        values["timeout_seconds"] = int(
            (request.get("limits") or {}).get("turn_timeout_seconds") or 600
        )
        body = render_template(self.request_template, values)
        return body if isinstance(body, dict) else {"payload": body}

    def _extract(self, payload: Any, key: str, default: Any = None) -> Any:
        mapping = self.response_mapping if isinstance(self.response_mapping, dict) else {}
        path = str(mapping.get(key) or "").strip()
        if not path:
            return default
        value = extract_path(payload, path)
        return default if value is None else value

    def call_turn(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute one turn against the plain customer endpoint and return a
        turn result shaped like the server-driven protocol result."""
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        api_key = self.api_key or resolve_replay_api_key(self.auth_profile)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = int((request.get("limits") or {}).get("turn_timeout_seconds") or 600)
        turn_num = int(request.get("turn_num") or 0)
        request_id = str(request.get("request_id") or "")

        def result(
            *,
            status: str,
            final_response: str = "",
            messages: list | None = None,
            metrics: dict[str, Any] | None = None,
            artifacts: list | None = None,
            metrics_incomplete: bool = False,
            error: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return {
                "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
                "protocol_version": AGENT_PROTOCOL_VERSION,
                "request_id": request_id,
                "turn_num": turn_num,
                "branch": str(request.get("branch") or ""),
                "runtime": {"type": self.runtime_type},
                "status": status,
                "final_response": final_response,
                "messages": messages or [],
                "artifacts": artifacts or [],
                "metrics": metrics or {},
                "metrics_incomplete": metrics_incomplete,
                "error": error,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

        try:
            response = (self.post or httpx.post)(
                self.endpoint,
                json=self._render_body(request),
                headers=headers,
                timeout=max(1, timeout),
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            return result(
                status="failed",
                error={"code": "TIMEOUT", "message": str(exc), "retryable": False},
            )
        except Exception as exc:  # noqa: BLE001
            return result(
                status="failed",
                error={
                    "code": "HTTP_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                    "retryable": False,
                },
            )

        status_raw = str(self._extract(payload, "status", "") or "").strip().lower()
        if status_raw in _UNSUPPORTED_STATUS_VALUES:
            return result(
                status="unsupported",
                error={
                    "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
                    "message": str(
                        self._extract(payload, "error.message", "")
                        or self._extract(payload, "error", "")
                        or "agent cannot replay this turn"
                    ),
                    "retryable": False,
                },
            )
        if status_raw and status_raw not in _SUCCESS_STATUS_VALUES:
            return result(
                status="failed",
                error={
                    "code": "EXECUTION_FAILED",
                    "message": str(
                        self._extract(payload, "error.message", "")
                        or self._extract(payload, "error", "")
                        or f"agent status: {status_raw}"
                    ),
                    "retryable": False,
                },
            )

        raw_messages = self._extract(payload, "messages", [])
        messages = [item for item in raw_messages if isinstance(item, dict)] if isinstance(
            raw_messages, list
        ) else []
        tool_call_count = _as_int(self._extract(payload, "tool_call_count"))
        total_tokens = _as_int(self._extract(payload, "total_tokens"))
        metrics_incomplete = tool_call_count is None or total_tokens is None
        metrics = {
            key: value for key, value in {"tool_call_count": tool_call_count, "total_tokens": total_tokens}.items()
            if value is not None
        }
        for key in ("input_tokens", "output_tokens", "api_calls"):
            value = _as_int(self._extract(payload, key))
            if value is not None:
                metrics[key] = value
        return result(
            status="succeeded",
            final_response=str(self._extract(payload, "final_response", "") or ""),
            messages=messages,
            artifacts=self._extract(payload, "artifacts", []),
            metrics=metrics,
            metrics_incomplete=metrics_incomplete,
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
