"""Inline Context selection, exact request evidence, and legacy transport projection."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest

from team_replay.adapters.examples.agent_lab import AgentLabFactory
from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.contracts import ReplaySpec, ReplayTreatment
from team_replay.datasets.schema import DATASET_SCHEMA
from team_replay.engine import make_context
from team_replay.execution_inputs import case_execution_plan_pair
from team_replay.execution_plan import (
    ExecutionPlanError,
    execution_plan_hash,
    normalize_execution_plan,
    render_components,
    validate_execution_plan_pair,
)
from team_replay.hooks import ReplayUnsupported
from team_replay.integration import FixedHost
from team_replay.request_capture import comparable_requests, materialize_messages, verify_execution_receipt
from team_replay.runner import evaluate_spec
from team_replay.variable_context import (
    applicable_component_ids,
    project_inline_plan,
    source_value_hash,
)
from tests.support.replay import judge
from tests.support.variable_context import changed_inline_plan, inline_case, inline_plan


@pytest.mark.parametrize("value,expected", [("Original A", "Improved A"), ("Original B", "Original B"),
                                            ("", ""), ("Unknown original", "Unknown original")])
def test_one_asset_edit_preserves_other_values_and_empty_cases(value, expected):
    left = inline_plan()
    right = changed_inline_plan(left)
    case = inline_case(left, value)
    frozen = copy.deepcopy((left, right, case))
    before, after = case_execution_plan_pair(left, right, case)
    assert before["environment"] == after["environment"]
    assert after["environment"]["template_variables"]["rules"] == value
    assert render_components(before)[0]["content"] == f"Fixed before|{value}|Fixed after|Reader"
    assert render_components(after)[0]["content"] == f"Fixed before|{expected}|Fixed after|Reader"
    assert len(render_components(after)) == 2
    assert (left, right, case) == frozen
    expected_ids = {"main", "shared"} | ({"rule-a"} if value == "Original A" else
                                         {"rule-b"} if value == "Original B" else set())
    assert applicable_component_ids(before) == expected_ids


def test_asset_body_and_original_variable_are_literal_single_substitutions():
    plan = changed_inline_plan(inline_plan(), "Keep {{literal_example}} and {% literal %} unchanged")
    assert render_components(plan)[0]["content"] == (
        "Fixed before|Keep {{literal_example}} and {% literal %} unchanged|Fixed after|Reader")
    projected = project_inline_plan(plan)
    assert render_components(projected)[0]["content"] == render_components(plan)[0]["content"]
    assert projected["environment"]["template_variables"]["rules"] != plan["environment"]["template_variables"]["rules"]
    assert len(projected["components"]) == 2


@pytest.mark.parametrize("mutation", ["host", "name", "hash", "duplicate", "kind", "context_ref", "null"])
def test_invalid_or_ambiguous_selector_is_rejected(mutation):
    value = inline_plan()
    asset = value["components"][2]
    binding = asset["binding"]["inline_variable"]
    if mutation == "host":
        binding["component_id"] = "missing"
    elif mutation == "name":
        binding["name"] = "absent"
    elif mutation == "hash":
        binding["source_value_hash"] = "not-a-sha256"
    elif mutation == "duplicate":
        value["components"][3]["binding"] = copy.deepcopy(asset["binding"])
    elif mutation == "kind":
        asset["kind"] = "agent_prompt"
    elif mutation == "context_ref":
        asset["binding"]["context_ref"] = "context-item"
    else:
        asset["binding"]["inline_variable"] = None
    with pytest.raises(ExecutionPlanError):
        normalize_execution_plan(value)


def test_source_selector_and_original_case_inputs_cannot_be_changed_by_candidate():
    left = inline_plan()
    right = changed_inline_plan(left)
    right["components"][2]["binding"]["inline_variable"]["source_value_hash"] = source_value_hash("Another source")
    with pytest.raises(ExecutionPlanError, match="shared component"):
        validate_execution_plan_pair(left, right)
    case = inline_case(left)
    case["provenance"]["execution_inputs"]["template_variables"]["rules"] = "Original B"
    with pytest.raises(ExecutionPlanError, match="frozen source"):
        case_execution_plan_pair(left, changed_inline_plan(left), case)


def test_multihost_core_is_scoped_and_scalar_projection_refuses_to_broaden_edit():
    plan = changed_inline_plan(inline_plan())
    host = copy.deepcopy(plan["components"][0])
    host.update(component_id="other-host", order=4)
    plan["components"].append(host)
    rendered = render_components(normalize_execution_plan(plan))
    assert "Improved A" in rendered[0]["content"] and "Original A" in rendered[2]["content"]
    with pytest.raises(ValueError, match="host-specific"):
        project_inline_plan(plan)


def test_agent_lab_refuses_inexact_multihost_projection_before_opening_transport():
    plan = changed_inline_plan(inline_plan())
    plan["binding"].update(runtime_type="agentscope-java", agent_id="agent-one")
    host = copy.deepcopy(plan["components"][0])
    host.update(component_id="other-host", order=4)
    plan["components"].append(host)
    context = make_context("candidate", None, {}, {"runtime": {"type": "agentscope-java"}}, 30,
                           execution_plan=plan, snapshot={})
    opened = []
    factory = AgentLabFactory({"endpoint": "http://agent.invalid", "control_secret": "test-only"},
                              client_factory=lambda **kwargs: opened.append(kwargs))
    with pytest.raises(ReplayUnsupported, match="host-specific"):
        factory.open(context)
    assert not opened


def test_comparison_masks_only_bound_target_spans_and_preserves_rest_of_host():
    left, right = inline_plan(), changed_inline_plan(inline_plan(), "A much longer replacement rule")
    requests = []
    for plan in [left, right]:
        ctx = make_context("candidate", None, {}, {"runtime": {"type": "observed_http"}}, 30,
                           execution_plan=plan, snapshot={})
        requests.append([{"request": {"model": "test-model", "messages": materialize_messages(ctx, "Current")},
                          "destination": "same"}])
    assert comparable_requests(requests[0], left) == comparable_requests(requests[1], right)
    for phrase in ["Fixed before", "Fixed after", "Reader"]:
        tampered = copy.deepcopy(requests[1])
        tampered[0]["request"]["messages"][0]["content"] = tampered[0]["request"]["messages"][0]["content"].replace(
            phrase, "Changed non-target text")
        assert comparable_requests(requests[0], left) != comparable_requests(tampered, right)


def test_native_replay_dispatches_inline_asset_and_keeps_shared_inputs_and_metrics():
    left, right = inline_plan(), changed_inline_plan(inline_plan())
    cases = [inline_case(left), inline_case(left, "Original B", identity="case-b")]
    spec = ReplaySpec(run_id="inline", subject_kind="execution", subject_id="variable-rules",
                      dataset_format=DATASET_SCHEMA, cases=tuple(cases),
                      baseline=ReplayTreatment("execution_plan", "1", execution_plan=left),
                      candidate=ReplayTreatment("execution_plan", "2", execution_plan=right),
                      runtime={"type": "observed_http"}, limits={"max_interactions": 1})
    sent = []

    def post(endpoint, **kwargs):
        sent.append(json.loads(kwargs["content"]))
        return httpx.Response(200, request=httpx.Request("POST", endpoint), json={
            "choices": [{"message": {"content": "Answer"}}],
            "usage": {"total_tokens": 10, "prompt_tokens": 7, "completion_tokens": 3}})

    factory = ObservedHttpFactory({"endpoint": "https://model.invalid/chat"}, post=post)
    result = evaluate_spec(spec, host=FixedHost(factory, {}), judge=judge)
    assert len(sent) == 4
    for pair in result["cases"]:
        for branch in ["baseline", "candidate"]:
            observed = pair[branch]
            assert observed["execution_verification"] == "verified" and observed["ok"]
            assert observed["interaction_turns"] == observed["api_calls"] == 1
            assert len(observed["observed_requests"][0]["component_positions"]) == 2
            assert observed["messages"][0] == {"role": "user", "content": "Original public history"}


def projected_session(*, mutation=None):
    plan = changed_inline_plan(inline_plan())
    plan["binding"].update(runtime_type="agentscope-java", agent_id="agent-one")
    ctx = make_context("candidate", None, {}, {"runtime": {"type": "agentscope-java"}}, 30,
                       execution_plan=plan, snapshot={})
    secret, opened, dispatches = "test-only-secret", [], []

    def handle(request):
        if request.method == "DELETE":
            return httpx.Response(200, json={"closed": True})
        data = json.loads(request.content)
        if request.url.path.endswith("/replay-sessions"):
            opened.append(data)
            return httpx.Response(201, json={"id": "isolated", "plan_hash": data["plan_hash"],
                                           "runtime_fingerprint": "build"})
        wire = opened[0]["execution_plan"]
        wire_context = replace(ctx, execution_plan=wire)
        body = {**wire["environment"]["request"], "messages": materialize_messages(wire_context, data["text"])}
        if mutation == "body":
            body["messages"][0]["content"] += "Unplanned instruction"
        raw_request = json.dumps(body, ensure_ascii=False).encode()
        dispatches.append(raw_request)
        claims = {"schema_version": "teamevolver.remote-dispatch.v1", "request_id": ctx.request_id,
                  "run_id": ctx.request_id, "branch": "candidate", "plan_hash": opened[0]["plan_hash"],
                  "turn_num": data["turn_num"], "session_id": "isolated", "nonce": data["nonce"],
                  "runtime_fingerprint": "build", "output_sha256": hashlib.sha256(b"Answer").hexdigest(),
                  "dispatches": [{"dispatch_id": "actual-dispatch", "raw_request_base64":
                      base64.b64encode(raw_request).decode(), "request_sha256": hashlib.sha256(raw_request).hexdigest(),
                      "response_status": 200, "destination": {"scheme": "https", "host": "model.invalid",
                      "port": 443, "path": "/chat"}, "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                                                          "total_tokens": 10}}]}
        if mutation == "logical_hash":
            claims["plan_hash"] = execution_plan_hash(ctx.execution_plan)
        raw = json.dumps(claims).encode()
        envelope = {"payload_b64": base64.b64encode(raw).decode(),
                    "signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}
        if mutation == "signature":
            envelope["signature"] = "0" * 64
        return httpx.Response(200, json={"text": "Answer", "execution_receipt": envelope})

    factory = AgentLabFactory({"endpoint": "http://agent.invalid", "control_secret": secret},
                              client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
    return ctx, factory.open(ctx), opened, dispatches


def test_legacy_agent_transport_signs_wire_hash_and_logical_receipt_checks_actual_bytes():
    ctx, session, opened, dispatches = projected_session()
    try:
        observation = session.send("Question")
        wire_hash = execution_plan_hash(opened[0]["execution_plan"])
        logical_hash = execution_plan_hash(ctx.execution_plan)
        assert opened[0]["plan_hash"] == wire_hash != logical_hash
        receipt = observation.execution_receipt
        assert receipt["plan_hash"] == logical_hash
        observed = verify_execution_receipt(ctx, receipt, turn_num=1, user_message="Question", after=0)
        assert observed[0]["request_hash"] == hashlib.sha256(dispatches[0]).hexdigest()
        assert observed[0]["execution_projection"] == {"logical_plan_hash": logical_hash,
                                                        "transport_plan_hash": wire_hash}
        assert observation.metrics["total_tokens"] == 10
        forged = copy.deepcopy(receipt)
        forged["requests"][0]["execution_projection"]["transport_plan_hash"] = logical_hash
        with pytest.raises(ReplayUnsupported, match="transport provenance"):
            verify_execution_receipt(ctx, forged, turn_num=1, user_message="Question", after=0)
    finally:
        session.close()


@pytest.mark.parametrize("mutation", ["signature", "logical_hash", "body"])
def test_remote_projection_never_accepts_wrong_signature_hash_or_request(mutation):
    ctx, session, _, _ = projected_session(mutation=mutation)
    try:
        if mutation == "body":
            observation = session.send("Question")
            with pytest.raises(ReplayUnsupported, match="ignored, changed"):
                verify_execution_receipt(ctx, observation.execution_receipt, turn_num=1,
                                         user_message="Question", after=0)
        else:
            with pytest.raises(ReplayUnsupported, match="unverifiable"):
                session.send("Question")
            assert not ctx.request_observer.records()
    finally:
        session.close()


def test_arbitrary_wire_plan_cannot_be_bound_as_the_logical_projection():
    ctx, session, _, _ = projected_session()
    session.transport_plan["environment"]["request"]["model"] = "another-model"
    try:
        with pytest.raises(ReplayUnsupported, match="unverifiable"):
            session.send("Question")
        assert not ctx.request_observer.records()
    finally:
        session.close()
