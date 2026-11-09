"""Reference Adapter for a model HTTP endpoint with observable final requests.

This is a transport reference, not an integration with an existing business Agent.
The owner must configure a frozen plan environment.request (model/tools/options)
and an endpoint accepting that JSON plus messages. Tests inject the HTTP transport;
this module never calls a model merely by being loaded. Credentials are read from
the existing Replay auth profile and excluded from captured request evidence.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping

import httpx

from team_replay.adapters import resolve_replay_api_key
from team_replay.execution_plan import EXECUTION_PLAN_SCHEMA, require_execution_capabilities
from team_replay.hooks import AgentObservation, ReplayUnsupported
from team_replay.request_capture import make_execution_receipt, materialize_messages

REPLAY_ADAPTER = {
    "label": "Observed model HTTP — reference transport",
    "enabled": True,
    "capabilities": {
        "execution_plan": "teamevolver.execution-plan.v1",
        "component_kinds": ["agent_prompt", "context_fragment"],
        "request_observation": "local_dispatch",
        "skill_treatment": True,
    },
}


def build_replay_adapter(config):
    values = dict(config) if isinstance(config, Mapping) else vars(config)
    return ObservedHttpFactory(values.get("observed_http", values))


class ObservedHttpFactory:
    capabilities = REPLAY_ADAPTER["capabilities"]

    def __init__(self, config, *, post=None):
        self.config = copy.deepcopy(dict(config))
        self.post = post or httpx.post

    def open(self, context):
        require_execution_capabilities(self, context.execution_plan)
        if context.execution_plan is None or context.execution_plan["schema_version"] != EXECUTION_PLAN_SCHEMA:
            raise ReplayUnsupported("reference transport requires a complete frozen execution_plan")
        if not self.config.get("endpoint") or context.runtime_type != self.config.get("runtime_type", "observed_http"):
            raise ReplayUnsupported("reference transport endpoint/runtime binding is unavailable")
        binding_runtime = context.execution_plan["binding"].get("runtime_type")
        if binding_runtime and binding_runtime != context.runtime_type:
            raise ReplayUnsupported("execution_plan runtime binding mismatch")
        request = context.execution_plan["environment"].get("request")
        if not isinstance(request, dict) or "messages" in request:
            raise ReplayUnsupported("reference transport requires frozen environment.request without messages")
        return ObservedHttpSession(context, self.config, self.post)


class ObservedHttpSession:
    def __init__(self, context, config, post):
        self.context, self.config, self.post = context, config, post
        self.history = []
        self.turn_num = 0
        self.closed = False

    def send(self, user_message):
        if self.closed:
            raise RuntimeError("Replay Session is closed")
        self.turn_num += 1
        body = copy.deepcopy(self.context.execution_plan["environment"]["request"])
        body["messages"] = materialize_messages(self.context, user_message, self.history)
        headers = {"Content-Type": "application/json"}
        key = resolve_replay_api_key(str(self.config.get("auth_profile") or ""))
        if key:
            headers["Authorization"] = "Bearer " + key

        def transport(raw):
            response = self.post(
                self.config["endpoint"], content=raw, headers=headers, timeout=self.context.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        payload, identity = self.context.request_observer.dispatch(body, transport, destination=self.config["endpoint"])
        try:
            response = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("model endpoint returned no assistant content") from exc
        usage = payload.get("usage") or {}
        metrics = {"tool_call_count": 0, "api_calls": 1}
        for source, target in (("total_tokens", "total_tokens"), ("prompt_tokens", "input_tokens"),
                               ("completion_tokens", "output_tokens")):
            if source in usage:
                metrics[target] = usage[source]
        self.history.extend([{"role": "user", "content": user_message}, {"role": "assistant", "content": response}])
        return AgentObservation(
            response=response, messages=({"role": "assistant", "content": response},), metrics=metrics,
            execution_receipt=make_execution_receipt(self.context, self.turn_num, [identity]),
        )

    def close(self):
        self.closed = True
