"""Prompt-only attribution uses observed requests without inventing Skill usage."""
from __future__ import annotations

import copy

import pytest

from team_skills.evolution.session_processing import evidence_gap, groups
from team_skills.evolution.stages.analyze import _compose_system, build_analysis_payload
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.evolution import fixed_skill_material, prompt_evidence_gap, prompt_session_matches
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import profile_raw


def pure_profile():
    raw = profile_raw()
    raw["affected_skill_ids"] = []
    return validate_profile(raw)


def source(index=1, *, content="Original business Prompt", verified=True):
    ref = {"source_session_key": f"source-{index}", "trace_id": f"trace-{index}",
           "revision": "fact-r1", "context_revision": "context-r1"}
    return {
        "session_id": f"session-{index}", "user_alias": f"user-{index}", "runtime_context": {"agent_id": "agent-1"},
        "used_skills": [], "provenance": {"schema_version": "session-source.v1",
            "source_session_key": ref["source_session_key"], "identity_verified": verified,
            "trace_identity_verified": True, "target_trace_refs": [ref], "trace_revisions": [ref]},
        "turns": [{"turn_num": 1, "source_trace_id": ref["trace_id"], "used_skills": [],
            "prompt_text": "A user corrected a shared rule", "response_text": "Recorded result",
            "request_context": {"evidence_level": "generation_input", "completeness": "unknown",
                "generation_requests": [{"observation_id": f"generation-{index}",
                    "source_id": "upstream", "model_call_id": f"call-{index}",
                    "input": [{"role": "system", "content": content},
                              {"role": "developer", "content": "Keep the shared business policy"},
                              {"role": "user", "content": "A user corrected a shared rule"}]}]}}],
    }


def test_profile_zero_skill_dependencies_is_explicit_and_does_not_mutate_input():
    profile = pure_profile()
    assert profile["affected_skill_ids"] == []
    plan = plan_for(profile, profile["components"])
    assert all(part["binding"]["affected_skill_ids"] == [] for part in plan["components"])


def test_zero_skill_material_contains_no_fabricated_name_or_file():
    material, diff = fixed_skill_material(None, {}, {"action": "change_execution"})
    assert material == {} and diff["changed_count"] == 0
    for result in ({"action": "create_skill"}, {"skill": {"name": "fake", "content": "made up"}}):
        with pytest.raises(SkillLibraryError):
            fixed_skill_material(None, {}, result)


@pytest.mark.parametrize("verified,content,metadata,expected", [
    (True, "Original business Prompt", {}, True),
    (False, "Original business Prompt", {}, False),
    (True, "Another Agent baseline", {"execution_profile_id": "instructor"}, False),
    (True, "Original business Prompt", {"execution_profile_id": "other"}, False),
    (True, "Original business Prompt", {"agent_id": "other-agent"}, False),
])
def test_profile_binding_requires_observed_prompt_and_rejects_conflicting_identity(
    verified, content, metadata, expected,
):
    session = source(content=content, verified=verified)
    session["runtime_context"].update(metadata)
    profile = pure_profile()
    assert prompt_session_matches(session, plan_for(profile, profile["components"])) is expected


def test_pure_prompt_sources_group_by_profile_with_unchanged_independent_origin_gate():
    profile = pure_profile()
    result = groups([source(1), source(2), source(3, content="Unrelated Prompt")],
                    execution_profile=profile, baseline=plan_for(profile, profile["components"]))
    assert [s["session_id"] for s in result["execution:instructor"]] == ["session-1", "session-2"]
    assert list(result) == ["execution:instructor"]  # Unrelated sources are not another requested asset scope.
    assert not evidence_gap(result["execution:instructor"], {"min_group_sessions": 2, "min_group_users": 2})
    duplicate = copy.deepcopy(source(1))
    duplicate["session_id"] = "another-formal-id"
    assert "Sessions" in evidence_gap([source(1), duplicate], {"min_group_sessions": 2, "min_group_users": 2})
    same_user = source(2)
    same_user["user_alias"] = "user-1"
    assert "users" in evidence_gap([source(1), same_user], {"min_group_sessions": 2, "min_group_users": 2})


def test_every_frozen_prompt_component_must_be_observed_in_the_same_request_order():
    profile = pure_profile()
    profile["components"].insert(1, {**profile["components"][0], "component_id": "developer-prompt",
                                    "slot": "developer", "order": 2, "content": "Second business rule"})
    plan = plan_for(profile, profile["components"])
    session = source()
    assert not prompt_session_matches(session, plan)
    messages = session["turns"][0]["request_context"]["generation_requests"][0]["input"]
    messages.insert(2, {"role": "developer", "content": "Second business rule"})
    assert prompt_session_matches(session, plan)
    messages[0], messages[1] = messages[1], messages[0]
    assert not prompt_session_matches(session, plan)


@pytest.mark.parametrize("mutation", ["missing", "changed", "wrong_role", "wrong_order"])
def test_a_registered_fixed_context_requires_its_original_observed_content_role_and_order(mutation):
    profile, session = pure_profile(), source()
    plan = plan_for(profile, profile["components"])
    assert prompt_session_matches(session, plan)
    messages = session["turns"][0]["request_context"]["generation_requests"][0]["input"]
    if mutation == "missing":
        messages.pop(1)
    elif mutation == "changed":
        messages[1]["content"] += "unregistered addition"
    elif mutation == "wrong_role":
        messages[1]["role"] = "user"
    else:
        messages[0], messages[1] = messages[1], messages[0]
    assert not prompt_session_matches(session, plan)
    assert groups([session], execution_profile=profile, baseline=plan) == {}


def test_fixed_context_alone_does_not_enable_a_business_prompt_profile_without_an_agent_prompt():
    profile, session = pure_profile(), source()
    profile["components"] = [part for part in profile["components"] if part["kind"] == "context_fragment"]
    assert not prompt_session_matches(session, plan_for(profile, profile["components"]))


def test_joint_prompt_evidence_requires_the_registered_prompt_and_context_in_one_model_request():
    profile, session = pure_profile(), source()
    plan = plan_for(profile, profile["components"])
    assert prompt_evidence_gap([session], plan) == ""
    request = session["turns"][0]["request_context"]
    actual = request["generation_requests"][0]["input"]
    request["generation_requests"] = [{"input": [actual[0], actual[2]]}, {"input": [actual[1], actual[2]]}]
    assert prompt_evidence_gap([session], plan).startswith("prompt_provenance_missing")
    request["generation_requests"] = [{"input": copy.deepcopy(actual)}]
    assert prompt_evidence_gap([session], plan) == ""


def test_complete_final_request_still_requires_the_actual_registered_prompt_and_context_bytes():
    profile, session = pure_profile(), source(verified=False)
    plan = plan_for(profile, profile["components"])
    request = session["turns"][0]["request_context"]
    actual = request.pop("generation_requests")[0]["input"]
    request.update(evidence_level="final_request", completeness="complete", messages=actual)
    assert prompt_evidence_gap([session], plan) == ""  # Existing complete-request eligibility is unchanged.
    request["messages"][0]["content"] = "Different business Prompt"
    assert prompt_evidence_gap([session], plan).startswith("prompt_provenance_missing")
    request["messages"][0]["content"] = "Original business Prompt"
    request["messages"][1]["content"] = "Different business Context"
    assert prompt_evidence_gap([session], plan).startswith("prompt_provenance_missing")


def test_same_text_without_upstream_agent_or_profile_binding_cannot_guess_the_profile():
    profile = pure_profile()
    plan = plan_for(profile, profile["components"])
    session = source()
    session.pop("runtime_context")
    assert not prompt_session_matches(session, plan)
    context = session["turns"][0]["request_context"]
    context["execution_binding"] = {"execution_profile_id": "instructor", "evidence_level": "upstream_metadata"}
    assert prompt_session_matches(session, plan)
    context["execution_binding"]["agent_id"] = "another-agent"
    assert not prompt_session_matches(session, plan)


def test_prompt_analysis_receives_original_generation_input_and_does_not_invent_skill_experiences():
    session = source()
    before = copy.deepcopy(session)
    payload = build_analysis_payload(session)
    assert payload["used_skills"] == []
    request = payload["observed_request_contexts"][0]
    assert request["trace_id"] == "trace-1"
    assert request["request_context"] == before["turns"][0]["request_context"]
    assert request["request_context"]["completeness"] == "unknown"
    request["request_context"]["generation_requests"][0]["input"][0]["content"] = "test mutation"
    assert session == before
    system = _compose_system("Existing analysis policy", [])
    assert "业务 Prompt/上下文规则缺陷" in system and "skill_experiences 必须为空" in system
    assert "业务 Prompt/上下文规则缺陷" not in _compose_system("Existing analysis policy", ["actual-skill"])
