"""Real-Agent-style Langfuse observations reach the existing Prompt grouping path."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from session_ingestion.adapters._shared.langfuse_convert import convert_langfuse_session
from session_ingestion.source_revisions import plan_snapshot, preserve_requests, source_identity
from team_replay.execution_inputs import EXECUTION_INPUTS_SCHEMA, case_execution_plan_pair
from team_replay.execution_plan import render_components
from team_replay.variable_context import source_value_hash
from team_skills.evolution.replay_case_synthesis import LLMCrossSessionCaseSynthesizer
from team_skills.evolution.session_processing import eligible_evidence, groups, project_prompt_session
from team_skills.execution.evolution import prompt_session_matches
from team_skills.execution.inputs import shared_template_match_report
from team_skills.execution.planner import prompt_editor_input
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport
from tests.support.variable_context import inline_plan


def observed_trace(identity="trace-one", *, rule="Original A", plan=None):
    plan = plan or inline_plan()
    host = plan["components"][0]
    inputs = {"schema_version": EXECUTION_INPUTS_SCHEMA, "template_refs": {host["component_id"]: {
        "template_id": host["binding"]["template_id"], "revision": host["revision"],
        "content_hash": host["content_hash"]}}, "template_variables": {"rules": rule, "person": "Reader"},
        "input_messages": [{"role": "user", "content": "Original public history"}]}
    effective = copy.deepcopy(plan)
    effective["environment"]["template_variables"] = inputs["template_variables"]
    query = "  Current request\r\n"
    messages = [{"role": part["role"], "content": part["content"]} for part in render_components(effective)]
    messages += copy.deepcopy(inputs["input_messages"]) + [{"role": "user", "content": query}]
    return {"id": identity, "sessionId": "conversation", "projectId": "project-one", "userId": "user-one",
            "timestamp": "2026-11-05T00:00:00Z", "updatedAt": "2026-11-05T00:01:00Z",
            "input": {"messages": [{"role": "user", "content": query}]}, "output": "Observed reply",
            "metadata": {"teamevolver_execution": {
                "schema_version": "teamevolver.execution-observation.v1",
                "execution_profile_id": plan["binding"]["profile_id"],
                "agent_id": plan["binding"]["agent_id"], "runtime_type": plan["binding"]["runtime_type"],
                "release_id": "actually-loaded-release", "composition_hash": "a" * 64,
                "template_inputs": inputs,
            }}, "observations": [{"id": "generation-" + identity, "type": "GENERATION",
                                   "startTime": "2026-11-05T00:00:00Z", "input": {"messages": messages},
                                   "output": "Observed reply", "model": "business-model"}]}


def ingest(traces, *, tenant="alpha", previous=None, project="project-one", session_id="conversation"):
    session = convert_langfuse_session({"id": session_id, "projectId": project}, traces)
    preserve_requests(session, traces)
    session["source_identity"] = source_identity(SimpleNamespace(host="https://langfuse.example"), {
        "provider": "langfuse", "project_id": project,
    }, {"projectId": project}, traces)
    return plan_snapshot(session, tenant_id=tenant, external_id=session_id, previous=previous)


def test_live_metadata_matches_one_profile_and_only_the_used_context_value_without_excel_reconstruction():
    plan = inline_plan()
    a, b = ingest([observed_trace(rule="Original A")]), ingest([observed_trace(rule="Original B")])
    before = copy.deepcopy((a, b))
    for session, selected in ((a, "rule-a"), (b, "rule-b")):
        report = shared_template_match_report(session, plan)
        assert report["matched"] is True
        assert report["targets"][0]["active_component_ids"] == ["main", selected, "shared"]
        assert session["used_skills"] == [] and session["injected_skills"] == []
        assert "reconstruction" not in session["turns"][0]["request_context"]["trace_input"]
        assert session["turns"][0]["prompt_text"] == "  Current request\r\n"
        result = groups([session], execution_profile={"profile_id": "variable-rules", "affected_skill_ids": []},
                        baseline=plan)
        assert len(result["execution:variable-rules"]) == 1
    assert (a, b) == before


@pytest.mark.parametrize("mutation", [
    "profile", "agent", "runtime", "template", "revision", "hash", "variable", "model_input",
    "missing_generation", "flat_conflict", "input_conflict", "schema", "extra_field", "forged_source",
    "invalid_json", "project_conflict", "identity_conflict", "revision_conflict",
])
def test_live_declarations_do_not_override_original_model_inputs_or_source_identity(mutation):
    trace = observed_trace()
    observed = trace["metadata"]["teamevolver_execution"]
    reference = observed["template_inputs"]["template_refs"]["main"]
    if mutation in {"profile", "agent", "runtime"}:
        field = {"profile": "execution_profile_id", "agent": "agent_id", "runtime": "runtime_type"}[mutation]
        observed[field] = "other"
    elif mutation in {"template", "revision", "hash"}:
        key = {"template": "template_id", "revision": "revision", "hash": "content_hash"}[mutation]
        reference[key] = "b" * 64 if mutation == "hash" else "other"
    elif mutation == "variable":
        observed["template_inputs"]["template_variables"]["rules"] = "Unobserved rule"
    elif mutation == "model_input":
        trace["observations"][0]["input"]["messages"][0]["content"] += "unregistered rule"
    elif mutation == "missing_generation":
        trace["observations"] = []
    elif mutation == "flat_conflict":
        trace["metadata"]["execution_profile_id"] = "conflicting-profile"
    elif mutation == "input_conflict":
        trace["input"]["template_inputs"] = copy.deepcopy(observed["template_inputs"])
        trace["input"]["template_inputs"]["input_messages"] = []
    elif mutation == "schema":
        observed["schema_version"] = "future-unsupported"
    elif mutation == "extra_field":
        observed["claimed_used"] = True
    elif mutation == "forged_source":
        observed["template_inputs"]["source_ref"] = {"session_id": "another-tenant"}
    elif mutation == "invalid_json":
        observed["template_inputs"]["template_variables"]["rules"] = float("nan")
    elif mutation == "project_conflict":
        trace["projectId"] = "another-project"
    session = ingest([trace])
    if mutation in {"identity_conflict", "revision_conflict"}:
        session["provenance"]["identity_conflict" if mutation == "identity_conflict" else "revision_conflicts"] = True
    report = shared_template_match_report(session, inline_plan())
    assert report["matched"] is False and report["reason"]
    assert report["targets"] == []
    assert not prompt_session_matches(session, inline_plan())
    # Failed attribution does not discard an otherwise ingestible source reply.
    assert session["turns"][0]["response_text"] == "Observed reply"


def test_live_template_metadata_does_not_turn_continuous_history_into_new_independent_evidence():
    trace = observed_trace()
    first = ingest([trace])
    repeat = ingest([copy.deepcopy(trace)], previous=first)
    assert repeat["provenance"]["target_trace_refs"] == []
    assert eligible_evidence([repeat]) == []
    second_trace = observed_trace("trace-two", rule="Original B")
    second_trace["timestamp"] = "2026-11-05T00:02:00Z"
    growth = ingest([trace, second_trace], previous=first)
    assert growth["provenance"]["source_session_key"] == first["provenance"]["source_session_key"]
    assert [ref["trace_id"] for ref in growth["provenance"]["target_trace_refs"]] == ["trace-two"]
    targets = eligible_evidence([growth])[0]
    report = shared_template_match_report(targets, inline_plan())
    assert report["matched"] and [r["source_ref"]["trace_id"] for r in report["targets"]] == ["trace-two"]
    other_tenant = ingest([trace], tenant="beta")
    assert other_tenant["provenance"]["source_session_key"] != first["provenance"]["source_session_key"]


def test_explicit_observation_errors_cannot_fall_back_to_a_legacy_fixed_prompt_match():
    from team_skills.execution.contracts import plan_for, validate_profile
    from tests.support.execution import profile_raw

    profile = validate_profile(profile_raw())
    trace = observed_trace()
    trace["metadata"]["teamevolver_execution"].update(execution_profile_id=profile["profile_id"],
                                                        agent_id=profile["agent_id"],
                                                        runtime_type=profile["runtime_type"],
                                                        schema_version="unsupported")
    trace["observations"][0]["input"]["messages"] = [{"role": "system", "content": "Original business Prompt"}]
    assert not prompt_session_matches(ingest([trace]), plan_for(profile, profile["components"]))


def test_one_continuous_session_contributes_only_its_own_targets_to_each_registered_profile():
    first_plan = inline_plan()
    second_plan = copy.deepcopy(first_plan)
    second_plan["binding"]["profile_id"] = "second-profile"
    second_plan["target_component_ids"] = ["second-" + identity for identity in second_plan["target_component_ids"]]
    for part in second_plan["components"]:
        part["component_id"] = "second-" + part["component_id"]
        if "inline_variable" in part["binding"]:
            part["binding"]["inline_variable"]["component_id"] = "second-main"
    second_plan["components"][0]["content"] += "|Second profile instructions"
    second_plan["components"][0]["content_hash"] = source_value_hash(second_plan["components"][0]["content"])
    second_plan["components"][0]["binding"]["template_id"] = "second-template"
    first = observed_trace("first-target", plan=first_plan)
    second = observed_trace("second-target", plan=second_plan)
    second["timestamp"] = "2026-11-05T00:02:00Z"
    session = ingest([first, second])
    before = copy.deepcopy(session)
    for plan, target, excluded in ((first_plan, "first-target", "second-target"),
                                   (second_plan, "second-target", "first-target")):
        assert not prompt_session_matches(session, plan)
        selected = project_prompt_session(session, plan)
        assert selected is not None and prompt_session_matches(selected, plan)
        assert selected["session_id"] == session["session_id"]
        assert selected["provenance"]["source_session_key"] == session["provenance"]["source_session_key"]
        assert len(selected["turns"]) == 2
        assert [turn["source_trace_id"] for turn in selected["_evidence_turns"]] == [target]
        assert [ref["trace_id"] for ref in selected["provenance"]["target_trace_refs"]] == [target]
        assert [ref["trace_id"] for ref in selected["provenance"]["context_trace_refs"]] == [excluded]
        _, payload = prompt_editor_input([selected], {"baseline_execution_plan": plan})
        assert target in payload["allowed_evidence_refs"] and excluded not in payload["allowed_evidence_refs"]
        assert [turn["source_trace_id"] for turn in payload["evidence"][0]["turns"]] == [target]
        assert payload["evidence"][0]["analysis_context"]["context_only"] is True
        grouped = groups([session], execution_profile={"profile_id": plan["binding"]["profile_id"],
                                                      "affected_skill_ids": []}, baseline=plan)
        assert len(grouped["execution:" + plan["binding"]["profile_id"]]) == 1
    assert session == before


def test_a_different_template_version_is_context_not_evidence_for_the_registered_version():
    plan = inline_plan()
    old_plan = copy.deepcopy(plan)
    old_plan["components"][0]["revision"] = "2"
    old = observed_trace("different-version", plan=old_plan)
    current = observed_trace("registered-version", plan=plan)
    current["timestamp"] = "2026-11-05T00:02:00Z"
    selected = project_prompt_session(ingest([old, current]), plan)
    assert [ref["trace_id"] for ref in selected["provenance"]["target_trace_refs"]] == ["registered-version"]
    assert [ref["trace_id"] for ref in selected["provenance"]["context_trace_refs"]] == ["different-version"]


def test_explicit_pure_prompt_scope_ignores_skill_mutation_branches_and_includes_incidental_reads():
    source = ingest([observed_trace()])
    source["turns"][0]["used_skills"] = ["existing-skill"]
    source["_skills_referenced"] = {"existing-skill"}
    selected = groups([source], execution_profile={"profile_id": "variable-rules", "affected_skill_ids": []},
                      baseline=inline_plan())
    assert list(selected) == ["execution:variable-rules"]
    assert selected["execution:variable-rules"][0]["provenance"]["source_session_key"] == source["provenance"][
        "source_session_key"]
    assert "existing-skill" in groups([source])  # Original Skill-only grouping remains intact.


@pytest.mark.asyncio
async def test_real_agent_sources_reach_existing_case_synthesis_and_replay_materialization_without_offline_flags():
    class OriginalRequestCaseModel:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **options):
            self.calls += 1
            payload = json.loads(messages[1]["content"])
            if payload.get("schema") == SCHEMA:
                payload = decode_evidence_transport(payload)
            self.payload = payload
            packet = payload["evidence_context"]
            assert packet["anchor_required_for_session_ids"] == []  # Never relabel a real conversation as offline.
            assert all(example["relationship"] == "recorded_session_turn" for example in packet["examples"])
            assert len(packet["verified_request_anchor_ids"]) == 2
            return json.dumps({"test_datasets": [{
                "name": "Original business request", "query_mode": "recorded_anchor",
                "anchor_id": example["example_id"], "source_session_ids": ["conversation-1", "conversation-2"],
                "evidence_window": "recent", "checks": [{"id": f"R{i}", "kind": "output",
                    "text": f"Shared observable business requirement {i}"} for i in range(6)],
            } for example in packet["examples"]]})

    plan = inline_plan()
    model = OriginalRequestCaseModel()
    original = [ingest([observed_trace("trace-1", rule="Original A")], session_id="conversation-1"),
                ingest([observed_trace("trace-2", rule="Original B")], session_id="conversation-2")]
    before = copy.deepcopy(original)
    cases = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name=None, sessions=original, candidate_skill={}, evidence_context={
            "execution_profile_id": plan["binding"]["profile_id"], "baseline_execution_plan": plan,
            "allowed_modules": ["prompt"],
        },
    )
    assert model.calls == 1 and len(cases) == 2 and original == before
    for case, rule in zip(cases, ["Original A", "Original B"], strict=True):
        assert case["query"] == "  Current request\r\n"
        assert case["provenance"]["source_runtime"]["type"] == "observed_http"
        assert case["provenance"]["execution_inputs"]["template_variables"]["rules"] == rule
        assert len(case["provenance"]["source_session_keys"]) == 2
        left, right = case_execution_plan_pair(plan, plan, case)
        assert left == right and left["environment"]["template_variables"]["rules"] == rule
        assert case["provenance"]["query_materialization"]["model_query_supplied"] is False
        assert "Observed reply" not in json.dumps(case["provenance"]["session_snapshot"])
