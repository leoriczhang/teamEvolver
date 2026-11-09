"""Read-only registration previews and a real-Agent observation template."""
from __future__ import annotations

import copy
import re

from team_replay.variable_context import inline_variable_binding
from team_skills.evolution.repository import digest

from .contracts import validate_profile

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def registration_preview(raw):
    """Use exactly the registration validator, without creating business state."""
    profile = validate_profile(raw)
    variables = {}
    for part in profile["components"]:
        if inline_variable_binding(part):
            continue
        for name in dict.fromkeys(_PLACEHOLDER.findall(part["content"])):
            variables.setdefault(name, []).append(part["component_id"])
    result = {key: copy.deepcopy(profile[key]) for key in (
        "profile_id", "components", "environment",
    )}
    result["summary"] = {
        "component_count": len(profile["components"]),
        "affected_skill_count": len(profile["affected_skill_ids"]),
        "template_variables": [{"name": name, "component_ids": ids} for name, ids in sorted(variables.items())],
    }
    result["warnings"] = ([] if any(part["kind"] == "agent_prompt" for part in profile["components"]) else [
        "Context-only registration has no business Prompt to match; register its host Prompt before evolution.",
    ])
    result["preview_hash"] = digest(result)
    return result


def agent_integration_template(profile, release):
    """A copyable schema template, never proof of an observed model request.

    Values shown here are the operator's registered defaults. The reporting
    Agent must replace them with the values and public inputs actually used.
    """
    plan = release["document"]["execution_plan"]
    environment = plan["environment"]
    refs, managed = {}, {}
    for part in plan["components"]:
        inline = inline_variable_binding(part)
        if inline:
            managed.setdefault(inline["name"], []).append(part["component_id"])
        if part["kind"] == "agent_prompt":
            refs[part["component_id"]] = {
                "template_id": part["binding"].get("template_id", part["component_id"]),
                "revision": str(part["revision"]), "content_hash": part["content_hash"],
            }
    contract = environment.get("template_input_contract")
    defaults = copy.deepcopy(environment.get("template_variables", {}))
    names = contract["variable_names"] if contract else list(defaults)
    observation = {
        "schema_version": "teamevolver.execution-observation.v1",
        "execution_profile_id": profile["profile_id"], "agent_id": profile["agent_id"],
        "runtime_type": profile["runtime_type"], "release_id": release["release_id"],
        "composition_hash": release["composition_hash"],
    }
    if contract:
        observation["template_inputs"] = {
            "schema_version": "teamevolver.execution-inputs.v1", "template_refs": refs,
            "template_variables": defaults, "input_messages": [],
        }
    return {
        "profile_id": profile["profile_id"], "current_release_id": release["release_id"],
        "composition_hash": release["composition_hash"],
        "metadata": {"teamevolver_execution": observation},
        "variables": [{"name": name, "managed_context_component_ids": managed.get(name, [])}
                      for name in sorted(names)],
        "requirements": [
            "This is an observation template, not evidence that the Agent applied this release.",
            "Report the release actually loaded; do not stamp the latest release on an older request.",
            "Replace registered defaults with the exact variables and public input messages used for this request.",
            "Record the complete actual model input in the Langfuse GENERATION observation.",
            "Keep stable sessionId and a distinct trace ID for each new model interaction; "
            "never resend old turns as new IDs.",
        ],
        "shared_template": contract is not None,
    }
