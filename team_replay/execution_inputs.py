"""Frozen per-Case inputs for a shared business Prompt template.

Inputs are materialized from source records by the host. They may vary across
Cases, but the Baseline and Candidate of one Case receive exactly the same
variables and public messages. This module does not select sources or judge
business outcomes.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping

from ._util import stable_hash
from .execution_plan import ExecutionPlanError, validate_execution_plan_pair

EXECUTION_INPUTS_SCHEMA = "teamevolver.execution-inputs.v1"
TEMPLATE_INPUT_CONTRACT_SCHEMA = "teamevolver.template-input-contract.v1"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _object(value, label):
    if not isinstance(value, Mapping):
        raise ExecutionPlanError(f"{label} must be an object")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ExecutionPlanError(f"{label} must contain JSON values") from exc


def _identity(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ExecutionPlanError(f"{label} must be a bounded nonempty string")
    return value


def public_input_messages(value):
    """Only source-supplied public conversation inputs, never privileged rules."""
    if not isinstance(value, list) or len(value) > 128:
        raise ExecutionPlanError("execution input_messages must be an array of at most 128 messages")
    result = []
    for message in value:
        message = _object(message, "execution input message")
        if (set(message) != {"role", "content"} or not isinstance(message["role"], str)
                or message["role"] not in {"user", "assistant"}):
            raise ExecutionPlanError("execution input messages allow only public user/assistant text")
        if not isinstance(message["content"], str) or not message["content"].strip():
            raise ExecutionPlanError("execution input message content must be nonempty text")
        if len(message["content"].encode("utf-8")) > 262_144:
            raise ExecutionPlanError("execution input message exceeds the size limit")
        result.append(message)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 1_048_576:
        raise ExecutionPlanError("execution input_messages exceed the size limit")
    return result


def normalize_execution_inputs(value):
    """Validate a server-materialized source binding without consulting live data."""
    result = _object(value, "execution_inputs")
    if set(result) != {
        "schema_version", "source_ref", "template_refs", "template_variables", "input_messages",
    } or result.get("schema_version") != EXECUTION_INPUTS_SCHEMA:
        raise ExecutionPlanError("unsupported execution_inputs contract")
    source = _object(result["source_ref"], "execution_inputs.source_ref")
    required = {"session_id", "source_session_key", "trace_id", "revision"}
    if not required <= set(source) or set(source) - required - {"context_revision"}:
        raise ExecutionPlanError("execution_inputs requires its frozen source identity")
    result["source_ref"] = {key: _identity(item, "execution source " + key) for key, item in source.items()}
    references = _object(result["template_refs"], "execution_inputs.template_refs")
    if not references or len(references) > 128:
        raise ExecutionPlanError("execution_inputs requires bounded template references")
    for identity, raw in references.items():
        _identity(identity, "template component ID")
        ref = _object(raw, "execution template reference")
        if set(ref) != {"template_id", "revision", "content_hash"}:
            raise ExecutionPlanError("execution template reference has unsupported fields")
        _identity(ref["template_id"], "template_id")
        if isinstance(ref["revision"], bool) or not isinstance(ref["revision"], (str, int)):
            raise ExecutionPlanError("execution template revision must be a string or integer")
        ref["revision"] = _identity(str(ref["revision"]), "template revision")
        if not isinstance(ref["content_hash"], str) or not _HASH.fullmatch(ref["content_hash"]):
            raise ExecutionPlanError("execution template content_hash must be SHA256")
        references[identity] = ref
    result["template_refs"] = references
    variables = _object(result["template_variables"], "execution_inputs.template_variables")
    if len(variables) > 128 or any(not _NAME.fullmatch(name) for name in variables):
        raise ExecutionPlanError("execution template variable names are invalid")
    for item in variables.values():
        if type(item) not in (str, bool, int, float):
            raise ExecutionPlanError("execution template variables must be scalar values")
        if isinstance(item, str) and len(item.encode("utf-8")) > 262_144:
            raise ExecutionPlanError("execution template variable exceeds the size limit")
    if len(json.dumps(variables, ensure_ascii=False).encode("utf-8")) > 1_048_576:
        raise ExecutionPlanError("execution template variables exceed the size limit")
    result["template_variables"] = variables
    result["input_messages"] = public_input_messages(result["input_messages"])
    return result


def template_input_contract(plan):
    """An absent contract keeps the legacy fixed-variable behavior."""
    environment = (plan or {}).get("environment") or {}
    if "template_input_contract" not in environment:
        return None
    value = environment["template_input_contract"]
    contract = _object(value, "template_input_contract")
    names = contract.get("variable_names")
    if (set(contract) != {"schema_version", "variable_names"}
            or contract.get("schema_version") != TEMPLATE_INPUT_CONTRACT_SCHEMA
            or not isinstance(names, list) or not names or len(names) > 128
            or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names)
            or len(names) != len(set(names))):
        raise ExecutionPlanError("unsupported template_input_contract")
    return contract


def plan_input_messages(plan):
    """Read only inputs already materialized into an effective Case plan."""
    environment = (plan or {}).get("environment") or {}
    present = {key for key in ("case_input_messages", "case_input_binding_hash") if key in environment}
    if not present:
        return []
    if (len(present) != 2 or template_input_contract(plan) is None
            or not isinstance(environment["case_input_binding_hash"], str)
            or not _HASH.fullmatch(environment["case_input_binding_hash"])):
        raise ExecutionPlanError("public Case messages require a materialized template input binding")
    return public_input_messages(environment["case_input_messages"])


def source_execution_inputs(session, turn):
    """Attach source identity to declared inputs from one actual target Trace."""
    request = turn.get("request_context") or {}
    trace_input = request.get("trace_input") or {}
    nested = trace_input.get("template_inputs") if isinstance(trace_input, Mapping) else None
    direct = request.get("template_inputs")
    if nested is not None and direct is not None and nested != direct:
        raise ExecutionPlanError("conflicting source template_inputs")
    raw = nested if nested is not None else direct
    if raw is None:
        return None
    raw = _object(raw, "source template_inputs")
    if "source_ref" in raw:
        raise ExecutionPlanError("execution source_ref must be materialized by the host")
    if (session.get("context_only") is True or session.get("usage") == "context_only"
            or turn.get("context_only") is True or turn.get("usage") == "context_only"):
        raise ExecutionPlanError("Context-only records cannot supply execution input targets")
    provenance = session.get("provenance") or {}
    trace = str(turn.get("source_trace_id") or turn.get("trace_id") or "")
    refs = provenance.get("target_trace_refs")
    matches = [ref for ref in refs or [] if isinstance(ref, Mapping) and ref.get("trace_id") == trace]
    if not trace or not isinstance(refs, list) or len(matches) != 1:
        raise ExecutionPlanError("execution inputs require one frozen target Trace reference")
    ref = matches[0]
    if ref.get("source_session_key") != provenance.get("source_session_key"):
        raise ExecutionPlanError("execution input source root differs from its Session")
    source = {key: ref[key] for key in ("source_session_key", "trace_id", "revision", "context_revision") if key in ref}
    source["session_id"] = session.get("session_id")
    return normalize_execution_inputs({**raw, "source_ref": source})


def _check_case_source(inputs, provenance):
    source = inputs["source_ref"]
    if "session_id" in provenance and provenance["session_id"] != source["session_id"]:
        raise ExecutionPlanError("execution input Session differs from its Case source")
    anchor = provenance.get("evidence_anchor")
    if isinstance(anchor, Mapping) and any(
        anchor.get(key) != source[key] for key in ("session_id", "trace_id")
    ):
        raise ExecutionPlanError("execution input Trace differs from its Case anchor")
    refs = provenance.get("target_trace_refs")
    if refs is not None:
        if not isinstance(refs, list) or not any(isinstance(ref, Mapping) and all(
            ref.get(key) == item for key, item in source.items() if key != "session_id"
        ) for ref in refs):
            raise ExecutionPlanError("execution input source is outside the Case target Trace set")
    snapshot = provenance.get("session_snapshot")
    if isinstance(snapshot, Mapping):
        turns = [turn for turn in snapshot.get("turns") or [] if isinstance(turn, Mapping)
                 and str(turn.get("source_trace_id") or turn.get("trace_id") or "") == source["trace_id"]]
        if len(turns) != 1 or source_execution_inputs(snapshot, turns[0]) != inputs:
            raise ExecutionPlanError("execution inputs differ from the frozen source record")


def case_execution_plan_pair(baseline, candidate, case):
    """Derive two plans from the same source variables without changing assets."""
    left, right = validate_execution_plan_pair(baseline, candidate)
    provenance = case.get("provenance") or {}
    value = provenance.get("execution_inputs")
    contract = template_input_contract(left)
    if contract is None:
        if value is not None:
            raise ExecutionPlanError("per-Case execution inputs require an explicit template input contract")
        return left, right
    if value is None:
        raise ExecutionPlanError("shared-template Replay requires frozen per-Case execution inputs")
    inputs = normalize_execution_inputs(value)
    if set(inputs["template_variables"]) != set(contract["variable_names"]):
        raise ExecutionPlanError("execution inputs must supply exactly the declared template variables")
    components = {part["component_id"]: part for part in left["components"] if part["kind"] == "agent_prompt"}
    if not components or set(inputs["template_refs"]) != set(components):
        raise ExecutionPlanError("execution inputs must bind every business Prompt template")
    for identity, part in components.items():
        if inputs["template_refs"][identity] != {
            "template_id": part["binding"].get("template_id"),
            "revision": part["revision"], "content_hash": part["content_hash"],
        }:
            raise ExecutionPlanError("execution inputs refer to a different template identity or version")
    _check_case_source(inputs, provenance)
    for plan in (left, right):
        environment = plan["environment"]
        if "case_input_messages" in environment or "case_input_binding_hash" in environment:
            raise ExecutionPlanError("Case input fields must be derived from frozen source bindings")
        environment["template_variables"] = copy.deepcopy(inputs["template_variables"])
        environment["case_input_messages"] = copy.deepcopy(inputs["input_messages"])
        environment["case_input_binding_hash"] = stable_hash(inputs)
    return validate_execution_plan_pair(left, right)
