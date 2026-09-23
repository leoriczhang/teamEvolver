"""Built-in transports exposed through the single customer factory interface."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .adapters import HttpReplayAdapter, MappedHttpAdapter, TurnBasedReplayAdapter
from .artifacts import skill_treatment_members
from .hooks import AgentObservation, ReplayContext, ReplayUnsupported, validate_observation
from .protocol import REPLAY_REQUEST_SCHEMA_V1, REPLAY_TURN_REQUEST_SCHEMA_V1


class TurnSession:
    def __init__(self, context: ReplayContext, adapter: Any):
        self.context = copy.deepcopy(context)
        self.adapter = adapter
        self.history: list[dict[str, Any]] = []
        self.deadline = time.monotonic() + context.timeout_seconds
        self.closed = False

    def send(self, user_message: str) -> AgentObservation:
        if self.closed:
            raise RuntimeError("Replay session is closed")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Replay session timed out")
        request = {
            "schema_version": REPLAY_TURN_REQUEST_SCHEMA_V1, "protocol_version": "1.0",
            "request_id": self.context.request_id, "turn_num": len(self.history) + 1,
            "branch": self.context.treatment.branch, "prompt": user_message,
            "history": copy.deepcopy(self.history),
            "limits": {"turn_timeout_seconds": max(1, int(remaining))},
        }
        if not self.history or isinstance(self.adapter, MappedHttpAdapter):
            request.update(
                skill=copy.deepcopy(self.context.treatment.skill),
                skills=copy.deepcopy(
                    skill_treatment_members(self.context.treatment.skill)
                ),
                context_snapshot=copy.deepcopy(self.context.context_snapshot),
                materials=copy.deepcopy(list(self.context.materials)),
            )
        result = self.adapter.call_turn(request)
        error = result.get("error") or {}
        if result.get("status") == "unsupported":
            raise ReplayUnsupported(str(error.get("message") or "runtime cannot execute this branch"))
        if result.get("status") != "succeeded":
            raise RuntimeError(str(error.get("message") or "runtime execution failed"))
        observation = validate_observation(AgentObservation(
            response=result.get("final_response"),
            messages=tuple(result.get("messages") or []),
            artifacts=tuple(result.get("artifacts") or []),
            metrics=result.get("metrics"),
            trace_id=str(result.get("trace_id") or ""),
            metrics_incomplete_reason=str(
                result.get("metrics_incomplete_reason") or ""
            ),
        ))
        self.history.append({"turn_num": len(self.history) + 1, "prompt": user_message, "response": observation.response})
        return observation

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        close = getattr(self.adapter, "close", None)
        if callable(close):
            close()


@dataclass
class TurnBasedReplayFactory:
    endpoint: str
    runtime_type: str
    isolated_sessions: bool = False
    auth_profile: str = ""
    api_key: str = ""
    post: Callable | None = None

    def open(self, context: ReplayContext) -> TurnSession:
        if not self.isolated_sessions:
            raise ReplayUnsupported("runtime must guarantee isolation by request_id and fixed model/tools")
        return TurnSession(context, TurnBasedReplayAdapter(
            self.endpoint, self.runtime_type, self.auth_profile, self.api_key, self.post,
        ))


@dataclass
class MappedHttpReplayFactory(TurnBasedReplayFactory):
    request_template: dict[str, Any] = field(default_factory=dict)
    response_mapping: dict[str, Any] = field(default_factory=dict)

    def open(self, context: ReplayContext) -> TurnSession:
        if not self.isolated_sessions:
            raise ReplayUnsupported("mapped runtime must guarantee isolated sessions and fixed model/tools")
        rendered = str(self.request_template)
        if "{{request_id}}" not in rendered:
            raise ReplayUnsupported("mapped runtime must accept a separate request_id for each branch")
        if context.treatment.skill and not any(
            marker in rendered
            for marker in ("{{skill}}", "{{skills}}", "{{skill_content}}")
        ):
            raise ReplayUnsupported("mapped request does not apply the Skill treatment")
        if context.materials and "{{materials}}" not in rendered:
            raise ReplayUnsupported("mapped request does not pass fixed materials")
        if context.context_snapshot and "{{context_snapshot}}" not in rendered:
            raise ReplayUnsupported("mapped request does not pass fixed context")
        return TurnSession(context, MappedHttpAdapter(
            endpoint=self.endpoint, runtime_type=self.runtime_type,
            request_template=copy.deepcopy(self.request_template),
            response_mapping=copy.deepcopy(self.response_mapping),
            auth_profile=self.auth_profile, api_key=self.api_key, post=self.post,
        ))


@dataclass
class DeapReplayFactory:
    endpoint: str
    employee_no: str = ""
    auth_profile: str = ""
    client_factory: Callable | None = None

    def open(self, context: ReplayContext) -> TurnSession:
        from .deap import DeapReplayAdapter

        if context.materials or context.context_snapshot:
            raise ReplayUnsupported("DEAP transport does not support injecting fixed materials/context")
        return TurnSession(context, DeapReplayAdapter(
            self.endpoint, employee_no=self.employee_no, auth_profile=self.auth_profile,
            client=self.client_factory() if self.client_factory else None,
        ))


class LegacyBranchSession:
    """One-turn migration bridge. Never exposes an evaluation Checklist."""

    def __init__(self, context: ReplayContext, adapter: HttpReplayAdapter):
        self.context = copy.deepcopy(context)
        self.adapter = adapter
        self.sent = False
        self.closed = False

    def send(self, user_message: str) -> AgentObservation:
        if self.sent or self.closed:
            raise ReplayUnsupported("legacy branch transport cannot resume; bind a turn-based factory")
        self.sent = True
        result = self.adapter.execute_branch({
            "schema_version": REPLAY_REQUEST_SCHEMA_V1, "protocol_version": "1.0",
            "request_id": self.context.request_id, "job_id": self.context.request_id,
            "branch": self.context.treatment.branch, "skill": self.context.treatment.skill,
            "skills": skill_treatment_members(self.context.treatment.skill),
            "case": {"query": user_message, "materials": list(self.context.materials)},
            "context_snapshot": self.context.context_snapshot,
            "limits": {"timeout_seconds": self.context.timeout_seconds, "max_interactions": 1},
        })
        if result.get("status") != "succeeded":
            raise ReplayUnsupported(str((result.get("error") or {}).get("message") or "legacy execution failed"))
        metrics = {key: value for key, value in (result.get("metrics") or {}).items() if key != "interaction_turns"}
        return validate_observation(AgentObservation(
            response=(result.get("output") or {}).get("final_response"),
            messages=tuple((result.get("trace") or {}).get("messages") or []),
            artifacts=tuple(result.get("artifacts") or []), metrics=metrics,
        ))

    def close(self) -> None:
        self.closed = True


@dataclass
class LegacyBranchFactory:
    adapter_builder: Callable[[], HttpReplayAdapter]
    isolated_sessions: bool = False

    def open(self, context: ReplayContext) -> LegacyBranchSession:
        if not self.isolated_sessions:
            raise ReplayUnsupported("legacy runtime must guarantee isolated branch execution")
        return LegacyBranchSession(context, self.adapter_builder())
