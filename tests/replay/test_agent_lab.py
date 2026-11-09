"""Exact remote transport evidence, isolation, and pure Prompt Replay contracts."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json

import httpx
import pytest

from team_replay.adapters.examples.agent_lab import AgentLabFactory
from team_replay.contracts import ReplayContractError, skill_job_from_spec, skill_replay_spec
from team_replay.engine import make_context
from team_replay.execution_plan import execution_plan_hash
from team_replay.hooks import ReplayUnsupported
from team_replay.request_capture import (
    comparable_requests,
    make_execution_receipt,
    materialize_messages,
    verify_execution_receipt,
)
from team_skills.execution.contracts import plan_for, validate_profile
from tests.support.execution import profile_raw

SECRET = "test-only-transport-control-secret"


def context():
    raw = profile_raw()
    raw.update(runtime_type="agentscope-java", agent_id="agent-one", affected_skill_ids=[])
    raw["environment"]["message_prefixes"] = {"user": "HWP：", "assistant": "C："}
    profile = validate_profile(raw)
    plan = plan_for(profile, profile["components"], targets=["main-prompt"])
    return make_context("candidate", None, {}, {"runtime": {"type": "agentscope-java"}}, 30,
                        execution_plan=plan, snapshot={})


def envelope(ctx, *, nonce="fresh", changes=None, body_change=None):
    body = copy.deepcopy(ctx.execution_plan["environment"]["request"])
    body["messages"] = materialize_messages(ctx, "Question", [{"role": "assistant", "content": "Earlier answer"}])
    if body_change:
        body_change(body)
    raw = json.dumps(body, ensure_ascii=False).encode()
    claims = {"schema_version": "teamevolver.remote-dispatch.v1", "request_id": ctx.request_id,
              "run_id": ctx.request_id, "branch": "candidate", "plan_hash": execution_plan_hash(ctx.execution_plan),
              "turn_num": 1, "session_id": "isolated-session", "nonce": nonce, "runtime_fingerprint": "build-hash",
              "dispatches": [{"dispatch_id": "actual-model-dispatch",
                              "raw_request_base64": base64.b64encode(raw).decode(),
                              "request_sha256": hashlib.sha256(raw).hexdigest(), "response_status": 200,
                              "destination": {"scheme": "https", "host": "provider.invalid", "port": 443,
                                              "path": "/v1/chat/completions"},
                              "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}]}
    claims.update(changes or {})
    raw = json.dumps(claims, ensure_ascii=False).encode()
    return {"payload_b64": base64.b64encode(raw).decode(),
            "signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()}


def accept(ctx, value, **kwargs):
    return ctx.request_observer.accept_attested(value, secret=SECRET, context=ctx, turn_num=1,
                                               session_id="isolated-session", nonce="fresh", **kwargs)


def test_final_model_bytes_are_authenticated_and_business_speaker_format_is_preserved():
    ctx = context()
    ids = accept(ctx, envelope(ctx))
    receipt = make_execution_receipt(ctx, 1, ids)
    observed = verify_execution_receipt(ctx, receipt, turn_num=1, user_message="Question", after=0)
    assert observed[0]["request"]["messages"][-1] == {"role": "user", "content": "HWP：Question"}
    assert observed[0]["request"]["messages"][-2] == {"role": "assistant", "content": "C：Earlier answer"}
    changed_build = copy.deepcopy(observed)
    changed_build[0]["runtime_fingerprint"] = "another-runtime-build"
    assert comparable_requests(observed, ctx.execution_plan) != comparable_requests(changed_build, ctx.execution_plan)
    forged_receipt = copy.deepcopy(receipt)
    forged_receipt["requests"][0]["runtime_fingerprint"] = "another-runtime-build"
    with pytest.raises(ReplayUnsupported, match="transport provenance"):
        verify_execution_receipt(ctx, forged_receipt, turn_num=1, user_message="Question", after=0)
    with pytest.raises(ReplayUnsupported, match="already consumed"):
        accept(ctx, envelope(ctx))


@pytest.mark.parametrize("changes", [{"branch": "baseline"}, {"plan_hash": "other"},
                                     {"session_id": "other"}, {"nonce": "stale"}, {"turn_num": 2}])
def test_signed_but_wrong_branch_turn_or_nonce_is_rejected(changes):
    ctx = context()
    with pytest.raises(ReplayUnsupported):
        accept(ctx, envelope(ctx, changes=changes))
    assert not ctx.request_observer.records()


def test_unsigned_telemetry_and_rpc_wrappers_cannot_prove_prompt_loading():
    ctx = context()
    value = envelope(ctx)
    value["signature"] = "0" * 64
    with pytest.raises(ReplayUnsupported):
        accept(ctx, value)
    forged = envelope(ctx, body_change=lambda body: body.update(messages=[{"role": "user", "content": "RPC"}]))
    ids = accept(ctx, forged)
    with pytest.raises(ReplayUnsupported, match="complete model messages"):
        verify_execution_receipt(ctx, make_execution_receipt(ctx, 1, ids), turn_num=1, user_message="Question", after=0)


def test_pure_prompt_spec_round_trips_without_fake_skill_or_bundle():
    ctx = context()
    raw = {"job_id": "candidate", "candidate_kind": "execution", "execution_profile_id": "instructor",
           "current_skill": None, "candidate_skill": None, "baseline_execution_plan": ctx.execution_plan,
           "candidate_execution_plan": ctx.execution_plan,
           "replay_cases": [{"case_id": "case", "skill_ids": [], "query": "Question",
                             "checks": [{"id": "R1", "kind": "output", "text": "Answer"}]}]}
    spec = skill_replay_spec(raw)
    assert spec.subject_kind == "execution" and not spec.baseline.payload and not spec.candidate.payload
    job = skill_job_from_spec(spec)
    assert job["skill_name"] is None and job["skill_ids"] == [] and job["candidate_skill"] is None
    with pytest.raises(ReplayContractError):
        skill_replay_spec({**raw, "candidate_skill": {"name": "fake"}})


@pytest.mark.parametrize("tamper", [None, "output", "runtime"])
def test_adapter_http_protocol_freezes_plan_binds_output_and_closes_isolated_session(tamper):
    ctx = context()
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + SECRET
        if request.url.path.endswith("/replay-sessions"):
            frozen = json.loads(request.content)
            assert frozen["execution_plan"] == ctx.execution_plan
            assert frozen["frozen_messages"] == []
            return httpx.Response(201, json={"id": "isolated-session", "plan_hash": frozen["plan_hash"],
                                             "runtime_fingerprint": "build-hash"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"closed": True})
        incoming = json.loads(request.content)
        claims = {"output_sha256": hashlib.sha256(b"Customer answer").hexdigest()}
        if tamper == "runtime":
            claims["runtime_fingerprint"] = "changed-build"
        receipt = envelope(ctx, nonce=incoming["nonce"], changes=claims)
        return httpx.Response(200, json={"text": "tampered" if tamper == "output" else "Customer answer",
                                         "execution_receipt": receipt})

    factory = AgentLabFactory({"endpoint": "http://agent.invalid", "control_secret": SECRET},
                              client_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handle),
                                                                           **kwargs))
    session = factory.open(ctx)
    try:
        if tamper:
            with pytest.raises(ReplayUnsupported):
                session.send("Question")
            assert not ctx.request_observer.records()
        else:
            result = session.send("Question")
            assert result.response == "Customer answer"
            assert result.metrics["total_tokens"] == 10
            observed = verify_execution_receipt(ctx, result.execution_receipt, turn_num=1,
                                                user_message="Question", after=0)
            assert observed[0]["observation"] == "remote_attested_dispatch"
    finally:
        session.close()
    assert requests[-1].method == "DELETE"


def cleanup_fixture(handler):
    ctx, clients, options, requests = context(), [], [], []

    def handle(request):
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/replay-sessions"):
            return httpx.Response(201, json={"id": "isolated-session",
                                             "plan_hash": execution_plan_hash(ctx.execution_plan),
                                             "runtime_fingerprint": "build-hash"})
        return handler(request)

    def client_factory(**kwargs):
        options.append(kwargs)
        client = httpx.Client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    session = AgentLabFactory({"endpoint": "http://agent.invalid", "control_secret": SECRET},
                              client_factory=client_factory).open(ctx)
    return session, clients, options, requests


@pytest.mark.parametrize("first_delete_applied", [False, True])
def test_cleanup_transport_retry_uses_fresh_client_and_handles_lost_ack(first_delete_applied):
    calls, deleted = 0, False

    def handle(request):
        nonlocal calls, deleted
        assert request.method == "DELETE"
        assert request.headers["Connection"] == "close"
        assert request.headers["Authorization"] == "Bearer " + SECRET
        calls += 1
        if calls == 1:
            deleted = first_delete_applied
            raise httpx.RemoteProtocolError("No cleanup acknowledgement", request=request)
        if deleted:
            return httpx.Response(404, json={"error": "隔离 Replay 不存在", "code": "INTERNAL_ERROR"})
        deleted = True
        return httpx.Response(200, json={"closed": True})

    session, clients, options, requests = cleanup_fixture(handle)
    session.close()
    assert deleted and session.closed and not session.cleanup_error
    assert calls == len(clients) == 2 and all(client.is_closed for client in clients)
    assert all(option["limits"].max_keepalive_connections == 0 for option in options)
    assert options[1]["timeout"] == 10.0
    session.close()
    assert calls == 2  # Repeated confirmed close must issue no further request.
    assert [request.method for request in requests] == ["POST", "DELETE", "DELETE"]


def test_two_cleanup_transport_failures_are_bounded_and_remain_unconfirmed():
    def handle(request):
        raise httpx.RemoteProtocolError("Still no acknowledgement", request=request)

    session, clients, _, requests = cleanup_fixture(handle)
    with pytest.raises(httpx.RemoteProtocolError):
        session.close()
    assert not session.closed and session.cleanup_error == "RemoteProtocolError"
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert [request.method for request in requests] == ["POST", "DELETE", "DELETE"]
    with pytest.raises(ReplayUnsupported, match="previously failed"):
        session.close()
    with pytest.raises(ReplayUnsupported, match="closed"):
        session.send("Do not generate after failed cleanup")
    assert len(requests) == 3


@pytest.mark.parametrize("body", [{"error": "执行控制接口不存在"}, {"error": "Not Found"}, {}])
def test_unrecognized_404_does_not_confirm_cleanup_or_trigger_transport_retry(body):
    session, clients, _, requests = cleanup_fixture(lambda request: httpx.Response(404, json=body))
    with pytest.raises(httpx.HTTPStatusError):
        session.close()
    assert not session.closed and session.cleanup_error == "HTTPStatusError"
    assert len(clients) == 1 and clients[0].is_closed
    assert [request.method for request in requests] == ["POST", "DELETE"]


@pytest.mark.parametrize("method,path", [("GET", "/api/agents/agent-one/replay-sessions/isolated-session"),
                                       ("DELETE", "/api/agents/agent-two/replay-sessions/isolated-session")])
def test_missing_session_ack_must_match_the_exact_delete_path(method, path):
    session, _, _, _ = cleanup_fixture(lambda request: httpx.Response(200, json={"closed": True}))
    response = httpx.Response(404, json={"error": "隔离 Replay 不存在"},
                              request=httpx.Request(method, "http://agent.invalid" + path))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            session._confirm_cleanup(response)
        assert not session.closed
    finally:
        session.close()


@pytest.mark.parametrize("body", [{}, {"closed": False}, {"closed": 1}])
def test_success_status_without_explicit_closed_true_is_not_a_cleanup_ack(body):
    session, clients, _, _ = cleanup_fixture(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ReplayUnsupported, match="acknowledge"):
        session.close()
    assert not session.closed and session.cleanup_error == "ReplayUnsupported"
    assert len(clients) == 1 and clients[0].is_closed


def test_model_message_transport_failure_is_never_retried():
    messages = 0

    def handle(request):
        nonlocal messages
        if request.method == "DELETE":
            return httpx.Response(200, json={"closed": True})
        messages += 1
        raise httpx.RemoteProtocolError("Model response connection interrupted", request=request)

    session, clients, _, requests = cleanup_fixture(handle)
    with pytest.raises(httpx.RemoteProtocolError):
        session.send("One real request only")
    assert messages == 1 and len(clients) == 1
    assert not session.context.request_observer.records()
    session.close()
    assert session.closed and [request.method for request in requests] == ["POST", "POST", "DELETE"]
