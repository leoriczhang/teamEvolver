"""Frozen, runtime-independent Prompt/Context treatments; legacy Skills need no plan."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

from ._util import stable_hash
from .variable_context import inline_variable_binding, selected_inline_components

EXECUTION_PLAN_SCHEMA = "teamevolver.execution-plan.v1"
EXECUTION_RECEIPT_SCHEMA = "teamevolver.execution-receipt.v1"
COMPONENT_KINDS = frozenset({"agent_prompt", "context_fragment"})
COMPONENT_ROLES = {"system": "system", "developer": "developer", "context": "developer"}
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PLACEHOLDER = re.compile(r"{{(.*?)}}", re.DOTALL)


class ExecutionPlanError(ValueError):
    """A frozen plan is invalid or changes an input outside its declared treatment."""


def _object(value, label):
    if not isinstance(value, Mapping):
        raise ExecutionPlanError(f"{label} must be an object")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ExecutionPlanError(f"{label} must contain JSON values") from exc


def render_components(plan: Mapping) -> list[dict]:
    """Render literal scalar substitutions once, with no expression evaluation.

    Only {{identifier}} (optionally padded with whitespace) is a placeholder.
    Single JSON braces remain literal; inserted variable values are never parsed
    as templates. The frozen environment, rather than runtime/session data,
    supplies every value.
    """
    variables = _object(plan.get("environment", {}).get("template_variables", {}), "template_variables")
    if len(variables) > 128:
        raise ExecutionPlanError("template_variables exceeds 128 variables")
    for name, value in variables.items():
        if not _VARIABLE_NAME.fullmatch(name) or type(value) not in (str, bool, int, float):
            raise ExecutionPlanError("template_variables requires identifier keys and scalar string/bool/number values")
        if isinstance(value, str) and len(value.encode("utf-8")) > 262_144:
            raise ExecutionPlanError("template variable exceeds the size limit")
    if len(json.dumps(variables, ensure_ascii=False).encode("utf-8")) > 1_048_576:
        raise ExecutionPlanError("template_variables exceeds the size limit")
    try:
        selected = selected_inline_components(plan, variables)
    except ValueError as exc:
        raise ExecutionPlanError(str(exc)) from exc
    overrides = {(inline_variable_binding(part)["component_id"], inline_variable_binding(part)["name"]): part
                 for part in selected}
    variable_hash, total, rendered = stable_hash(variables), 0, []
    for component in plan["components"]:
        if inline_variable_binding(component) is not None:
            continue
        text, used, parts, cursor, expanded_size = component["content"], set(), [], 0, 0
        inline_spans, output_chars = [], 0
        if any(marker in text for marker in ("{%", "%}", "{#", "#}")):
            raise ExecutionPlanError("unsupported Prompt template syntax")
        for match in _PLACEHOLDER.finditer(text):
            literal, name = text[cursor:match.start()], match[1].strip()
            if "{{" in literal or not _VARIABLE_NAME.fullmatch(name):
                raise ExecutionPlanError("unsupported Prompt template syntax")
            if name not in variables:
                raise ExecutionPlanError(f"missing Prompt template variable: {name}")
            asset = overrides.get((component["component_id"], name))
            value = asset["content"] if asset is not None else variables[name]
            substitution = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
            if asset is not None:
                start = output_chars + len(literal)
                inline_spans.append({"component_id": asset["component_id"], "start": start,
                                     "end": start + len(substitution)})
            output_chars += len(literal) + len(substitution)
            expanded_size += len(literal.encode("utf-8")) + len(substitution.encode("utf-8"))
            if expanded_size > 262_144:
                raise ExecutionPlanError("rendered execution_plan content exceeds the size limit")
            parts.extend((literal, substitution))
            used.add(name)
            cursor = match.end()
        tail = text[cursor:]
        if "{{" in tail:
            raise ExecutionPlanError("unsupported Prompt template syntax")
        parts.append(tail)
        content = "".join(parts)
        size = len(content.encode("utf-8"))
        total += size
        if size > 262_144 or total > 1_048_576:
            raise ExecutionPlanError("rendered execution_plan content exceeds the size limit")
        rendered.append({
            "component_id": component["component_id"], "role": COMPONENT_ROLES[component["slot"]],
            "content": content, "template_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "template_variables_hash": variable_hash,
            "rendered_content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "variable_names": sorted(used),
        })
        inline_assets = [part for part in selected
                         if inline_variable_binding(part)["component_id"] == component["component_id"]]
        if inline_assets:
            rendered[-1]["inline_components"] = [{
                "component_id": part["component_id"], "revision": str(part["revision"]),
                "content_hash": part["content_hash"], "binding": inline_variable_binding(part),
            } for part in inline_assets]
            rendered[-1]["inline_spans"] = inline_spans
    return rendered


def normalize_execution_plan(value: Any) -> dict | None:
    """Validate hashes rather than trusting the producer's claimed component content."""
    if value is None:
        return None
    plan = _object(value, "execution_plan")
    if plan.get("schema_version") != EXECUTION_PLAN_SCHEMA:
        raise ExecutionPlanError("unsupported execution_plan schema_version")
    plan["binding"] = _object(plan.get("binding"), "execution_plan.binding")
    if "allow_skill_change" in plan["binding"] and type(plan["binding"]["allow_skill_change"]) is not bool:
        raise ExecutionPlanError("execution_plan.binding.allow_skill_change must be boolean")
    plan["environment"] = _object(plan.get("environment", {}), "execution_plan.environment")
    prefixes = plan["environment"].get("message_prefixes", {})
    if not isinstance(prefixes, dict) or set(prefixes) - {"user", "assistant"} or any(
        not isinstance(p, str) or len(p) > 64 for p in prefixes.values()
    ):
        raise ExecutionPlanError("message_prefixes must bind bounded user/assistant prefixes")
    components = plan.get("components")
    targets = plan.get("target_component_ids")
    if not isinstance(components, list) or not components or len(components) > 128:
        raise ExecutionPlanError("execution_plan requires 1..128 components")
    if not isinstance(targets, list) or any(not isinstance(item, str) or not item for item in targets):
        raise ExecutionPlanError("target_component_ids must be a list of component IDs")
    if len(set(targets)) != len(targets):
        raise ExecutionPlanError("duplicate target_component_ids")
    ids, orders, context_refs, total = set(), set(), set(), 0
    for component in components:
        if not isinstance(component, dict):
            raise ExecutionPlanError("execution_plan component must be an object")
        identity = component.get("component_id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 256 or identity in ids:
            raise ExecutionPlanError("component_id must be nonempty and unique")
        ids.add(identity)
        if not isinstance(component.get("kind"), str) or component["kind"] not in COMPONENT_KINDS:
            raise ExecutionPlanError("unsupported execution_plan component kind")
        if not isinstance(component.get("slot"), str) or component["slot"] not in COMPONENT_ROLES:
            raise ExecutionPlanError("unsupported execution_plan slot")
        order = component.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 0 or order in orders:
            raise ExecutionPlanError("component order must be a unique nonnegative integer")
        orders.add(order)
        component["binding"] = _object(component.get("binding", {}), "component.binding")
        context_ref = component["binding"].get("context_ref")
        if context_ref is not None:
            if component["kind"] != "context_fragment" or not isinstance(context_ref, str) or not context_ref.strip():
                raise ExecutionPlanError("context_ref must bind a Context fragment to one frozen item")
            if context_ref in context_refs:
                raise ExecutionPlanError("multiple components cannot replace the same Context item")
            context_refs.add(context_ref)
        revision = component.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, (str, int)) or not str(revision).strip():
            raise ExecutionPlanError("component revision is required")
        component["revision"] = str(revision)
        content = component.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ExecutionPlanError("component content must be nonempty text")
        size = len(content.encode("utf-8"))
        total += size
        if size > 262_144 or total > 1_048_576:
            raise ExecutionPlanError("execution_plan content exceeds the size limit")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if component.get("content_hash") != digest:
            raise ExecutionPlanError(f"component content_hash mismatch: {identity}")
    if set(targets) - ids:
        raise ExecutionPlanError("target_component_ids reference unavailable components")
    plan["components"] = sorted(components, key=lambda item: item["order"])
    plan["target_component_ids"] = sorted(targets)
    render_components(plan)
    return plan


def execution_plan_hash(value: Any) -> str:
    return stable_hash(normalize_execution_plan(value))


def validate_execution_plan_pair(baseline: Any, candidate: Any) -> tuple[dict | None, dict | None]:
    """Freeze placement, shared components and environment; vary only declared text/revision."""
    left, right = normalize_execution_plan(baseline), normalize_execution_plan(candidate)
    if left is None and right is None:
        return left, right
    if left is None or right is None:
        raise ExecutionPlanError("both Replay branches require an execution_plan")
    if {k: v for k, v in left.items() if k != "components"} != {
        k: v for k, v in right.items() if k != "components"
    }:
        raise ExecutionPlanError("execution_plan binding, targets and environment must remain fixed")
    if len(left["components"]) != len(right["components"]):
        raise ExecutionPlanError("execution_plan component inventory must remain fixed")
    targets = set(left["target_component_ids"])
    for before, after in zip(left["components"], right["components"], strict=True):
        ignored = {"content", "content_hash", "revision"} if before["component_id"] in targets else set()
        if {k: v for k, v in before.items() if k not in ignored} != {
            k: v for k, v in after.items() if k not in ignored
        }:
            raise ExecutionPlanError("execution_plan changed a shared component or component placement")
    return left, right


def plan_from_job(job: Mapping, branch: str) -> dict | None:
    key = f"{branch}_execution_plan"
    payload = job.get("current_skill" if branch == "baseline" else "candidate_skill") or {}
    value = job[key] if key in job else payload.get("execution_plan")
    return normalize_execution_plan(value)


def treatment_execution_plan(treatment) -> dict | None:
    """Only absence falls back to an embedded plan; malformed empty objects never do."""
    value = treatment.execution_plan
    if value is None:
        value = treatment.payload.get("execution_plan")
    return normalize_execution_plan(value)


def normalize_capabilities(value: Any) -> dict:
    """An old adapter explicitly has no Prompt/Context replacement capability."""
    if value is None:
        return {}
    capabilities = _object(value, "Replay adapter capabilities")
    if capabilities.get("execution_plan") not in (None, EXECUTION_PLAN_SCHEMA):
        raise ExecutionPlanError("unsupported adapter execution_plan capability")
    kinds = capabilities.get("component_kinds", [])
    if not isinstance(kinds, list) or any(not isinstance(kind, str) or kind not in COMPONENT_KINDS for kind in kinds):
        raise ExecutionPlanError("invalid adapter component_kinds capability")
    if capabilities.get("request_observation") not in (None, "local_dispatch", "remote_attested_dispatch"):
        raise ExecutionPlanError("unsupported adapter request_observation capability")
    return copy.deepcopy(capabilities)


def require_execution_capabilities(factory, plan: Mapping | None) -> None:
    if plan is None:
        return
    from .hooks import ReplayUnsupported

    capabilities = normalize_capabilities(getattr(factory, "capabilities", None))
    if capabilities.get("execution_plan") != EXECUTION_PLAN_SCHEMA or (
        capabilities.get("request_observation") not in {"local_dispatch", "remote_attested_dispatch"}
    ):
        raise ReplayUnsupported("adapter cannot replace Prompt/Context with observed final model dispatch")
    if {item["kind"] for item in plan["components"]} - set(capabilities.get("component_kinds", [])):
        raise ReplayUnsupported("adapter does not support all execution_plan component kinds")
    if plan["binding"].get("allow_skill_change") and capabilities.get("skill_treatment") is not True:
        raise ReplayUnsupported("adapter does not support observed joint Skill/Prompt treatment")
