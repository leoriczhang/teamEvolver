"""A stable template attributes different variable-bound requests without text guessing."""
from __future__ import annotations

import copy

import pytest

from team_skills.evolution.session_processing import groups
from team_skills.execution.evolution import prompt_evidence_gap, prompt_session_matches
from team_skills.execution.inputs import verified_source_execution_inputs
from tests.support.shared_template_sources import (
    shared_template_plan,
    shared_template_profile,
    shared_template_source,
)


def test_one_frozen_template_groups_different_variables_and_does_not_use_profile_defaults():
    plan = shared_template_plan()
    sources = [shared_template_source(1), shared_template_source(2)]
    before = copy.deepcopy((plan, sources))
    assert all(prompt_session_matches(source, plan) for source in sources)
    result = groups(sources, execution_profile=shared_template_profile(), baseline=plan)
    assert [source["session_id"] for source in result["execution:shared-customer"]] == ["source-1", "source-2"]
    assert not prompt_evidence_gap(sources, plan)
    inputs = [verified_source_execution_inputs(source, source["turns"][0], plan) for source in sources]
    assert inputs[0]["template_variables"] != inputs[1]["template_variables"]
    assert inputs[0]["template_refs"] == inputs[1]["template_refs"]
    assert (plan, sources) == before


@pytest.mark.parametrize("change", [
    "template_id", "revision", "content_hash", "extra_variable", "missing_variable", "changed_variable",
    "rendered_system", "changed_history", "history_position", "current_query", "agent", "profile", "runtime",
    "unverified_identity", "unverified_trace", "unbound", "missing_metadata", "context_only", "wrong_target",
    "source_ref", "private_grade", "untrusted_binding", "conflicting_metadata", "incomplete_request",
])
def test_shared_source_requires_exact_original_variables_rendering_order_and_provenance(change):
    plan, source = shared_template_plan(), shared_template_source()
    turn = source["turns"][0]
    request = turn["request_context"]
    inputs = request["trace_input"]["template_inputs"]
    messages = request["generation_requests"][0]["input"]["messages"]
    if change in {"template_id", "revision", "content_hash"}:
        inputs["template_refs"]["main-prompt"][change] = "0" * 64 if change == "content_hash" else "other"
    elif change == "extra_variable":
        inputs["template_variables"]["rogue"] = "not allowed"
    elif change == "missing_variable":
        inputs["template_variables"].pop("aiTask")
    elif change == "changed_variable":
        inputs["template_variables"]["userProfile"] = "Another client"
    elif change == "rendered_system":
        messages[0]["content"] += "extra instruction"
    elif change == "changed_history":
        inputs["input_messages"][0]["content"] += "invented prior refusal"
    elif change == "history_position":
        messages[1], messages[2] = messages[2], messages[1]
    elif change == "current_query":
        turn["prompt_text"] += "altered current request"
    elif change in {"agent", "profile"}:
        source["runtime_context"]["agent_id" if change == "agent" else "execution_profile_id"] = "another"
    elif change == "runtime":
        source["runtime"]["type"] = "another"
    elif change in {"unverified_identity", "unverified_trace"}:
        field = "identity_verified" if change == "unverified_identity" else "trace_identity_verified"
        source["provenance"][field] = False
    elif change == "unbound":
        source.pop("runtime_context")
    elif change == "missing_metadata":
        request["trace_input"].pop("template_inputs")
    elif change == "context_only":
        turn["context_only"] = True
    elif change == "wrong_target":
        source["provenance"]["target_trace_refs"][0]["trace_id"] = "context-trace"
    elif change == "source_ref":
        inputs["source_ref"] = {"session_id": "forged"}
    elif change == "private_grade":
        messages.append({"role": "user", "content": "PRIVATE_OLD_GRADE"})
    elif change == "untrusted_binding":
        request["execution_binding"] = {"agent_id": "agent-1", "evidence_level": "guessed"}
    elif change == "conflicting_metadata":
        request["template_inputs"] = {**copy.deepcopy(inputs), "input_messages": []}
    elif change == "incomplete_request":
        request["evidence_level"] = "final_request"
    assert not prompt_session_matches(source, plan)
    assert prompt_evidence_gap([source], plan).startswith("prompt_provenance_missing")


def test_explicit_request_metadata_also_works_without_trace_input_wrapper():
    source, plan = shared_template_source(), shared_template_plan()
    request = source["turns"][0]["request_context"]
    request["template_inputs"] = request["trace_input"].pop("template_inputs")
    assert prompt_session_matches(source, plan)
