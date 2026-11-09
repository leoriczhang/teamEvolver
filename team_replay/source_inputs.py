"""Verify upstream business inputs against a frozen shared-template request.

Pure Replay contract helpers: no current configuration, source selection, model
calls, or business Handler dependencies. Metadata alone is never proof of use.
"""
from __future__ import annotations

from collections.abc import Mapping

from .execution_inputs import case_execution_plan_pair, source_execution_inputs
from .execution_plan import ExecutionPlanError, render_components
from .request_capture import format_message


def has_template_input_contract(plan):
    return isinstance(plan, Mapping) and "template_input_contract" in (plan.get("environment") or {})


def _require_source_binding(session, turn, baseline):
    provenance = session.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        raise ExecutionPlanError("shared-template source provenance must be an object")
    if (provenance.get("identity_verified") is not True
            or provenance.get("trace_identity_verified") is not True
            or provenance.get("identity_conflict") or provenance.get("revision_conflicts")):
        raise ExecutionPlanError("shared-template source requires verified Session and Trace identity")
    binding = baseline.get("binding") or {}
    context = session.get("runtime_context") or {}
    request = turn.get("request_context") or {}
    if not isinstance(request, Mapping):
        raise ExecutionPlanError("shared-template request context must be an object")
    if request.get("execution_observation_errors"):
        raise ExecutionPlanError("shared-template source execution metadata is invalid or conflicting")
    observed = request.get("execution_binding") or {}
    bound = False
    for values, upstream in ((context, False), (observed, True)):
        if not isinstance(values, Mapping):
            raise ExecutionPlanError("shared-template source binding must be an object")
        if upstream and values and values.get("evidence_level") != "upstream_metadata":
            raise ExecutionPlanError("shared-template source binding is not upstream metadata")
        for field in ("execution_profile_id", "agent_id"):
            expected = binding.get("profile_id" if field == "execution_profile_id" else field)
            if values.get(field):
                if values[field] != expected:
                    raise ExecutionPlanError("shared-template source belongs to another execution binding")
                bound = True
        if values.get("runtime_type") and values["runtime_type"] != binding.get("runtime_type"):
            raise ExecutionPlanError("shared-template source runtime differs from the frozen execution profile")
    if not bound:
        raise ExecutionPlanError("shared-template source requires an observed Agent or profile binding")
    runtime = session.get("runtime") or {}
    if isinstance(runtime, Mapping) and runtime.get("type") and runtime["type"] != binding.get("runtime_type"):
        raise ExecutionPlanError("shared-template source runtime differs from the frozen execution profile")
    level = request.get("evidence_level")
    if level not in {"generation_input", "final_request"}:
        raise ExecutionPlanError("shared-template source lacks recorded model input")
    if level == "final_request" and request.get("completeness") != "complete":
        raise ExecutionPlanError("shared-template final request is incomplete")


def _message_views(turn):
    request = turn.get("request_context") or {}
    if isinstance(request.get("messages"), list):
        yield request["messages"]
    for item in request.get("generation_requests") or []:
        value = item.get("input") if isinstance(item, Mapping) else None
        messages = value.get("messages") if isinstance(value, Mapping) else value
        if isinstance(messages, list):
            yield messages


def verified_source_execution_inputs(session, turn, baseline):
    """Materialize one original target's inputs, then verify its complete request.

    Explicit offline reconstruction remains declared reconstruction. This check
    establishes consistency with that recorded source, not an unseen historical
    request or complete replayability of the source Agent.
    """
    if not has_template_input_contract(baseline):
        return None
    if not isinstance(session, Mapping) or not isinstance(turn, Mapping):
        raise ExecutionPlanError("shared-template source and target turn must be objects")
    _require_source_binding(session, turn, baseline)
    inputs = source_execution_inputs(session, turn)
    if inputs is None:
        raise ExecutionPlanError("shared-template source is missing its original variable bindings")
    query = turn.get("prompt_text") or turn.get("instruction") or turn.get("user_query") or ""
    if not isinstance(query, str) or not query.strip():
        raise ExecutionPlanError("shared-template source requires its original current query")
    case = {"query": query, "provenance": {"execution_inputs": inputs}}
    effective, _ = case_execution_plan_pair(baseline, baseline, case)
    expected = [{"role": part["role"], "content": part["content"]} for part in render_components(effective)]
    expected.extend(inputs["input_messages"])
    expected.append({"role": "user", "content": format_message(effective, "user", query)})
    if not any(messages == expected for messages in _message_views(turn)):
        raise ExecutionPlanError("shared-template variables or public inputs differ from the recorded model request")
    return inputs
