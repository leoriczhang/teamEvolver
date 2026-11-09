"""Freeze business assets at planning, preserve the existing Skill writer unchanged."""
from __future__ import annotations

import copy

from team_replay.execution_plan import render_components
from team_skills.evolution.history import skill_payload
from team_skills.evolution.kernel.enums import DecisionAction
from team_skills.library.bundle import diff_skill_bundles
from team_skills.library.repository import SkillLibraryError, SkillLibraryRepository

from .candidates import revise_execution_detail
from .contracts import apply_changes
from .holdout import reserved_families
from .inputs import has_template_input_contract, shared_template_session_matches
from .planning import cumulative_changes
from .repository import ExecutionRepository


async def freeze_profile_scope(conn, tx, profile_id, allowed_modules):
    if "prompt" not in allowed_modules:
        return None
    if not profile_id:
        return {"status": "execution_profile_missing"}
    profile, release = await ExecutionRepository(tx).current(conn, profile_id)
    return {"status": "frozen", "profile": copy.deepcopy(profile), "release": {
        "release_id": release["release_id"], "document": copy.deepcopy(release["document"]),
        "composition_hash": release["composition_hash"],
    }}


async def planning_context(conn, tx, *, profile_id, skill_name, allowed_modules, frozen=None):
    if "prompt" not in allowed_modules:
        return {}, ""
    if frozen is not None and frozen.get("status") != "frozen":
        return {}, frozen.get("status", "execution_profile_missing")
    repo = ExecutionRepository(tx)
    if not profile_id:
        matches = [p for p in await repo.profiles(conn) if skill_name in p["affected_skill_ids"]]
        if len(matches) != 1:
            return {}, "execution_profile_missing" if not matches else "execution_profile_ambiguous"
        profile_id = matches[0]["profile_id"]
    if frozen is not None:
        profile, release = frozen["profile"], frozen["release"]
    else:
        profile, release = await repo.current(conn, profile_id)
    execution_only = skill_name is None and not profile["affected_skill_ids"]
    if not execution_only and skill_name not in profile["affected_skill_ids"]:
        return {}, "skill_not_bound_to_execution_profile"
    dependencies = copy.deepcopy(release["document"]["skill_dependencies"])
    library = SkillLibraryRepository(tx)
    for dependency in dependencies:
        head = await library.get_skill(conn, dependency["name"])
        if not head or head["version"] != dependency["version"] or head["tree_sha256"] != dependency["tree_sha256"]:
            return {}, "execution_skill_dependency_changed"
    # Dependency changes are frozen now and checked again when publishing.
    return {
        "candidate_kind": "execution" if execution_only else "skill",
        "execution_profile_id": profile_id, "base_release_id": release["release_id"],
        "baseline_execution_plan": copy.deepcopy(release["document"]["execution_plan"]),
        "affected_skill_ids": profile["affected_skill_ids"], "skill_dependencies": dependencies,
        "excluded_source_keys": await reserved_families(conn, tx, release["document"]["execution_plan"]),
    }, ""


def _contains_registered_components(messages, expected):
    if not isinstance(messages, list):
        return False
    observed = [(message.get("role"), message.get("content"))
                for message in messages if isinstance(message, dict)]
    cursor = 0
    for wanted in expected:
        try:
            cursor = observed.index(wanted, cursor) + 1
        except ValueError:
            return False
    return bool(expected)


def prompt_evidence_gap(sessions, baseline=None):
    """Observed Generation messages support diagnosis, not proof of complete replayability."""
    if has_template_input_contract(baseline):
        if any(shared_template_session_matches(session, baseline) for session in sessions):
            return ""
        return "prompt_provenance_missing: no recorded variable-bound request matching the frozen template"
    prompt_ids = {p["component_id"] for p in (baseline or {}).get("components", []) if p["kind"] == "agent_prompt"}
    rendered = render_components(baseline) if baseline else []
    main = {p["content"] for p in rendered if p["component_id"] in prompt_ids}
    expected = [(part["role"], part["content"]) for part in rendered]
    if baseline and not prompt_ids:
        return "prompt_provenance_missing: no registered business Prompt"
    for session in sessions:
        for turn in session.get("turns", []):
            value = turn.get("request_context") or {}
            complete = value.get("completeness") == "complete" and value.get("evidence_level") == "final_request"
            upstream = (session.get("provenance") or {}).get("identity_verified") is True
            generation = upstream and value.get("evidence_level") == "generation_input" and bool(main)
            if complete or generation:
                if value.get("execution_observation_errors"):
                    continue
                inputs = [value.get("messages") or []]
                for request in value.get("generation_requests") or []:
                    source = request.get("input") if isinstance(request, dict) else None
                    if isinstance(source, dict):
                        inputs.append(source.get("messages") or [])
                    elif isinstance(source, list):
                        inputs.append(source)
                for messages in inputs:
                    if baseline:
                        if _contains_registered_components(messages, expected):
                            return ""
                    elif any(isinstance(m, dict) and m.get("role") in {"system", "developer"}
                             and isinstance(m.get("content"), str) and m["content"].strip() for m in messages):
                        return ""
    return "prompt_provenance_missing: no observed business Prompt matching the frozen baseline"


def prompt_session_matches(session, baseline):
    """Bind a no-Skill source to observed baseline bytes, never a self-reported profile ID."""
    if not baseline or (session.get("provenance") or {}).get("identity_verified") is not True:
        return False
    if any((session.get("provenance") or {}).get(key) for key in ("identity_conflict", "revision_conflicts")):
        return False
    if has_template_input_contract(baseline):
        return shared_template_session_matches(session, baseline)
    binding = (baseline or {}).get("binding") or {}
    context = session.get("runtime_context") or {}
    for key in ("execution_profile_id", "agent_id"):
        expected = binding.get("profile_id" if key == "execution_profile_id" else key)
        if context.get(key) and context[key] != expected:
            return False
    session_bound = bool(context.get("execution_profile_id") or context.get("agent_id"))
    turns = session.get("_evidence_turns", session.get("turns")) or []
    prompt_ids = {part["component_id"] for part in baseline["components"] if part["kind"] == "agent_prompt"}
    if not prompt_ids:
        return False
    parts = render_components({**baseline, "components": sorted(baseline["components"], key=lambda p: p["order"])})
    # A registered Context is eligible for evolution only when it was actually
    # present alongside the business Prompt, in the same observed request.
    expected = [(part["role"], part["content"]) for part in parts]

    def matches(turn):
        value = turn.get("request_context") or {}
        if value.get("execution_observation_errors"):
            return False
        observed_binding = value.get("execution_binding") or {}
        turn_bound = False
        if observed_binding:
            if observed_binding.get("evidence_level") != "upstream_metadata":
                return False
            for key in ("execution_profile_id", "agent_id"):
                expected_id = binding.get("profile_id" if key == "execution_profile_id" else key)
                if observed_binding.get(key):
                    if observed_binding[key] != expected_id:
                        return False
                    turn_bound = True
            if (observed_binding.get("runtime_type")
                    and observed_binding["runtime_type"] != binding.get("runtime_type")):
                return False
        if not session_bound and not turn_bound:
            return False
        if value.get("evidence_level") not in {"generation_input", "final_request"}:
            return False
        if value.get("evidence_level") == "final_request" and value.get("completeness") != "complete":
            return False
        inputs = [value.get("messages") or []]
        for request in value.get("generation_requests") or []:
            source = request.get("input") if isinstance(request, dict) else None
            inputs.append(source.get("messages", []) if isinstance(source, dict) else source)
        for messages in inputs:
            if _contains_registered_components(messages, expected):
                return True
        return False

    return bool(turns) and all(matches(turn) for turn in turns)


def attach_execution_proposal(result, material, *, context, allowed_modules, working_bundle, skill_changed):
    changes = result.get("component_changes") or []
    if changes and "prompt" not in allowed_modules:
        raise SkillLibraryError("scope_violation: planner attempted an unselected Prompt change")
    if "prompt" not in allowed_modules:
        return material
    if not context.get("baseline_execution_plan"):
        raise SkillLibraryError("execution_profile_missing")
    detail = {key: copy.deepcopy(context[key]) for key in (
        "execution_profile_id", "base_release_id", "baseline_execution_plan",
        "affected_skill_ids", "skill_dependencies",
    )}
    detail.update(allowed_modules=allowed_modules, skill_changed=skill_changed)
    detail["candidate_kind"] = context.get("candidate_kind", "skill")
    if changes:
        apply_changes(context.get("working_execution_plan") or detail["baseline_execution_plan"],
                      changes, allowed_modules=allowed_modules)
    combined = cumulative_changes(context, changes)
    if combined:
        detail = revise_execution_detail(detail, combined)
    else:
        detail["candidate_execution_plan"] = copy.deepcopy(detail["baseline_execution_plan"])
        detail["baseline_execution_plan"]["target_component_ids"] = []
        detail["candidate_execution_plan"]["target_component_ids"] = []
        detail["actual_modules"] = ["skill"] if skill_changed else []
        detail["component_changes"] = []
    for key in ("baseline_execution_plan", "candidate_execution_plan"):
        detail[key].setdefault("binding", {})["allow_skill_change"] = bool(skill_changed)
    result_material = copy.deepcopy(material)
    result_material["_execution_detail"] = detail
    return result_material


def fixed_skill_material(name, working_bundle, result):
    """A Prompt-only path never renders or versions an unchanged Skill."""
    current = skill_payload(name, working_bundle)
    if not current:
        if (name is None and not working_bundle and not result.get("skill")
                and result.get("action") != DecisionAction.CREATE):
            return {}, diff_skill_bundles({}, {})
        raise SkillLibraryError("Prompt evolution requires an existing associated Skill")
    proposed = result.get("skill") or {}
    if proposed.get("file_changes") or any(
        key in proposed and proposed[key] != current.get(key) for key in ("name", "content", "description", "category")
    ):
        raise SkillLibraryError("scope_violation: Skill was not selected")
    if result.get("action") == DecisionAction.CREATE:
        raise SkillLibraryError("scope_violation: cannot create a Skill in Prompt-only mode")
    return current, diff_skill_bundles(working_bundle, working_bundle)
