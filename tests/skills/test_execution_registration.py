"""Registration previews and independent variable collections share execution semantics."""
from __future__ import annotations

import copy

import pytest

from team_replay.execution_plan import render_components
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.registration import agent_integration_template, registration_preview
from team_skills.execution.variable_registration import variable_context_registration
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import profile_raw


def _raw():
    raw = profile_raw("live-agent")
    raw["affected_skill_ids"] = []
    raw["components"] = [{"component_id": "live-template", "kind": "agent_prompt", "slot": "system",
                          "order": 0, "content": "Rules: {{rules}}; Examples: {{examples}}"}]
    raw["environment"] = {
        "template_variables": {"rules": "first rule", "examples": "example A"},
        "template_input_contract": {"schema_version": "teamevolver.template-input-contract.v1",
                                    "variable_names": ["rules", "examples"]},
    }
    return raw


def _source():
    profile = validate_profile(_raw())
    return profile, {"release_id": "release-1", "composition_hash": "release-hash", "document": {
        "execution_plan": plan_for(profile, profile["components"]), "skill_dependencies": [],
    }}


def test_preview_preserves_template_and_defaults_without_mutating_registration_input():
    raw = _raw()
    before = copy.deepcopy(raw)
    preview = registration_preview(raw)
    assert raw == before
    assert preview["summary"]["template_variables"] == [
        {"name": "examples", "component_ids": ["live-template"]},
        {"name": "rules", "component_ids": ["live-template"]},
    ]
    assert preview["components"][0]["content"] == raw["components"][0]["content"]
    assert registration_preview(raw) == preview
    raw["environment"]["template_variables"].pop("rules")
    with pytest.raises(SkillLibraryError):
        registration_preview(raw)


def test_independent_variable_collections_need_no_cross_product_or_dummy_values():
    profile, release = _source()
    body = {"profile_id": "managed", "source_release_id": "release-1", "managed_variables": ["rules", "examples"],
            "variable_values": {"rules": ["first rule", "second rule", "first rule", ""],
                                "examples": ["example A", "example B", "example C"]}}
    derived, preview = variable_context_registration(profile, release, body)
    value = validate_profile(derived)
    assert len(value["components"]) == 6  # template + 2 rules + 3 examples; never 2 * 3 registrations
    assert preview["summary"]["input_mode"] == "variable_values"
    assert preview["summary"]["sample_count"] == 7
    original = release["document"]["execution_plan"]
    registered = plan_for(value, value["components"])
    for rule in ("first rule", "second rule", "unregistered", ""):
        original["environment"]["template_variables"]["rules"] = rule
        registered["environment"]["template_variables"]["rules"] = rule
        assert render_components(original)[0]["content"] == render_components(registered)[0]["content"]


@pytest.mark.parametrize("values", [None, {}, {"rules": []}, {"rules": [1]}, {"rules": ["x"], "typo": ["y"]}])
def test_grouped_value_registration_rejects_incomplete_or_undeclared_inputs(values):
    with pytest.raises(SkillLibraryError):
        variable_context_registration(*_source(), {
            "profile_id": "managed", "source_release_id": "release-1", "managed_variables": ["rules"],
            "variable_values": values,
        })


def test_grouped_and_row_input_modes_are_mutually_exclusive():
    with pytest.raises(SkillLibraryError, match="exactly one"):
        variable_context_registration(*_source(), {
            "profile_id": "managed", "source_release_id": "release-1", "managed_variables": ["rules"],
            "variable_values": {"rules": ["x"]}, "variable_samples": [{"rules": "x"}],
        })


def test_integration_template_reports_exact_versions_and_labels_defaults_as_examples():
    profile, release = _source()
    before = copy.deepcopy((profile, release))
    result = agent_integration_template(profile, release)
    assert (profile, release) == before
    observation = result["metadata"]["teamevolver_execution"]
    assert observation["release_id"] == "release-1" and observation["agent_id"] == profile["agent_id"]
    template = release["document"]["execution_plan"]["components"][0]
    assert observation["template_inputs"]["template_refs"] == {"live-template": {
        "template_id": "live-template", "revision": "1", "content_hash": template["content_hash"],
    }}
    assert observation["template_inputs"]["template_variables"] == _raw()["environment"]["template_variables"]
    assert any("not evidence" in text for text in result["requirements"])
