"""Derive a new versioned variable-context baseline from bounded text samples."""
from __future__ import annotations

import copy
import json
import re

from team_replay.variable_context import inline_variable_binding, source_value_hash
from team_skills.evolution.repository import digest
from team_skills.library.repository import SkillLibraryError

from .contracts import identifier, validate_profile

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_FIELDS = {"profile_id", "source_release_id", "managed_variables", "variable_samples", "variable_values",
           "evolution_scope"}


def _id(profile_id, label, key):
    return f"{profile_id[:120]}:{label}:{digest(key)[:20]}"


def _variable_inputs(request, managed, names):
    """Accept source rows or independent value collections, never a Cartesian product."""
    if ("variable_values" in request) == ("variable_samples" in request):
        raise SkillLibraryError("provide exactly one of variable_samples or variable_values")
    grouped = "variable_values" in request
    inputs = request["variable_values"] if grouped else request["variable_samples"]
    try:
        size = len(json.dumps(inputs, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError) as exc:
        raise SkillLibraryError("samples must contain finite JSON scalar inputs") from exc
    if size > 1_048_576:
        raise SkillLibraryError("variable samples exceed 1 MiB")
    if grouped:
        if not isinstance(inputs, dict) or set(inputs) != set(managed):
            raise SkillLibraryError("variable_values must contain exactly the selected managed variables")
        if any(not isinstance(values, list) or not 1 <= len(values) <= 200
               or any(not isinstance(value, str) for value in values) for values in inputs.values()):
            raise SkillLibraryError("each variable_values collection requires 1..200 text values")
        return inputs, sum(len(values) for values in inputs.values()), "variable_values"
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 200:
        raise SkillLibraryError("variable_samples requires 1..200 input objects")
    for sample in inputs:
        if (not isinstance(sample, dict) or not set(managed) <= sample.keys() or not sample.keys() <= names
                or any(type(value) not in (str, bool, int, float) for value in sample.values())):
            raise SkillLibraryError("samples must contain selected variables and only declared scalar inputs")
        if any(not isinstance(sample[name], str) for name in managed):
            raise SkillLibraryError("only text variables can be registered as context components")
    return {name: [sample[name] for sample in inputs] for name in managed}, len(inputs), "variable_samples"


def variable_context_registration(source, release, request):
    """No I/O or mutation: preview and create derive exactly the same profile.

    Source values remain selectors after their managed body evolves. Empty text
    and values not registered here continue to render as their original input.
    """
    if not isinstance(request, dict) or set(request) - _FIELDS:
        raise SkillLibraryError("unsupported variable-context registration field")
    pid = identifier(request.get("profile_id"), "profile_id")
    if pid == source["profile_id"]:
        raise SkillLibraryError("variable contexts require a new execution profile ID")
    if request.get("source_release_id") != release["release_id"]:
        raise SkillLibraryError("source release changed; refresh and preview again")
    scope = request.get("evolution_scope", "context")
    if scope not in {"context", "prompt_and_context"}:
        raise SkillLibraryError("evolution_scope must be context or prompt_and_context")
    original = release["document"]["execution_plan"]
    environment = copy.deepcopy(original["environment"])
    defaults = environment.get("template_variables", {})
    contract = environment.get("template_input_contract")
    names = set(contract["variable_names"] if contract else defaults)
    managed = request.get("managed_variables")
    if (not isinstance(managed, list) or not managed or any(not isinstance(name, str) for name in managed)
            or len(managed) != len(set(managed)) or not set(managed) <= names):
        raise SkillLibraryError("managed_variables must select distinct declared template variables")
    variable_values, input_count, input_mode = _variable_inputs(request, managed, names)

    id_map = {part["component_id"]: _id(pid, "component", [pid, part["component_id"]])
              for part in original["components"]}
    components = []
    existing = {}
    for part in sorted(original["components"], key=lambda item: item["order"]):
        value = {key: copy.deepcopy(part[key]) for key in ("kind", "slot", "content")}
        value.update(component_id=id_map[part["component_id"]], order=len(components))
        binding = inline_variable_binding(part)
        if binding:
            bound = copy.deepcopy(binding)
            bound["component_id"] = id_map[bound["component_id"]]
            value["inline_variable"] = bound
            existing[(bound["component_id"], bound["name"], bound["source_value_hash"])] = value
        elif part.get("binding", {}).get("context_ref"):
            raise SkillLibraryError("a retrieved Context binding cannot be silently converted to a variable baseline")
        components.append(value)
    hosts = {name: [part for part in components if part["kind"] == "agent_prompt"
                    and name in _PLACEHOLDER.findall(part["content"])] for name in managed}
    if any(not parts for parts in hosts.values()):
        raise SkillLibraryError("every managed variable must occur in a business Prompt template")
    summary, selected, templates = [], set(), set()
    for name in sorted(managed):
        values = list(dict.fromkeys(value for value in variable_values[name] if value.strip()))
        chosen = []
        for host in hosts[name]:
            templates.add(host["component_id"])
            for content in values:
                sha = source_value_hash(content)
                key = (host["component_id"], name, sha)
                part = existing.get(key)
                if part is None:
                    part = {"component_id": _id(pid, "variable", [pid, *key]), "kind": "context_fragment",
                            "slot": "context", "order": len(components), "content": content,
                            "inline_variable": {"component_id": host["component_id"], "name": name,
                                                "source_value_hash": sha}}
                    components.append(part)
                    existing[key] = part
                chosen.append(part["component_id"])
                selected.add(part["component_id"])
        summary.append({"name": name, "unique_values": len(values),
                        "blank_samples": sum(not value.strip() for value in variable_values[name]),
                        "component_ids": chosen})
    if not selected:
        raise SkillLibraryError("samples contain no nonempty selected text to register")
    if len(components) > 32:
        raise SkillLibraryError("variable contexts exceed the existing 32-component limit; choose fewer values")
    if not contract:
        environment["template_input_contract"] = {
            "schema_version": "teamevolver.template-input-contract.v1", "variable_names": sorted(names),
        }
    environment["evolution"] = {"editable_component_ids": sorted(
        selected | templates if scope == "prompt_and_context" else selected,
    )}
    environment["variable_context_source"] = {
        "profile_id": source["profile_id"], "release_id": release["release_id"],
        "composition_hash": release["composition_hash"],
    }
    raw = {key: copy.deepcopy(source[key]) for key in ("runtime_type", "agent_id", "user_id", "affected_skill_ids")}
    raw.update(profile_id=pid, components=components, environment=environment)
    validated = validate_profile(raw)
    result = {
        "profile_id": pid, "source_profile_id": source["profile_id"], "source_release_id": release["release_id"],
        "components": validated["components"], "summary": {
            "sample_count": input_count, "input_mode": input_mode,
            "managed_variables": summary, "component_count": len(components),
        },
    }
    result["preview_hash"] = digest(result)
    return raw, result
