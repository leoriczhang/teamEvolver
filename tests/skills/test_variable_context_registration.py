"""Variable assets preserve literal requests and an operator's edit boundary."""
from __future__ import annotations

import copy

import pytest

from team_replay.execution_plan import render_components
from team_replay.variable_context import variable_context_groups
from team_skills.execution.contracts import apply_changes, editable_component_ids, plan_for, validate_profile
from team_skills.execution.variable_registration import variable_context_registration
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import component_change


def source():
    profile = validate_profile({
        "profile_id": "source", "runtime_type": "test", "agent_id": "agent", "user_id": "operator",
        "affected_skill_ids": [], "components": [{
            "component_id": "main", "kind": "agent_prompt", "slot": "system", "order": 0,
            "content": "Account {{account}}. Task: {{taskRules}}; repeat {{ taskRules }}.",
        }], "environment": {
            "template_variables": {"account": "A", "taskRules": ""},
            "template_input_contract": {"schema_version": "teamevolver.template-input-contract.v1",
                                        "variable_names": ["account", "taskRules"]},
            "request": {"model": "test-model", "temperature": 0},
        },
    })
    release = {"release_id": "source-release", "composition_hash": "source-hash", "document": {
        "execution_plan": plan_for(profile, profile["components"]), "skill_dependencies": [],
    }}
    return profile, release


def request():
    return {"profile_id": "derived", "source_release_id": "source-release",
            "managed_variables": ["taskRules"], "evolution_scope": "context", "variable_samples": [
                {"account": "A", "taskRules": "literal {{account}}\nfirst"},
                {"account": "B", "taskRules": "literal {{account}}\nfirst"},
                {"account": "C", "taskRules": "second"}, {"account": "D", "taskRules": ""},
            ]}


def test_conversion_preserves_requests_deduplicates_values_and_is_nonmutating():
    profile, release = source()
    before = copy.deepcopy((profile, release))
    raw, preview = variable_context_registration(profile, release, request())
    result = validate_profile(raw)
    assert (profile, release) == before
    assert len(result["components"]) == 3
    assert preview["summary"]["managed_variables"][0]["unique_values"] == 2
    assert preview["summary"]["managed_variables"][0]["blank_samples"] == 1
    original = release["document"]["execution_plan"]
    converted = plan_for(result, result["components"])
    for sample in request()["variable_samples"]:
        left, right = copy.deepcopy(original), copy.deepcopy(converted)
        left["environment"]["template_variables"] = sample
        right["environment"]["template_variables"] = sample
        assert [x["content"] for x in render_components(left)] == [x["content"] for x in render_components(right)]
        assert len(render_components(right)) == 1
    assert variable_context_registration(profile, release, request())[1] == preview


def test_multiple_variables_register_independent_groups_without_cartesian_members():
    raw = request()
    raw["managed_variables"] = ["account", "taskRules"]
    raw["variable_samples"] = [{"account": account, "taskRules": rule}
                               for account in ("A", "B") for rule in ("first", "second")]
    derived, _ = variable_context_registration(*source(), raw)
    profile = validate_profile(derived)
    plan = plan_for(profile, profile["components"])
    groups = variable_context_groups(plan)
    assert len(plan["components"]) == 5  # one host + two values for each of two placeholders
    assert [(group["name"], group["value_count"], len(group["members"])) for group in groups] == [
        ("account", 2, 2), ("taskRules", 2, 2),
    ]
    original = source()[1]["document"]["execution_plan"]
    for sample in raw["variable_samples"]:
        before, after = copy.deepcopy(original), copy.deepcopy(plan)
        before["environment"]["template_variables"] = sample
        after["environment"]["template_variables"] = sample
        assert render_components(before)[0]["content"] == render_components(after)[0]["content"]


def test_context_only_scope_rejects_template_edits_and_preserves_original_selector():
    raw, preview = variable_context_registration(*source(), request())
    saved = validate_profile(raw)
    plan = plan_for(saved, saved["components"])
    context = next(x for x in plan["components"] if x["kind"] == "context_fragment")
    host = next(x for x in plan["components"] if x["kind"] == "agent_prompt")
    assert editable_component_ids(plan) == {x["component_id"] for x in plan["components"] if x is not host}
    with pytest.raises(SkillLibraryError, match="editable scope"):
        apply_changes(plan, [component_change("wrong scope", component_id=host["component_id"])],
                      allowed_modules=["prompt"])
    updated = apply_changes(plan, [component_change("improved", component_id=context["component_id"])],
                            allowed_modules=["prompt"])
    assert updated["components"][0] == plan["components"][0]
    assert updated["components"][1]["binding"] == context["binding"]
    updated["environment"]["template_variables"] = {"account": "A", "taskRules": context["content"]}
    assert render_components(updated)[0]["content"] == "Account A. Task: improved; repeat improved."
    updated["environment"]["template_variables"]["taskRules"] = "unregistered"
    assert render_components(updated)[0]["content"] == "Account A. Task: unregistered; repeat unregistered."


@pytest.mark.parametrize("patch", [
    {"profile_id": "source"}, {"source_release_id": "stale"}, {"managed_variables": ["unknown"]},
    {"managed_variables": ["taskRules", "taskRules"]}, {"variable_samples": []},
    {"variable_samples": [{"taskRules": 3}]}, {"variable_samples": [{"taskRules": "x", "typo": "y"}]},
    {"variable_samples": [{"taskRules": ""}]}, {"evolution_scope": "all-agent"},
    {"variable_samples": [{"taskRules": "x", "account": float("nan")}]},
])
def test_invalid_or_ambiguous_registration_is_rejected(patch):
    raw = request()
    raw.update(patch)
    with pytest.raises(SkillLibraryError):
        variable_context_registration(*source(), raw)


def test_component_limit_and_combined_scope_are_explicit():
    raw = request()
    raw["variable_samples"] = [{"taskRules": f"rule {n}"} for n in range(32)]
    with pytest.raises(SkillLibraryError, match="32-component"):
        variable_context_registration(*source(), raw)
    raw = request()
    raw["evolution_scope"] = "prompt_and_context"
    derived, _ = variable_context_registration(*source(), raw)
    profile = validate_profile(derived)
    assert editable_component_ids(plan_for(profile, profile["components"])) == {
        c["component_id"] for c in profile["components"]
    }
