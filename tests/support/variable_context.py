"""Versioned variable-Context fixtures, independent of business datasets."""
from __future__ import annotations

import copy

from team_replay.execution_inputs import EXECUTION_INPUTS_SCHEMA, TEMPLATE_INPUT_CONTRACT_SCHEMA
from team_replay.execution_plan import EXECUTION_PLAN_SCHEMA, normalize_execution_plan
from team_replay.variable_context import source_value_hash


def inline_plan():
    def part(identity, kind, order, content, binding=None):
        return {"component_id": identity, "kind": kind, "slot": "system" if order == 0 else "context",
                "order": order, "content": content, "content_hash": source_value_hash(content), "revision": 1,
                "binding": binding or {}}

    plan = {"schema_version": EXECUTION_PLAN_SCHEMA,
            "binding": {"profile_id": "variable-rules", "runtime_type": "observed_http", "agent_id": "agent-one"},
            "target_component_ids": ["rule-a"], "components": [
                part("main", "agent_prompt", 0, "Fixed before|{{rules}}|Fixed after|{{person}}",
                     {"template_id": "shared-template"}),
                part("shared", "context_fragment", 1, "Shared policy"),
                *[part("rule-" + key, "context_fragment", index, text, {"inline_variable": {
                    "component_id": "main", "name": "rules", "source_value_hash": source_value_hash(text)}})
                  for index, (key, text) in enumerate([("a", "Original A"), ("b", "Original B")], start=2)],
            ], "environment": {
                "request": {"model": "test-model", "temperature": 0, "tools": []},
                "template_variables": {"rules": "Original A", "person": "Reader"},
                "template_input_contract": {"schema_version": TEMPLATE_INPUT_CONTRACT_SCHEMA,
                                            "variable_names": ["rules", "person"]},
            }}
    return normalize_execution_plan(plan)


def changed_inline_plan(plan, content="Improved A"):
    result = copy.deepcopy(plan)
    result["components"][2].update(content=content, content_hash=source_value_hash(content), revision="2")
    return result


def inline_case(plan, value="Original A", *, identity="case-a"):
    ref = {"source_session_key": "root-" + identity, "trace_id": "trace-" + identity, "revision": "r1"}
    host = plan["components"][0]
    inputs = {"schema_version": EXECUTION_INPUTS_SCHEMA, "template_refs": {"main": {
        "template_id": host["binding"]["template_id"], "revision": host["revision"],
        "content_hash": host["content_hash"]}}, "template_variables": {"rules": value, "person": "Reader"},
        "input_messages": [{"role": "user", "content": "Original public history"}]}
    session_id = "source-" + identity
    snapshot = {"session_id": session_id, "provenance": {
        "source_session_key": ref["source_session_key"], "target_trace_refs": [ref]}, "turns": [
            {"source_trace_id": ref["trace_id"], "request_context": {"template_inputs": copy.deepcopy(inputs)}}]}
    return {"case_id": identity, "query": "Current request", "skill_ids": [], "materials": [],
            "checks": [{"id": "R1", "kind": "output", "text": "Follow the selected rule"}],
            "provenance": {"session_id": session_id, "source_runtime": {"type": "observed_http"},
                           "session_snapshot": snapshot, "target_trace_refs": [ref],
                           "execution_inputs": {**inputs, "source_ref": {**ref, "session_id": session_id}}}}
