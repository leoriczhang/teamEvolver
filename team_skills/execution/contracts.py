"""Bounded, explicit business-asset bindings; never TE evaluator instructions."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from team_skills.evolution.repository import digest
from team_skills.library.repository import SkillLibraryError

MAX_COMPONENT_BYTES = 262144
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$")
PROFILE_SCHEMA = "teamevolver.execution-profile.v1"


def identifier(value, field="id"):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise SkillLibraryError(f"{field} must be a stable identifier of at most 200 characters")
    return value


def text_hash(content):
    if not isinstance(content, str) or not content.strip():
        raise SkillLibraryError("business component content must be nonempty text")
    if len(content.encode()) > MAX_COMPONENT_BYTES:
        raise SkillLibraryError("business component exceeds 256 KiB")
    return hashlib.sha256(content.encode()).hexdigest()


def validate_profile(raw):
    if not isinstance(raw, dict) or set(raw) - {
        "profile_id", "runtime_type", "agent_id", "user_id", "affected_skill_ids", "components", "environment",
    }:
        raise SkillLibraryError("unsupported execution profile field")
    value = copy.deepcopy(raw)
    for key in ("profile_id", "runtime_type", "agent_id", "user_id"):
        identifier(value.get(key), key)
    skills = value.get("affected_skill_ids")
    if (not isinstance(skills, list) or len(skills) > 50
            or any(not isinstance(item, str) for item in skills) or len(set(skills)) != len(skills)):
        raise SkillLibraryError("affected_skill_ids must contain 0..50 distinct Skill names")
    for name in skills:
        identifier(name, "skill name")
    parts = value.get("components")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 32:
        raise SkillLibraryError("execution profile requires 1..32 components")
    seen, positions = set(), set()
    for part in parts:
        if not isinstance(part, dict) or set(part) - {
            "component_id", "kind", "slot", "order", "content", "inline_variable",
        }:
            raise SkillLibraryError("unsupported component field")
        cid = identifier(part.get("component_id"), "component_id")
        if cid in seen:
            raise SkillLibraryError("duplicate component_id")
        seen.add(cid)
        if not isinstance(part.get("kind"), str) or part["kind"] not in {"agent_prompt", "context_fragment"}:
            raise SkillLibraryError("unsupported business component kind")
        if not isinstance(part.get("slot"), str) or part["slot"] not in {"system", "developer", "context"}:
            raise SkillLibraryError("slot must be system, developer or context")
        order = part.get("order", 0)
        if type(order) is not int or not 0 <= order <= 1000 or order in positions:
            raise SkillLibraryError("component order must be globally unique")
        positions.add(order)
        part.update(order=order, content_hash=text_hash(part.get("content")), revision=1,
                    binding={"profile_id": value["profile_id"], "affected_skill_ids": sorted(skills)})
        if "inline_variable" in part:
            part["binding"]["inline_variable"] = part.pop("inline_variable")
    env = value.setdefault("environment", {})
    if not isinstance(env, dict) or len(str(env).encode()) > 65536:
        raise SkillLibraryError("environment must be a bounded object")
    _no_credentials(env)
    try:
        json.dumps(env, allow_nan=False)
        from team_replay.execution_inputs import template_input_contract
        from team_replay.execution_plan import normalize_execution_plan

        from .holdout import validation_policy
        contract = template_input_contract(plan_for(value, parts))
        if contract is not None:
            if set(env.get("template_variables") or {}) != set(contract["variable_names"]):
                raise ValueError("shared template defaults must bind exactly the declared variables")
            for part in parts:
                if part["kind"] == "agent_prompt":
                    part["binding"]["template_id"] = part["component_id"]
        plan = plan_for(value, parts)
        normalize_execution_plan(plan)
        editable_component_ids(plan)
        validation_policy(env)
    except (TypeError, ValueError) as exc:
        raise SkillLibraryError(str(exc)) from exc
    value["affected_skill_ids"] = sorted(skills)
    value["schema_version"] = PROFILE_SCHEMA
    return value


def _no_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in {"apikey", "xapikey", "password", "authorization", "accesstoken", "secretkey",
                              "rootkey", "rootapikey", "clientsecret", "bearertoken", "subscriptionkey"}:
                raise SkillLibraryError("execution environment must use Secret Ref, never credentials")
            _no_credentials(child)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item)


def plan_for(profile, components, *, targets=()):
    return {
        "schema_version": "teamevolver.execution-plan.v1",
        "binding": {key: profile[key] for key in ("profile_id", "runtime_type", "agent_id", "user_id")},
        "environment": copy.deepcopy(profile["environment"]),
        "components": copy.deepcopy(components), "target_component_ids": sorted(targets),
    }


def editable_component_ids(plan):
    """Optional, frozen per-component scope inside the existing Prompt module."""
    all_ids = {part["component_id"] for part in plan["components"]}
    policy = plan.get("environment", {}).get("evolution", {})
    if not isinstance(policy, dict) or set(policy) - {"editable_component_ids"}:
        raise SkillLibraryError("unsupported execution evolution policy")
    if "editable_component_ids" not in policy:
        return all_ids
    values = policy["editable_component_ids"]
    if (not isinstance(values, list) or not values or any(not isinstance(cid, str) for cid in values)
            or len(values) != len(set(values)) or not set(values) <= all_ids):
        raise SkillLibraryError("editable_component_ids must select distinct registered components")
    return set(values)


def apply_changes(plan, changes, *, allowed_modules):
    if "prompt" not in allowed_modules:
        raise SkillLibraryError("scope_violation: Prompt was not selected")
    if not isinstance(changes, list) or not changes or len(changes) > 32:
        raise SkillLibraryError("component_changes must be a nonempty bounded list")
    result = copy.deepcopy(plan)
    by_id = {part["component_id"]: part for part in result["components"]}
    editable = editable_component_ids(result)
    changed = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"component_id", "content", "reason", "evidence_refs"}:
            raise SkillLibraryError("only component text may be changed; bindings are frozen")
        cid = identifier(change.get("component_id"), "component_id")
        if cid not in by_id or cid in changed:
            raise SkillLibraryError("unknown or duplicate target component")
        if cid not in editable:
            raise SkillLibraryError("component is outside the frozen editable scope")
        if not isinstance(change.get("reason"), str) or not change["reason"].strip():
            raise SkillLibraryError("component modification requires a reason")
        refs = change.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
            raise SkillLibraryError("component modification requires original evidence_refs")
        part = by_id[cid]
        sha = text_hash(change.get("content"))
        if sha == part["content_hash"]:
            continue
        part.update(content=change["content"], content_hash=sha, revision=int(part["revision"]) + 1)
        changed.add(cid)
    if not changed:
        raise SkillLibraryError("no component content changed")
    result["target_component_ids"] = sorted(changed)
    return result


def composition_hash(document):
    return digest(document)
