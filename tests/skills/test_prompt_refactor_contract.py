"""Whole-component refinement uses exact tools and frozen metadata, without a review model."""
from __future__ import annotations

import copy

import pytest

from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.planner import propose_prompt
from tests.support.execution import profile_raw
from tests.support.prompt_editor import PromptEditorLLM, assert_successful_prompt_edit


def _inputs():
    raw = profile_raw()
    raw["components"][0]["content"] = (
        "## Role\nSpeak only as the customer.\n{{domain}}\n"
        "## Constraints\nUse only the original time window.\nReturn one reply.\n"
        "## Self-check\nReturn only one reply.\nKeep the original time window.\n"
    )
    raw["environment"]["template_variables"] = {"domain": "Frozen business domain"}
    profile = validate_profile(raw)
    baseline = plan_for(profile, profile["components"])
    inherited = baseline["components"][0]["content"] + "## Unverified inherited policy\nBlock every clarification.\n"
    working = apply_changes(baseline, [{"component_id": "main-prompt", "content": inherited,
        "reason": "Previous unverified implementation hypothesis", "evidence_refs": ["previous-source"]}],
        allowed_modules=["prompt"])
    context = {"baseline_execution_plan": baseline, "working_execution_plan": working,
               "active_candidate": {"candidate_id": "pending", "revision": 3}}
    sessions = [{"session_id": "source", "turns": [{"trace_id": "target", "prompt_text": "Original business task",
        "messages": [{"role": "user", "content": "Original business task"},
                     {"role": "assistant", "content": "Original response"},
                     {"role": "user", "content": (
                         "The unconditional inherited restriction blocks legitimate tasks."
                     )}]}]}]
    return sessions, context


@pytest.mark.asyncio
async def test_full_component_exact_refinement_preserves_frozen_metadata_and_only_dispatches_editor_rounds():
    sessions, context = _inputs()
    before = copy.deepcopy((sessions, context))
    replacement = ("## Customer contract\n{{domain}}\nSpeak only as the customer.\n"
                   "Use only the original time window.\nReturn one reply.\n")
    model = PromptEditorLLM(["source"], replacement=replacement)
    result = await propose_prompt(model, sessions=sessions, context=context)
    assert_successful_prompt_edit(model, result)
    candidate = apply_changes(context["working_execution_plan"], result["component_changes"],
                              allowed_modules=["prompt"])
    assert candidate["binding"] == context["working_execution_plan"]["binding"]
    assert candidate["environment"] == context["working_execution_plan"]["environment"]
    assert candidate["components"][1] == context["working_execution_plan"]["components"][1]
    assert result["editor_audit"]["mechanical_only"] is True
    assert result["editor_audit"]["applied_edits"] == 1
    assert len(replacement) < len(context["working_execution_plan"]["components"][0]["content"])
    assert (sessions, context) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["A whole-body rewrite without the required variable.",
                                         "{{invented_variable}} A compact new body."])
async def test_whole_component_refinement_cannot_bypass_the_original_template_contract(replacement):
    sessions, context = _inputs()
    before = copy.deepcopy((sessions, context))
    model = PromptEditorLLM(["source"], replacement=replacement)
    result = await propose_prompt(model, sessions=sessions, context=context)
    assert result["action"] == "skip" and not result.get("component_changes")
    assert all(options["trace_name"] == "team-skill-evolver:prompt_edit" for _, options in model.calls)
    assert (sessions, context) == before
