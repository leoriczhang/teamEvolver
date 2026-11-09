"""A shared template uses each Case's immutable source inputs in both branches."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from team_replay import engine
from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.contracts import ReplaySpec, ReplayTreatment, skill_job_from_spec
from team_replay.datasets.schema import DATASET_SCHEMA, DatasetSchemaError, normalize_case
from team_replay.execution_inputs import (
    EXECUTION_INPUTS_SCHEMA,
    TEMPLATE_INPUT_CONTRACT_SCHEMA,
    case_execution_plan_pair,
    normalize_execution_inputs,
    source_execution_inputs,
)
from team_replay.execution_plan import EXECUTION_PLAN_SCHEMA, ExecutionPlanError, execution_plan_hash
from team_replay.integration import FixedHost
from team_replay.runner import evaluate_spec
from tests.support.replay import judge


def plans():
    content = "Act as {{userProfile}}. Task conditions: {{aiTask}}."
    left = {
        "schema_version": EXECUTION_PLAN_SCHEMA,
        "binding": {"profile_id": "shared", "runtime_type": "observed_http", "agent_id": "agent-one"},
        "components": [{"component_id": "main", "kind": "agent_prompt", "slot": "system", "order": 0,
                        "binding": {"template_id": "customer-template"}, "revision": 1,
                        "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest()}],
        "target_component_ids": ["main"],
        "environment": {"template_variables": {"userProfile": "Default customer", "aiTask": ""},
                        "template_input_contract": {"schema_version": TEMPLATE_INPUT_CONTRACT_SCHEMA,
                                                    "variable_names": ["userProfile", "aiTask"]},
                        "request": {"model": "frozen-model", "temperature": 0, "tools": []}},
    }
    right = copy.deepcopy(left)
    new_content = content + " Preserve prior constraints."
    right["components"][0].update(content=new_content, revision=2,
                                   content_hash=hashlib.sha256(new_content.encode()).hexdigest())
    return left, right


def recorded_case(index=1, *, customer="Alice", task="", history="Public prior customer utterance"):
    left, _ = plans()
    ref = {"source_session_key": f"root-{index}", "trace_id": f"trace-{index}",
           "revision": f"revision-{index}", "context_revision": f"context-{index}"}
    raw = {"schema_version": EXECUTION_INPUTS_SCHEMA,
           "template_refs": {"main": {"template_id": "customer-template", "revision": 1,
                                       "content_hash": left["components"][0]["content_hash"]}},
           "template_variables": {"userProfile": customer, "aiTask": task},
           "input_messages": [{"role": "user", "content": history}]}
    turn = {"source_trace_id": ref["trace_id"], "prompt_text": f"Current salesperson message {index}",
            "response_text": "PRIVATE ORIGINAL ANSWER", "messages": [{"role": "user", "content": "PRIVATE GRADE"}],
            "request_context": {"trace_input": {"template_inputs": raw}}}
    source = {"session_id": f"source-{index}", "runtime": {"type": "observed_http"},
              "provenance": {"source_session_key": ref["source_session_key"], "target_trace_refs": [ref]},
              "turns": [turn]}
    inputs = source_execution_inputs(source, turn)
    return normalize_case({
        "case_id": f"case-{index}", "query": turn["prompt_text"], "skill_ids": [], "materials": [],
        "checks": [{"id": "R01", "kind": "output", "text": "Respond as the configured customer"}],
        "provenance": {"session_id": source["session_id"], "source_runtime": source["runtime"],
                       "target_trace_refs": [ref], "session_snapshot": source, "execution_inputs": inputs},
    })


def shared_spec(cases):
    left, right = plans()
    return ReplaySpec(run_id="shared-input-run", subject_kind="execution", subject_id="shared",
                      dataset_format=DATASET_SCHEMA, cases=tuple(cases),
                      baseline=ReplayTreatment("execution_plan", "1", execution_plan=left),
                      candidate=ReplayTreatment("execution_plan", "2", execution_plan=right),
                      runtime={"type": "observed_http"}, limits={"max_interactions": 1})


def factory():
    sent = []

    def post(endpoint, **kwargs):
        sent.append(json.loads(kwargs["content"]))
        return httpx.Response(200, request=httpx.Request("POST", endpoint), json={
            "choices": [{"message": {"content": "A customer response"}}],
            "usage": {"total_tokens": 12, "prompt_tokens": 9, "completion_tokens": 3},
        })

    return ObservedHttpFactory({"endpoint": "https://model.invalid/chat"}, post=post), sent


def test_two_customer_instances_share_template_but_freeze_different_case_inputs():
    left, right = plans()
    one, two = recorded_case(), recorded_case(2, customer="Bob", task="Decline the first invitation")
    frozen = copy.deepcopy((left, right, one, two))
    pair_one = case_execution_plan_pair(left, right, one)
    pair_two = case_execution_plan_pair(left, right, two)
    for pair, case in ((pair_one, one), (pair_two, two)):
        assert pair[0]["environment"] == pair[1]["environment"]
        assert pair[0]["environment"]["request"] == left["environment"]["request"]
        frozen_variables = case["provenance"]["execution_inputs"]["template_variables"]
        assert pair[0]["environment"]["template_variables"] == frozen_variables
        assert pair[0]["components"][0]["content"] == left["components"][0]["content"]
    assert execution_plan_hash(pair_one[0]) != execution_plan_hash(pair_two[0])
    assert frozen == (left, right, one, two)


def test_native_runner_dispatches_case_variables_and_gives_judge_only_public_history():
    cases = [recorded_case(), recorded_case(2, customer="Bob", task="Keep the original task list")]
    value = shared_spec(cases)
    transport, sent = factory()
    seen = []

    def inspect_judge(**kwargs):
        seen.append(copy.deepcopy(kwargs))
        assert kwargs["messages"][0] == {"role": "user", "content": "Public prior customer utterance"}
        assert len(kwargs["interactions"]) == 1
        assert "PRIVATE ORIGINAL ANSWER" not in json.dumps(kwargs)
        assert "PRIVATE GRADE" not in json.dumps(kwargs)
        assert "Act as" not in json.dumps(kwargs)
        return judge(**kwargs)

    result = evaluate_spec(value, host=FixedHost(transport, {}), judge=inspect_judge)
    assert len(sent) == len(seen) == 4
    assert {body["messages"][0]["content"].split(".")[0] for body in sent} == {"Act as Alice", "Act as Bob"}
    for body in sent:
        assert body["messages"][1] == {"role": "user", "content": "Public prior customer utterance"}
        assert body["messages"][2]["role"] == "user"
        assert body["messages"][2]["content"].startswith("Current salesperson message")
        assert {k: v for k, v in body.items() if k != "messages"} == plans()[0]["environment"]["request"]
    for case in result["cases"]:
        for branch in ("baseline", "candidate"):
            actual = case[branch]
            assert actual["ok"] and actual["execution_verification"] == "verified"
            assert actual["interaction_turns"] == actual["api_calls"] == 1
            assert actual["tool_call_count"] == 0
    old_one, old_two = (case["baseline"] for case in result["cases"])
    assert old_one["execution_plan_hash"] != old_two["execution_plan_hash"]
    assert old_one["execution_receipts"][0]["plan_hash"] == old_one["execution_plan_hash"]
    assert old_two["execution_receipts"][0]["plan_hash"] == old_two["execution_plan_hash"]
    assert old_one["observed_requests"][0]["request_hash"] != old_two["observed_requests"][0]["request_hash"]


def test_standalone_engine_uses_the_same_effective_plans(monkeypatch):
    case = recorded_case(2, customer="Bob")
    value = shared_spec([case])
    transport, sent = factory()
    execute = engine.execute_pair
    monkeypatch.setattr(engine, "execute_pair", lambda *args, **kwargs: execute(*args, **kwargs, judge=judge))
    job = skill_job_from_spec(value)
    selected = engine.annotate_cases(job, [])[0]
    result = engine._evaluate_case(
        "standalone", job, selected, case["provenance"]["session_snapshot"], transport, 30, 1,
    )
    assert all(actual["execution_verification"] == "verified" for actual in result["cases"][0].values())
    assert len(sent) == 2 and all("Act as Bob" in body["messages"][0]["content"] for body in sent)


@pytest.mark.parametrize("mutation", ["missing", "extra_variable", "missing_variable", "template_id", "revision",
                                      "template_hash", "privileged_message", "message_fields", "model_override",
                                      "source_id", "source_revision", "source_value", "source_history", "frozen_field"])
def test_per_case_overrides_cannot_change_contract_source_or_non_target_inputs(mutation):
    left, right = plans()
    case = recorded_case()
    inputs = case["provenance"]["execution_inputs"]
    if mutation == "missing":
        del case["provenance"]["execution_inputs"]
    elif mutation == "extra_variable":
        inputs["template_variables"]["model"] = "another-model"
    elif mutation == "missing_variable":
        del inputs["template_variables"]["aiTask"]
    elif mutation in {"template_id", "revision", "template_hash"}:
        key = {"template_hash": "content_hash"}.get(mutation, mutation)
        inputs["template_refs"]["main"][key] = "0" * 64 if key == "content_hash" else "different"
    elif mutation == "privileged_message":
        inputs["input_messages"][0]["role"] = "system"
    elif mutation == "message_fields":
        inputs["input_messages"][0]["tool_calls"] = []
    elif mutation == "model_override":
        inputs["request"] = {"model": "different"}
    elif mutation == "source_id":
        inputs["source_ref"]["session_id"] = "different"
    elif mutation == "source_revision":
        inputs["source_ref"]["revision"] = "different"
    elif mutation == "source_value":
        inputs["template_variables"]["userProfile"] = "Unrecorded customer"
    elif mutation == "source_history":
        inputs["input_messages"][0]["content"] = "Unrecorded history"
    else:
        for plan in (left, right):
            plan["environment"]["case_input_messages"] = [{"role": "user", "content": "Unbound"}]
    with pytest.raises(ExecutionPlanError):
        case_execution_plan_pair(left, right, case)


def test_case_inputs_cannot_override_environment_on_only_one_branch():
    left, right = plans()
    right["environment"]["request"]["temperature"] = 1
    with pytest.raises(ExecutionPlanError, match="must remain fixed"):
        case_execution_plan_pair(left, right, recorded_case())


def test_explicit_null_contract_cannot_bypass_shared_input_validation():
    left, right = plans()
    for plan in (left, right):
        plan["environment"]["template_input_contract"] = None
    with pytest.raises(ExecutionPlanError, match="template_input_contract must be an object"):
        case_execution_plan_pair(left, right, recorded_case())


def test_minimal_source_snapshot_is_sufficient_without_original_answers_or_grades():
    case = recorded_case()
    source = case["provenance"]["session_snapshot"]
    turn = source["turns"][0]
    raw = turn["request_context"]["trace_input"]["template_inputs"]
    source.pop("runtime")
    source["turns"] = [{"source_trace_id": turn["source_trace_id"], "prompt_text": turn["prompt_text"],
                        "request_context": {"template_inputs": raw}}]
    left, right = case_execution_plan_pair(*plans(), case)
    assert left["environment"] == right["environment"]
    assert left["environment"]["template_variables"]["userProfile"] == "Alice"


def test_legacy_fixed_input_plans_remain_unchanged_and_reject_unopted_case_inputs():
    left, right = plans()
    for plan in (left, right):
        plan["environment"].pop("template_input_contract")
    original = copy.deepcopy((left, right))
    old = recorded_case()
    old["provenance"].pop("execution_inputs")
    actual = case_execution_plan_pair(left, right, old)
    assert actual[0]["environment"] == left["environment"] and actual[1]["environment"] == right["environment"]
    assert (left, right) == original
    with pytest.raises(ExecutionPlanError, match="explicit template input contract"):
        case_execution_plan_pair(left, right, recorded_case())
    assert case_execution_plan_pair(None, None, {"provenance": {}}) == (None, None)


def test_execution_input_structure_normalizes_revision_and_preserves_whitespace():
    value = recorded_case()["provenance"]["execution_inputs"]
    value["template_refs"]["main"]["revision"] = 1
    value["input_messages"][0]["content"] = "  Exact public history\n\n"
    result = normalize_execution_inputs(value)
    assert result["template_refs"]["main"]["revision"] == "1"
    assert result["input_messages"][0]["content"] == value["input_messages"][0]["content"]
    assert value["template_refs"]["main"]["revision"] == 1


@pytest.mark.parametrize("mutation", ["target_missing", "duplicate_target", "context_only", "conflict", "supplied_ref"])
def test_source_input_identity_is_materialized_from_one_actual_target(mutation):
    case = recorded_case()
    source = case["provenance"]["session_snapshot"]
    turn = source["turns"][0]
    if mutation == "target_missing":
        source["provenance"]["target_trace_refs"] = []
    elif mutation == "duplicate_target":
        source["provenance"]["target_trace_refs"] *= 2
    elif mutation == "context_only":
        turn["context_only"] = True
    elif mutation == "conflict":
        turn["request_context"]["template_inputs"] = {}
    else:
        turn["request_context"]["trace_input"]["template_inputs"]["source_ref"] = {}
    with pytest.raises(ExecutionPlanError):
        source_execution_inputs(source, turn)


def test_dataset_schema_rejects_malformed_privileged_input_messages():
    case = recorded_case()
    case["provenance"]["execution_inputs"]["input_messages"][0]["role"] = "developer"
    with pytest.raises(DatasetSchemaError, match="public user/assistant"):
        normalize_case(case)


def test_input_context_is_not_a_second_interaction_in_multi_turn_replay():
    case = recorded_case()
    value = replace(shared_spec([case]), limits={"max_interactions": 2})
    transport, sent = factory()
    seen = []

    def second_turn(**kwargs):
        seen.append(copy.deepcopy(kwargs))
        report = judge(**kwargs)
        report["all_satisfied"] = len(kwargs["interactions"]) == 2
        for item in report["items"]:
            item["satisfied"] = report["all_satisfied"]
        return report

    result = evaluate_spec(value, host=FixedHost(transport, {}), judge=second_turn,
                           feedback=lambda **kwargs: "A real progressive-disclosure follow-up")
    assert len(sent) == len(seen) == 4
    for observed in result["cases"][0].values():
        assert observed["interaction_turns"] == observed["api_calls"] == 2
        assert observed["messages"].count({"role": "user", "content": "Public prior customer utterance"}) == 1
        assert observed["interactions"][0]["prompt"] == case["query"]
