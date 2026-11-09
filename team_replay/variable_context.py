"""Literal, versioned Context assets selected by immutable source variable text."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_PLACEHOLDER = re.compile(r"{{(.*?)}}", re.DOTALL)


def source_value_hash(value):
    if not isinstance(value, str):
        raise ValueError("inline Context source value must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def inline_variable_binding(component):
    binding = component.get("binding") or {}
    if "inline_variable" not in binding:
        return None
    value = binding["inline_variable"]
    if (component.get("kind") != "context_fragment" or "context_ref" in binding
            or not isinstance(value, Mapping)
            or set(value) != {"component_id", "name", "source_value_hash"}
            or not isinstance(value["component_id"], str) or not value["component_id"].strip()
            or not isinstance(value["name"], str) or not _NAME.fullmatch(value["name"])
            or not isinstance(value["source_value_hash"], str) or not _HASH.fullmatch(value["source_value_hash"])):
        raise ValueError("invalid inline_variable Context binding")
    return dict(value)


def validate_inline_bindings(plan):
    components = {part["component_id"]: part for part in plan["components"]}
    seen = set()
    for part in plan["components"]:
        binding = inline_variable_binding(part)
        if binding is None:
            continue
        host = components.get(binding["component_id"])
        if (host is None or host.get("kind") != "agent_prompt"
                or binding["name"] not in {m[1].strip() for m in _PLACEHOLDER.finditer(host["content"])}):
            raise ValueError("inline Context must bind a variable in an existing Agent Prompt")
        key = (binding["component_id"], binding["name"], binding["source_value_hash"])
        if key in seen:
            raise ValueError("duplicate inline Context source binding")
        seen.add(key)


def selected_inline_components(plan, variables=None):
    validate_inline_bindings(plan)
    variables = plan.get("environment", {}).get("template_variables", {}) if variables is None else variables
    selected = []
    for part in plan["components"]:
        binding = inline_variable_binding(part)
        if binding is None:
            continue
        value = variables.get(binding["name"])
        if isinstance(value, str) and value and source_value_hash(value) == binding["source_value_hash"]:
            selected.append(part)
    return selected


def applicable_component_ids(plan, variables=None):
    return {part["component_id"] for part in plan["components"] if inline_variable_binding(part) is None} | {
        part["component_id"] for part in selected_inline_components(plan, variables)
    }


def variable_context_groups(plan):
    """Read-only variable aggregates over existing value/host components.

    No group field is written into a signed execution plan. Existing leaf IDs,
    revisions and source selectors remain the authoritative execution contract.
    """
    profile = str((plan.get("binding") or {}).get("profile_id") or "")
    groups = {}
    for part in plan.get("components") or []:
        binding = inline_variable_binding(part)
        if binding is None:
            continue
        name = binding["name"]
        group = groups.setdefault(name, {"group_id": f"variable-group:{profile}:{name}", "name": name, "members": []})
        group["members"].append({
            "component_id": part["component_id"], "host_component_id": binding["component_id"],
            "source_value_hash": binding["source_value_hash"], "revision": str(part["revision"]),
            "content_hash": part["content_hash"],
        })
    for group in groups.values():
        group["members"].sort(key=lambda member: member["component_id"])
        group["value_count"] = len({member["source_value_hash"] for member in group["members"]})
        raw = json.dumps(group["members"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        group["version_hash"] = hashlib.sha256(raw.encode()).hexdigest()
    return [groups[name] for name in sorted(groups)]


def project_inline_plan(plan):
    """Project into scalar substitution for older transports, or fail if inexact.

    Logical source variables remain unchanged. Only this derived transport view
    substitutes selected asset text; it must never be persisted as a source plan.
    """
    selected = selected_inline_components(plan)
    result = copy.deepcopy(plan)
    inline = {part["component_id"]: inline_variable_binding(part) for part in plan["components"]
              if inline_variable_binding(part) is not None}
    if not inline:
        return result
    originals = plan.get("environment", {}).get("template_variables", {})
    overrides = {(inline[part["component_id"]]["component_id"], inline[part["component_id"]]["name"]):
                 part["content"] for part in selected}
    variables = result["environment"].setdefault("template_variables", {})
    result["components"] = [part for part in result["components"] if part["component_id"] not in inline]
    # A scalar transport has one value per variable, whereas a logical plan can
    # bind different assets at different hosts. Never broaden a host-local edit.
    desired = {}
    for part in result["components"]:
        for match in _PLACEHOLDER.finditer(part["content"]):
            name = match[1].strip()
            value = overrides.get((part["component_id"], name), originals.get(name))
            if name in desired and desired[name] != value:
                raise ValueError("transport cannot represent host-specific inline Context values")
            desired[name] = value
    variables.update(desired)
    result["target_component_ids"] = sorted({
        inline[cid]["component_id"] if cid in inline else cid for cid in plan["target_component_ids"]
    })
    return result
