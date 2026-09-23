"""Customer execution interface. Evaluation requirements stay inside Replay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol


class ReplayUnsupported(RuntimeError):
    """The runtime cannot provide isolated, comparable execution."""


@dataclass(frozen=True)
class ReplayTreatment:
    branch: Literal["baseline", "candidate"]
    skill: Mapping[str, Any] | None


@dataclass(frozen=True)
class ReplayContext:
    request_id: str
    runtime_type: str
    treatment: ReplayTreatment
    materials: tuple[Mapping[str, Any], ...]
    context_snapshot: Mapping[str, Any]
    timeout_seconds: int


@dataclass(frozen=True)
class AgentObservation:
    response: str
    messages: tuple[Mapping[str, Any], ...] = ()
    artifacts: tuple[Mapping[str, Any] | str, ...] = ()
    metrics: Mapping[str, int | float] | None = None
    trace_id: str = ""
    metrics_incomplete_reason: str = ""


class ReplaySession(Protocol):
    def send(self, user_message: str) -> AgentObservation: ...

    def close(self) -> None: ...


class ReplayAdapterFactory(Protocol):
    def open(self, context: ReplayContext) -> ReplaySession: ...


RUNTIME_METRICS = (
    "tool_call_count", "total_tokens", "input_tokens", "output_tokens", "api_calls",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)


def validate_observation(value: Any) -> AgentObservation:
    if not isinstance(value, AgentObservation) or not isinstance(value.response, str) or not value.response.strip():
        raise ValueError("send() must return AgentObservation with a nonempty response")
    if any(not isinstance(message, Mapping) for message in value.messages):
        raise ValueError("observation messages must be mappings")
    if any(not isinstance(artifact, (Mapping, str)) for artifact in value.artifacts):
        raise ValueError("observation artifacts must be mappings or strings")
    if value.metrics is not None and not isinstance(value.metrics, Mapping):
        raise ValueError("observation metrics must be a mapping")
    for key, number in (value.metrics or {}).items():
        if key not in RUNTIME_METRICS:
            raise ValueError(f"unsupported runtime metric: {key}")
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            raise ValueError(f"invalid runtime metric: {key}")
    return value
