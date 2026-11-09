"""Isolated True Replay on the existing AgentScope Java customer Runtime.

The authenticated service captures and forwards final model bytes in its own
transport, then signs branch/turn/nonce-bound dispatches. No model credentials,
Checklist or evaluator instructions are sent through this Adapter.
"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

import httpx

from team_replay.execution_plan import execution_plan_hash, require_execution_capabilities
from team_replay.hooks import AgentObservation, ReplayUnsupported
from team_replay.request_capture import frozen_input_messages, make_execution_receipt
from team_replay.variable_context import inline_variable_binding, project_inline_plan

REPLAY_ADAPTER = {
    "label": "Existing Agent Lab — AgentScope Java customer",
    "enabled": True,
    "capabilities": {
        "execution_plan": "teamevolver.execution-plan.v1",
        "component_kinds": ["agent_prompt", "context_fragment"],
        "request_observation": "remote_attested_dispatch",
        "skill_treatment": False,
    },
}


def build_replay_adapter(config):
    values = dict(config) if isinstance(config, Mapping) else vars(config)
    return AgentLabFactory(values.get("agent_lab", {}))


class AgentLabFactory:
    capabilities = REPLAY_ADAPTER["capabilities"]

    def __init__(self, config, *, client_factory=None):
        self.config = copy.deepcopy(config)
        self.client_factory = client_factory or httpx.Client

    def open(self, context):
        require_execution_capabilities(self, context.execution_plan)
        if context.runtime_type != "agentscope-java" or context.execution_plan is None or context.treatment.skill:
            raise ReplayUnsupported("Agent Lab requires its pure Prompt execution plan without Skill treatment")
        config, plan = self.config, context.execution_plan
        transport_plan = None
        if any(inline_variable_binding(part) is not None for part in plan["components"]):
            try:
                transport_plan = project_inline_plan(plan)
            except ValueError as exc:
                raise ReplayUnsupported(str(exc)) from exc
        wire_plan = transport_plan if transport_plan is not None else plan
        endpoint = str(config.get("endpoint", "")).rstrip("/")
        address = urlsplit(endpoint)
        if address.scheme not in {"http", "https"} or not address.hostname or (
            address.username or address.password or address.query or address.fragment
        ) or not config.get("control_secret"):
            raise ReplayUnsupported("Agent Lab endpoint/control Secret Ref is unavailable")
        agent = plan["binding"].get("agent_id")
        if agent not in {"agent-one", "agent-two"} or plan["binding"].get("runtime_type") != "agentscope-java":
            raise ReplayUnsupported("Agent Lab execution profile binding mismatch")
        client = self.client_factory(
            base_url=endpoint, timeout=context.timeout_seconds,
            headers={"Authorization": "Bearer " + config["control_secret"]},
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        try:
            response = client.post(f"/api/agents/{agent}/replay-sessions", json={
                "schema_version": "teamevolver.agent-lab-replay.v1", "request_id": context.request_id,
                "run_id": context.request_id, "branch": context.treatment.branch,
                "execution_plan": copy.deepcopy(wire_plan), "plan_hash": execution_plan_hash(wire_plan),
                "frozen_messages": frozen_input_messages(context),
            })
            response.raise_for_status()
            result = response.json()
            if (not isinstance(result.get("id"), str) or not result["id"]
                    or result.get("plan_hash") != execution_plan_hash(wire_plan)
                    or not isinstance(result.get("runtime_fingerprint"), str) or not result["runtime_fingerprint"]):
                raise ReplayUnsupported("Agent Lab did not freeze the requested execution plan")
            return AgentLabSession(context, client, config["control_secret"], agent, result["id"],
                                   result["runtime_fingerprint"], client_factory=self.client_factory,
                                   transport_plan=transport_plan)
        except BaseException:
            client.close()
            raise


class AgentLabSession:
    def __init__(self, context, client, secret, agent, session_id, runtime_fingerprint, *, client_factory=httpx.Client,
                 transport_plan=None):
        self.context, self.client, self.secret = context, client, secret
        self.path = f"/api/agents/{agent}/replay-sessions/{quote(session_id, safe='')}"
        self.session_id, self.turn_num, self.closed = session_id, 0, False
        self.runtime_fingerprint = runtime_fingerprint
        self.transport_plan = copy.deepcopy(transport_plan)
        self.client_factory = client_factory
        self.cleanup_started, self.cleanup_error = False, ""

    def send(self, user_message):
        if self.closed or self.cleanup_started:
            raise ReplayUnsupported("Agent Lab Replay Session is closed")
        self.turn_num += 1
        nonce = uuid.uuid4().hex
        response = self.client.post(self.path + "/messages", json={
            "text": user_message, "turn_num": self.turn_num, "nonce": nonce,
        })
        response.raise_for_status()
        result = response.json()
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ReplayUnsupported("Agent Lab returned no completed assistant response")
        ids = self.context.request_observer.accept_attested(
            result.get("execution_receipt"), secret=self.secret, context=self.context,
            turn_num=self.turn_num, session_id=self.session_id, nonce=nonce,
            output=text, runtime_fingerprint=self.runtime_fingerprint,
            transport_plan=self.transport_plan,
        )
        records = [r for r in self.context.request_observer.records() if r["dispatch_id"] in ids]
        metrics = {"tool_call_count": 0, "api_calls": len(ids)}
        for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                               ("total_tokens", "total_tokens")):
            values = [(r.get("usage") or {}).get(source) for r in records]
            if all(type(v) is int and v >= 0 for v in values):
                metrics[target] = sum(values)
        return AgentObservation(
            response=text, messages=({"role": "assistant", "content": text},), metrics=metrics,
            execution_receipt=make_execution_receipt(self.context, self.turn_num, ids),
        )

    def close(self):
        if self.closed:
            return
        if self.cleanup_error:
            raise ReplayUnsupported("Agent Lab cleanup previously failed; remote Session closure is unconfirmed")
        self.cleanup_started = True
        timeout = min(10.0, self.context.timeout_seconds)
        try:
            try:
                response = self.client.delete(self.path, timeout=timeout, headers={"Connection": "close"})
            except httpx.TransportError:
                # A DELETE may not have arrived, or its acknowledgement may have
                # been lost. Only cleanup gets one fresh-connection retry.
                self.client.close()
                with self.client_factory(
                    base_url=str(self.client.base_url), timeout=timeout,
                    headers={"Authorization": "Bearer " + self.secret, "Connection": "close"},
                    limits=httpx.Limits(max_keepalive_connections=0),
                ) as fresh:
                    response = fresh.delete(self.path)
            self._confirm_cleanup(response)
            self.closed = True
        except BaseException as exc:
            self.cleanup_error = type(exc).__name__
            raise
        finally:
            self.client.close()

    def _confirm_cleanup(self, response):
        if (response.status_code == 404 and response.request.method == "DELETE"
                and response.request.url.path == self.path):
            try:
                if response.json().get("error") == "隔离 Replay 不存在":
                    return
            except (ValueError, AttributeError):
                pass
        response.raise_for_status()
        if response.json().get("closed") is not True:
            raise ReplayUnsupported("Agent Lab did not acknowledge isolated Session cleanup")
