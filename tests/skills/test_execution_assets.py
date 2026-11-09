"""Execution-asset scope, immutable placement and independent-verification gates."""

from __future__ import annotations

import copy

import pytest

from team_replay.execution_plan import normalize_execution_plan, validate_execution_plan_pair
from team_skills.execution.candidates import publication_gate, revise_execution_detail
from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.evolution import attach_execution_proposal, fixed_skill_material, prompt_evidence_gap
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import component_change, profile_raw
from tests.support.skills import _skill_md


def baseline():
    profile = validate_profile(profile_raw())
    return plan_for(profile, profile["components"])


def detail():
    return {"baseline_execution_plan": baseline(), "allowed_modules": ["prompt"], "skill_changed": False,
            "affected_skill_ids": ["demo", "helper"], "execution_profile_id": "instructor",
            "base_release_id": "baseline", "skill_dependencies": [{"name": "demo", "version": 1}],
            "replay_cases": [{"case_id": "both-skills", "skill_ids": ["demo", "helper"]}]}


def test_profile_validation_is_deterministic_nonmutating_and_matches_replay_contract():
    raw = profile_raw()
    saved = copy.deepcopy(raw)
    profile = validate_profile(raw)
    value = plan_for(profile, profile["components"])
    assert normalize_execution_plan(value)["components"] == [
        {**part, "revision": "1"} for part in value["components"]
    ]
    assert all(part["revision"] == 1 for part in value["components"])
    assert raw == saved
    assert profile["affected_skill_ids"] == ["demo", "helper"]


@pytest.mark.parametrize("change", [
    {"kind": "te_judge_prompt"}, {"slot": "hidden-system"}, {"content": " "}, {"order": True},
    {"path": "/some/runtime.py"},
])
def test_profile_rejects_unsupported_or_unbound_asset_shapes(change):
    raw = profile_raw()
    raw["components"][0].update(change)
    with pytest.raises(SkillLibraryError):
        validate_profile(raw)


@pytest.mark.parametrize("environment", [
    {"api_key": "example-not-a-real-key"}, {"nested": {"Authorization": "example"}},
    {"rows": [{"access_token": "example"}]},
    {"request": {"apiKey": "example"}}, {"request": {"api-key": "example"}},
    {"request": {"headers": {"x-api-key": "example"}}},
])
def test_profile_environment_does_not_store_credentials(environment):
    raw = profile_raw()
    raw["environment"] = environment
    with pytest.raises(SkillLibraryError, match="credentials"):
        validate_profile(raw)


def test_text_change_preserves_shared_component_placement_and_frozen_source():
    source = baseline()
    frozen = copy.deepcopy(source)
    result = apply_changes(source, [component_change()], allowed_modules=["prompt"])
    assert source == frozen
    assert result["components"][1] == source["components"][1]
    assert result["components"][0]["binding"] == source["components"][0]["binding"]
    assert result["components"][0]["revision"] == 2
    assert result["target_component_ids"] == ["main-prompt"]
    source["target_component_ids"] = result["target_component_ids"]
    validate_execution_plan_pair(source, result)


@pytest.mark.parametrize("change", [
    {"slot": "developer"}, {"binding": {"agent_id": "other"}}, {"order": 7},
    {"component_id": "unknown"}, {"reason": ""}, {"evidence_refs": []},
])
def test_component_editor_cannot_change_bindings_or_remove_provenance(change):
    with pytest.raises(SkillLibraryError):
        apply_changes(baseline(), [{**component_change(), **change}], allowed_modules=["prompt"])


def test_scope_and_noop_enforcement_do_not_silently_generate_another_treatment():
    with pytest.raises(SkillLibraryError, match="scope_violation"):
        apply_changes(baseline(), [component_change()], allowed_modules=["skill"])
    with pytest.raises(SkillLibraryError, match="no component"):
        apply_changes(baseline(), [component_change("Original business Prompt")], allowed_modules=["prompt"])


def test_prompt_only_material_preserves_complete_skill_bytes_and_rejects_skill_modification():
    bundle = {"SKILL.md": _skill_md("demo"), "scripts/run.py": b"print('unchanged')\n"}
    before = copy.deepcopy(bundle)
    skill, diff = fixed_skill_material("demo", bundle, {"action": "improve_skill"})
    assert bundle == before and skill["name"] == "demo"
    assert diff["changed_count"] == 0 and diff["before_tree_sha256"] == diff["after_tree_sha256"]
    for proposed in ({"action": "create_skill"}, {"skill": {"content": "rewrite"}},
                     {"skill": {"file_changes": [{"path": "scripts/run.py", "content": "modified"}]}}):
        with pytest.raises(SkillLibraryError, match="scope_violation"):
            fixed_skill_material("demo", bundle, proposed)


def test_candidate_revision_clears_old_independent_validation_and_tracks_actual_scope():
    source = detail()
    source["independent_validation"] = {"status": "passed", "candidate_revision": 1}
    changed = revise_execution_detail(source, [component_change()])
    assert changed["actual_modules"] == ["prompt"]
    assert "independent_validation" not in changed
    assert source["independent_validation"]["status"] == "passed"
    source["skill_changed"] = True
    assert revise_execution_detail(source, [component_change()])["actual_modules"] == ["skill", "prompt"]


def test_attach_joint_plan_explicitly_freezes_skill_change_permission():
    context = detail()
    result = attach_execution_proposal({"component_changes": [component_change()]}, {}, context=context,
                                       allowed_modules=["skill", "prompt"], working_bundle={}, skill_changed=True)
    saved = result["_execution_detail"]
    assert saved["baseline_execution_plan"]["binding"]["allow_skill_change"] is True
    assert saved["candidate_execution_plan"]["binding"]["allow_skill_change"] is True
    validate_execution_plan_pair(saved["baseline_execution_plan"], saved["candidate_execution_plan"])


def test_publication_requires_observed_execution_every_affected_skill_and_matching_revision():
    saved = revise_execution_detail(detail(), [component_change()])
    assert publication_gate(saved, {}, candidate_revision=2)["passed"] is False
    trusted = {"validation_run_id": "validate:reviewed", "validation_payload_hash": "a" * 64,
               "validation_result_hash": "b" * 64}
    saved["validation_plan"] = {"input_hash": "c" * 64, "holdout": {"status": "ready"}}
    saved["independent_validation"] = {"status": "passed", "evidence_ref": "artifact:checked",
        "candidate_revision": 2, "schema_version": "teamevolver.independent-validation.v2", **trusted,
        "validation_input_hash": "c" * 64, "case_ids": [case["case_id"] for case in saved["replay_cases"]]}
    evaluation = {"execution_gate": {"status": "passed"}, "probe_gate": {"status": "passed"},
                  "cost_gate": {"status": "passed"}}
    assert publication_gate(saved, evaluation, candidate_revision=2, **trusted)["passed"] is True
    assert publication_gate(saved, evaluation, candidate_revision=3)["passed"] is False
    saved["replay_cases"][0]["skill_ids"] = ["demo"]
    assert "regression_coverage_missing" in publication_gate(saved, evaluation, candidate_revision=2)["reasons"]


def test_upstream_template_or_empty_model_messages_do_not_prove_final_prompt():
    assert prompt_evidence_gap([{"system_prompt": "Old template", "turns": []}])
    assert prompt_evidence_gap([{"turns": [{"request_context": {"completeness": "complete",
                                                                 "evidence_level": "template", "messages": [{}]}}]}])
    assert prompt_evidence_gap([{"turns": [{"request_context": {"completeness": "complete",
                                                                 "evidence_level": "final_request"}}]}])
    observed = {"completeness": "complete", "evidence_level": "final_request",
                "messages": [{"role": "system", "content": "Real"}]}
    assert prompt_evidence_gap([{"turns": [{"request_context": observed}]}]) == ""


@pytest.mark.parametrize("messages", [[{}], [{"role": "system", "content": ""}]])
def test_complete_tag_without_actual_prompt_content_is_not_sufficient_evidence(messages):
    assert prompt_evidence_gap([{"turns": [{"request_context": {"completeness": "complete",
                                                                 "evidence_level": "final_request",
                                                                 "messages": messages}}]}])


def test_profile_and_change_invalid_identifier_types_raise_domain_validation_errors():
    raw = profile_raw()
    raw["affected_skill_ids"] = [{}]
    with pytest.raises(SkillLibraryError):
        validate_profile(raw)
    with pytest.raises(SkillLibraryError):
        apply_changes(baseline(), [{**component_change(), "component_id": []}], allowed_modules=["prompt"])
