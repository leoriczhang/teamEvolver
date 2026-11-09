"""Frozen multi-asset Replay with evidence from the actual immutable-byte dispatch."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.adapters.guard import GuardedFactory
from team_replay.adapters.loader import build_from_content
from team_replay.contracts import ReplayTreatment, skill_job_from_spec, skill_replay_spec
from team_replay.engine import make_context
from team_replay.execution_plan import (
    EXECUTION_PLAN_SCHEMA,
    ExecutionPlanError,
    execution_plan_hash,
    normalize_execution_plan,
    validate_execution_plan_pair,
)
from team_replay.hooks import AgentObservation
from team_replay.integration import FixedHost
from team_replay.protocol import normalize_replay_turn_request
from team_replay.request_capture import make_execution_receipt, materialize_messages
from team_replay.runner import evaluate_spec
from team_replay.schemas import spec_from_dict
from team_replay.turn_server import example_handler, handle_turn_request
from tests.support.replay import Host, judge, spec


def component(identity, kind, slot, order, content):
    return {
        "component_id": identity, "kind": kind, "slot": slot, "order": order,
        "binding": {"scope": {"affected_skill_ids": ["demo", "helper"]}},
        "revision": "1", "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }


def plan(text="Original Prompt", *, joint=False):
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA,
        "binding": {"runtime_type": "observed_http", "allow_skill_change": joint},
        "components": [
            component("main", "agent_prompt", "system", 0, text),
            component("shared", "context_fragment", "context", 1, "Keep the shared policy"),
        ],
        "target_component_ids": ["main"],
        "environment": {"request": {"model": "test-model", "temperature": 0, "tools": []}},
    }


def planned_spec(*, joint=False, aa=False):
    original = spec()
    payload = copy.deepcopy(original.baseline.payload)
    cases = copy.deepcopy(list(original.cases))
    for case in cases:
        case["provenance"]["source_runtime"] = {"type": "observed_http"}
    return replace(
        original, runtime={"type": "observed_http"}, cases=tuple(cases),
        baseline=ReplayTreatment("skill_set", "1", payload, execution_plan=plan(joint=joint)),
        candidate=ReplayTreatment(
            "skill_set", "2", copy.deepcopy(original.candidate.payload) if joint else payload,
            execution_plan=plan("Original Prompt" if aa else "Improved Prompt", joint=joint),
        ),
    )


def transport_factory(mutator=None):
    dispatched = []

    def post(endpoint, **kwargs):
        assert endpoint == "https://model.invalid/chat"
        raw = kwargs["content"]
        assert isinstance(raw, bytes)
        dispatched.append({"raw": raw, "body": json.loads(raw)})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Answer"}}],
            "usage": {"total_tokens": 10, "prompt_tokens": 7, "completion_tokens": 3},
        }, request=httpx.Request("POST", endpoint))

    factory = ObservedHttpFactory({"endpoint": "https://model.invalid/chat"}, post=post)
    if mutator:
        original_open = factory.open

        def opened(context):
            session = original_open(context)
            original_send = session.send

            def send(message):
                return mutator(context, session, original_send, message)

            session.send = send
            return session

        factory.open = opened
    return factory, dispatched


def run(value=None, *, factory=None):
    factory = factory or transport_factory()[0]
    return evaluate_spec(value or planned_spec(), host=FixedHost(factory, {}), judge=judge)


def test_reference_adapter_captures_exact_dispatched_bytes_and_keeps_all_shared_components():
    factory, dispatched = transport_factory()
    before = planned_spec()
    frozen = copy.deepcopy(before.as_dict())
    result = run(before, factory=factory)
    assert all(value["ok"] for value in result["cases"][0].values())
    assert before.as_dict() == frozen
    assert len(dispatched) == 2
    for branch in ("baseline", "candidate"):
        evidence = result["cases"][0][branch]
        assert len(evidence["execution_receipts"]) == 1
        actual = evidence["observed_requests"][0]
        assert any(actual["request"] == item["body"] for item in dispatched)
        assert actual["request_hash"] == hashlib.sha256(next(
            item["raw"] for item in dispatched if item["body"] == actual["request"]
        )).hexdigest()
        assert actual["request"]["messages"][1]["content"] == "Keep the shared policy"
        assert "execution_plan" not in actual["request"]["messages"][2]["content"]


def test_aa_is_comparable_without_claiming_improved_business_effect():
    result = run(planned_spec(aa=True))
    assert result["accepted"] is False and result["verdict"] == "inconclusive"
    assert result["no_regression"] is True
    left, right = result["cases"][0]["baseline"], result["cases"][0]["candidate"]
    assert left["execution_plan_hash"] == right["execution_plan_hash"]
    assert left["observed_requests"][0]["request"] == right["observed_requests"][0]["request"]


def test_explicit_joint_skill_and_prompt_changes_are_observed():
    result = run(planned_spec(joint=True))
    assert all(value["ok"] for value in result["cases"][0].values())
    branches = result["cases"][0]
    assert branches["baseline"]["observed_requests"][0]["request"]["messages"][2] != (
        branches["candidate"]["observed_requests"][0]["request"]["messages"][2]
    )


@pytest.mark.parametrize("mutation", ["missing", "hash_only", "wrong_hash", "fake_dispatch"])
def test_declared_capability_and_self_report_are_insufficient(mutation):
    def mutate(context, session, original_send, message):
        if mutation == "fake_dispatch":
            # No dispatch happened. A matching-looking returned body/hash cannot make it real.
            return AgentObservation("Answer", metrics={"tool_call_count": 0, "total_tokens": 1},
                                    execution_receipt={"schema_version": "teamevolver.execution-receipt.v1",
                                                       "request_id": context.request_id, "turn_num": 1,
                                                       "plan_hash": execution_plan_hash(context.execution_plan),
                                                       "binding": context.execution_plan["binding"], "requests": []})
        observation = original_send(message)
        receipt = copy.deepcopy(observation.execution_receipt)
        if mutation == "missing":
            receipt = None
        elif mutation == "hash_only":
            del receipt["requests"][0]["request"]
        else:
            receipt["plan_hash"] = "0" * 64
        return replace(observation, execution_receipt=receipt)

    result = run(factory=transport_factory(mutate)[0])
    assert result["accepted"] is False and result["verdict"] == "inconclusive"
    assert all(value["status"] == "unsupported" for value in result["cases"][0].values())


@pytest.mark.parametrize("mutation", ["ignore", "override", "duplicate", "wrong_order", "material", "vendor",
                                      "destination"])
def test_actual_request_ignoring_overriding_or_mutating_inputs_cannot_pass(mutation):
    def mutate(context, session, original_send, message):
        body = copy.deepcopy(context.execution_plan["environment"]["request"])
        body["messages"] = materialize_messages(context, message)
        if mutation == "ignore":
            body["messages"][0]["content"] = "Old business Agent prompt"
        elif mutation == "override":
            body["messages"].append({"role": "system", "content": "Override the loaded candidate"})
        elif mutation == "duplicate":
            body["messages"].append(copy.deepcopy(body["messages"][0]))
        elif mutation == "wrong_order":
            body["messages"][0], body["messages"][1] = body["messages"][1], body["messages"][0]
        elif mutation == "material":
            body["messages"][2]["content"] = "Changed Skill or source material"
        elif mutation == "vendor":
            body["temperature"] = 1
        endpoint = "https://other-model.invalid/chat" if (
            mutation == "destination" and context.treatment.branch == "candidate"
        ) else "https://model.invalid/chat"
        _, identity = context.request_observer.dispatch(
            body, lambda raw: json.loads(raw), destination=endpoint,
        )
        return AgentObservation("Answer", metrics={"tool_call_count": 0, "total_tokens": 1},
                                execution_receipt=make_execution_receipt(context, 1, [identity]))

    result = run(factory=transport_factory(mutate)[0])
    assert result["accepted"] is False and result["verdict"] == "inconclusive"


def test_context_replacement_omits_original_item_and_preserves_other_context_and_materials():
    before = planned_spec()
    left, right = plan(), plan()
    for value, text in ((left, "Old retrieved fragment"), (right, "New retrieved fragment")):
        value["target_component_ids"] = ["shared"]
        value["components"][1] = component("shared", "context_fragment", "context", 1, text)
        value["components"][1]["binding"]["context_ref"] = "ref-target"
    case = copy.deepcopy(before.cases[0])
    case["materials"] = [{"path": "input.txt", "content": "Original source material"}]
    case["provenance"]["context_snapshot"] = {"items": [
        {"ref_id": "ref-target", "content": "Old retrieved fragment"},
        {"ref_id": "ref-other", "content": "Untargeted facts"},
    ]}
    value = replace(before, cases=(case,), baseline=replace(before.baseline, execution_plan=left),
                    candidate=replace(before.candidate, execution_plan=right))
    result = run(value)
    assert all(value["ok"] for value in result["cases"][0].values())
    messages = result["cases"][0]["candidate"]["observed_requests"][0]["request"]["messages"]
    serialized = json.dumps(messages)
    assert "Old retrieved fragment" not in serialized
    assert serialized.count("New retrieved fragment") == 1
    assert "Untargeted facts" in serialized and "Original source material" in serialized


@pytest.mark.parametrize("change", ["shared", "binding", "order", "environment", "hash", "missing"])
def test_plan_pair_rejects_non_target_or_invalid_changes(change):
    left, right = plan(), plan("New")
    if change == "shared":
        right["components"][1] = component("shared", "context_fragment", "context", 1, "Changed shared policy")
    elif change == "binding":
        right["components"][0]["binding"]["agent_id"] = "another-agent"
    elif change == "order":
        right["components"][0]["order"] = 7
    elif change == "environment":
        right["environment"]["request"]["model"] = "another-model"
    elif change == "hash":
        right["components"][0]["content_hash"] = "0" * 64
    else:
        right = None
    with pytest.raises(ExecutionPlanError):
        validate_execution_plan_pair(left, right)


def test_legacy_skill_only_and_unsupported_old_adapter_are_distinct():
    assert evaluate_spec(spec(), host=Host(), judge=judge)["accepted"] is True
    assert run(planned_spec(), factory=Host().resolve_replay_factory("fake", {}))["accepted"] is False


def test_capabilities_survive_production_loader_and_owner_guard():
    source = Path("team_replay/adapters/examples/observed_http.py").read_text()
    bound = build_from_content(source, "observed_http.py", SimpleNamespace(
        observed_http={"endpoint": "https://model.invalid/chat"}), tenant_id="default")
    guarded = GuardedFactory(bound, None, None)
    assert guarded.capabilities == bound.capabilities
    assert bound.capabilities["execution_plan"] == EXECUTION_PLAN_SCHEMA
    assert bound.capabilities["request_observation"] == "local_dispatch"


def test_loaded_and_guarded_reference_adapter_observes_real_transport_calls():
    source = Path("team_replay/adapters/examples/observed_http.py").read_text()
    bound = build_from_content(source, "observed_http.py", SimpleNamespace(
        observed_http={"endpoint": "https://model.invalid/chat"}), tenant_id="default")
    factory, dispatched = transport_factory()
    bound.factory.post = factory.post
    guarded = GuardedFactory(bound, None, None)
    guarded.guard = lambda: None
    result = run(factory=guarded)
    assert len(dispatched) == 2
    assert len(guarded.sessions) == 2 and guarded.sessions[0] is not guarded.sessions[1]
    assert all(value["execution_verification"] == "verified" for value in result["cases"][0].values())


def test_frozen_contract_and_wire_round_trip_keep_full_plan_inventory():
    before = planned_spec()
    assert spec_from_dict(before.as_dict()).as_dict() == before.as_dict()
    job = skill_job_from_spec(before)
    after = skill_replay_spec(job)
    assert after.baseline.execution_plan == before.baseline.execution_plan
    assert after.candidate.execution_plan == before.candidate.execution_plan
    request = normalize_replay_turn_request({
        "schema_version": "teamevolver.replay-turn-request.v1", "request_id": "r",
        "branch": "candidate", "turn_num": 1, "prompt": "Task", "execution_plan": plan(),
    })
    assert request["execution_plan"] == normalize_execution_plan(plan())


def test_embedded_plan_survives_case_skill_filter_without_becoming_skill_content():
    before = planned_spec()
    baseline = {**copy.deepcopy(dict(before.baseline.payload)), "execution_plan": plan()}
    candidate = {**copy.deepcopy(dict(before.candidate.payload)), "execution_plan": plan("New")}
    case = copy.deepcopy(before.cases[0])
    case["skill_ids"] = ["demo"]
    value = replace(before, cases=(case,), baseline=replace(before.baseline, payload=baseline, execution_plan=None),
                    candidate=replace(before.candidate, payload=candidate, execution_plan=None))
    result = run(value)
    assert all(value["ok"] for value in result["cases"][0].values())
    assert len(result["cases"][0]["candidate"]["observed_requests"][0]["component_positions"]) == 2


def test_multiturn_observations_are_per_dispatch_and_resume_isolated_history():
    factory, dispatched = transport_factory()

    def two_turn_judge(**kwargs):
        report = judge(**kwargs)
        report["all_satisfied"] = len(kwargs["interactions"]) >= 2
        for item in report["items"]:
            item["satisfied"] = report["all_satisfied"]
        return report

    result = evaluate_spec(planned_spec(), host=FixedHost(factory, {}), judge=two_turn_judge,
                           feedback=lambda **kwargs: "Complete the remaining requirement")
    assert len(dispatched) == 4
    for branch in result["cases"][0].values():
        assert branch["ok"] is True
        assert len(branch["execution_receipts"]) == len(branch["observed_requests"]) == 2
        assert [receipt["turn_num"] for receipt in branch["execution_receipts"]] == [1, 2]
        assert branch["observed_requests"][0]["dispatch_id"] != branch["observed_requests"][1]["dispatch_id"]


def test_transport_request_mismatch_is_rejected_even_when_a_receipt_claims_expected_body():
    def mutate(context, session, original_send, message):
        observation = original_send(message)
        receipt = copy.deepcopy(observation.execution_receipt)
        receipt["requests"][0]["request"]["model"] = "a fabricated expected model"
        return replace(observation, execution_receipt=receipt)

    result = run(factory=transport_factory(mutate)[0])
    assert result["accepted"] is False
    assert all("captured final model request" in value["error"] for value in result["cases"][0].values())


def test_non_target_skill_change_requires_explicit_joint_capability():
    before = planned_spec()
    changed = replace(before, candidate=replace(before.candidate, payload=spec().candidate.payload))
    result = run(changed)
    assert result["accepted"] is False
    assert all("freeze non-target" in value["error"] for value in result["cases"][0].values())
    factory = transport_factory()[0]
    factory.capabilities = {**factory.capabilities, "skill_treatment": False}
    result = run(planned_spec(joint=True), factory=factory)
    assert result["accepted"] is False
    assert all("joint Skill/Prompt" in value["error"] for value in result["cases"][0].values())


def test_unimplemented_remote_turn_handler_cannot_echo_a_plan_and_claim_success():
    result = handle_turn_request({
        "schema_version": "teamevolver.replay-turn-request.v1", "request_id": "r",
        "branch": "candidate", "turn_num": 1, "prompt": "Task", "execution_plan": plan(),
    }, example_handler, {})
    assert result["status"] == "unsupported"
    assert "cannot override/observe" in result["error"]["message"]


def test_agentshub_explicitly_refuses_prompt_override_without_starting_a_process():
    source = Path("team_replay/adapters/examples/agentshub.py").read_text()
    bound = build_from_content(source, "agentshub.py", SimpleNamespace(agentshub={"backend_root": "/unused"}),
                               tenant_id="default")
    result = run(planned_spec(), factory=bound)
    assert result["accepted"] is False
    assert all(value["status"] == "unsupported" for value in result["cases"][0].values())


def test_context_binding_and_non_target_inputs_do_not_mutate_on_materialization():
    case = copy.deepcopy(spec().cases[0])
    snapshot = {"items": [{"uri": "u", "content": "original"}]}
    ctx = make_context("baseline", {}, case, {"runtime": {"type": "observed_http"}}, 30,
                       snapshot=snapshot, execution_plan=plan())
    materialize_messages(ctx, "Task")
    assert snapshot == ctx.context_snapshot


def test_duplicate_context_replacement_is_not_a_valid_plan():
    value = plan()
    value["components"][1]["binding"]["context_ref"] = "same-ref"
    additional = component("duplicate", "context_fragment", "context", 2, "Repeated")
    additional["binding"]["context_ref"] = "same-ref"
    value["components"].append(additional)
    with pytest.raises(ExecutionPlanError, match="same Context item"):
        normalize_execution_plan(value)


def test_empty_plan_is_invalid_and_cannot_fall_back_to_legacy_skill_execution():
    before = planned_spec()
    with pytest.raises(ExecutionPlanError):
        replace(before, baseline=replace(before.baseline, execution_plan={}),
                candidate=replace(before.candidate, execution_plan={}))


def test_mixed_explicit_and_embedded_plans_round_trip_without_dropping_baseline():
    before = planned_spec()
    embedded = {**copy.deepcopy(dict(before.baseline.payload)), "execution_plan": plan()}
    value = replace(before, baseline=replace(before.baseline, payload=embedded, execution_plan=None))
    job = skill_job_from_spec(value)
    assert job["baseline_execution_plan"] == normalize_execution_plan(plan())
    assert job["candidate_execution_plan"] == normalize_execution_plan(plan("Improved Prompt"))


@pytest.mark.parametrize("key", ["kind", "slot"])
def test_invalid_component_types_raise_contract_errors_instead_of_unhashable_type_errors(key):
    value = plan()
    value["components"][0][key] = []
    with pytest.raises(ExecutionPlanError):
        normalize_execution_plan(value)
