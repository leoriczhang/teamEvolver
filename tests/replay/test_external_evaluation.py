"""External scores use real frozen Replay output without changing the default policy."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import httpx
import pytest

from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.contracts import (
    ReplayContractError,
    ReplaySpec,
    ReplayTreatment,
    skill_job_from_spec,
    skill_replay_spec,
)
from team_replay.datasets.schema import DATASET_SCHEMA
from team_replay.engine import evaluate_job
from team_replay.evaluation import (
    EXTERNAL_AGENT_SCORE_POLICY,
    evaluation_policy_hash,
    external_score_decision,
    normalize_evaluation,
    normalize_external_report,
    valid_external_result,
)
from team_replay.external_agent import MAX_RESPONSE_BYTES, evaluate_external_http
from team_replay.gateway import EmbeddedReplayGateway
from team_replay.hooks import ReplayUnsupported
from team_replay.runner import aggregate_results, evaluate_spec
from team_replay.schemas import spec_from_dict
from tests.support.replay import Host, judge, spec
from tests.support.variable_context import changed_inline_plan, inline_case, inline_plan


def evaluation(**settings):
    return normalize_evaluation({"mode": "external_agent", "external_agent": {
        "url": "https://evaluator.example/score", "rubric": "Follow the original business requirements", **settings,
    }})


def external_spec(**settings):
    return replace(spec(), evaluation=evaluation(**settings), policy_id=EXTERNAL_AGENT_SCORE_POLICY)


class ScoringHost(Host):
    def __init__(self, *, scores=(60, 80), metrics=True, fail=False):
        super().__init__(metrics=metrics)
        self.scores = scores
        self.requests = []
        self.fail = fail

    def evaluate_external(self, request):
        self.requests.append(copy.deepcopy(request))
        if self.fail:
            raise RuntimeError("secret-provider-error-do-not-persist")
        return {"baseline": {"score": self.scores[0], "reason": "Original outcome"},
                "candidate": {"score": self.scores[1], "reason": "Improved outcome"}}


class RecordingHost(ScoringHost):
    def __init__(self, *, failure=None, include_tools=False):
        super().__init__()
        self.sent = []
        self.failure = failure
        self.include_tools = include_tools

    def resolve_replay_factory(self, runtime_type, source_session):
        factory = super().resolve_replay_factory(runtime_type, source_session)
        original_open = factory.open

        def opened(context):
            session = original_open(context)
            send, close = session.send, session.close

            def tracked_send(message):
                self.sent.append((context.treatment.branch, message))
                if self.failure == "send" and context.treatment.branch == "candidate":
                    raise RuntimeError("real business transport failed")
                observation = send(message)
                if self.include_tools:
                    observation = replace(observation, messages=(
                        {"role": "user", "content": message},
                        {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "one", "function": {"name": "read", "arguments": "{}"}},
                            {"id": "two", "function": {"name": "search", "arguments": "{}"}},
                        ]},
                        {"role": "tool", "tool_call_id": "one", "content": "Read result"},
                        {"role": "tool", "tool_call_id": "two", "content": "Search result"},
                        {"role": "assistant", "content": observation.response},
                    ), metrics={**observation.metrics, "api_calls": 3})
                return observation

            def tracked_close():
                close()
                if self.failure == "close" and context.treatment.branch == "candidate":
                    raise RuntimeError("actual session cleanup failed")

            session.send, session.close = tracked_send, tracked_close
            return session

        factory.open = opened
        return factory


def report(baseline, candidate):
    return normalize_external_report({"baseline": {"score": baseline, "reason": "old"},
                                      "candidate": {"score": candidate, "reason": "new"}}, {"request_id": "id"})


@pytest.mark.parametrize("bad", [True, False, "50", None, -1, 101, float("nan"), float("inf")])
def test_external_scores_reject_nonfinite_or_non_numeric_values(bad):
    with pytest.raises(ValueError):
        report(60, bad)


@pytest.mark.parametrize("settings", [
    {"min_score": True}, {"min_improvement": float("nan")}, {"timeout_seconds": False},
    {"url": "https://user:secret@example/score"}, {"url": "https://example/score?api_key=secret"},
    {"url": "https://example/score?access_token=secret"}, {"url": "file:///tmp/score"},
    {"url": ""}, {"secret": "plaintext"}, {"rubric": ["rule"]},
    {"secret_ref": "sk-real-key-must-never-be-stored"}, {"secret_ref": "sref_short"},
])
def test_external_configuration_is_strict_and_uses_secret_refs(settings):
    with pytest.raises(ValueError):
        evaluation(**settings)


def test_default_spec_wire_and_policy_are_unchanged():
    original = spec()
    result = evaluate_spec(original, host=Host(), judge=judge)
    assert result["accepted"] is True
    assert "external_evaluation" not in result and "evaluation" not in original.as_dict()
    assert spec_from_dict(original.as_dict()).as_dict() == original.as_dict()
    reference = "sref_" + "a" * 32
    assert evaluation(secret_ref=reference)["external_agent"]["secret_ref"] == reference


def test_external_contract_roundtrip_and_policy_must_agree():
    fixed = external_spec()
    assert spec_from_dict(fixed.as_dict()).as_dict() == fixed.as_dict()
    restored = skill_replay_spec(skill_job_from_spec(fixed))
    assert restored.evaluation == fixed.evaluation and restored.policy_id == EXTERNAL_AGENT_SCORE_POLICY
    with pytest.raises(ReplayContractError):
        replace(fixed, evaluation={})
    with pytest.raises(ReplayContractError):
        replace(spec(), evaluation=evaluation())


def test_external_score_uses_actual_output_and_excludes_historical_answers_and_edits():
    baseline = inline_plan()
    candidate = changed_inline_plan(baseline)
    case = inline_case(baseline)
    case["provenance"].update({
        "context_snapshot": {"items": [{"content": "actual frozen context"}]},
    })
    case["provenance"]["session_snapshot"]["turns"][0].update(
        response="old-answer-secret", feedback="old-rating-secret",
    )
    case["standard_answer"] = "standard-answer-secret"
    fixed = ReplaySpec(
        run_id="context-scoring", subject_kind="execution", subject_id="variable-rules",
        dataset_format=DATASET_SCHEMA, cases=(case,),
        baseline=ReplayTreatment("execution_plan", "1", execution_plan=baseline),
        candidate=ReplayTreatment("execution_plan", "2", execution_plan=candidate),
        limits={"max_interactions": 1}, evaluation=evaluation(min_score=70), policy_id=EXTERNAL_AGENT_SCORE_POLICY,
        metadata={"candidate_edits": "candidate-edit-secret"},
    )
    host = ScoringHost(metrics=False)
    def post(endpoint, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", endpoint), json={
            "choices": [{"message": {"content": "Answer"}}],
        })
    transport = ObservedHttpFactory({"endpoint": "https://model.invalid/chat"}, post=post)
    host.resolve_replay_factory = lambda runtime_type, source_session: transport
    result = evaluate_spec(fixed, host=host, judge=judge)
    assert result["verdict"] == "accept" and len(host.requests) == 1
    request = host.requests[0]
    assert request["case"]["inputs"]["template_variables"] == {"rules": "Original A", "person": "Reader"}
    assert request["case"]["inputs"]["input_messages"][0]["content"] == "Original public history"
    assert request["case"]["inputs"]["context_snapshot"]["items"][0]["content"] == "actual frozen context"
    assert request["branches"]["candidate"]["final_response"] == "Answer"
    assert request["branches"]["baseline"]["interactions"][0]["prompt"] == case["query"]
    text = json.dumps(request)
    for forbidden in ("old-answer-secret", "old-rating-secret", "standard-answer-secret", "candidate-edit-secret",
                      "min_score", "min_improvement", "checklist_report", "observed_requests"):
        assert forbidden not in text
    assert result["efficiency"]["candidate"]["total_tokens"] == "unavailable"
    assert result["cases"][0]["external_evaluation"] == result["external_evaluation"]
    assert valid_external_result(result, fixed.evaluation, case)
    changed = copy.deepcopy(case)
    changed["query"] = "different query"
    assert not valid_external_result(result, fixed.evaluation, changed)
    assert not valid_external_result(result, evaluation(min_score=71), case)


def test_one_interaction_external_replay_never_calls_auxiliary_judge_or_feedback():
    def forbidden(**kwargs):
        raise AssertionError("single-interaction external Replay must not invoke a Checklist model")
    fixed = replace(external_spec(), limits={"timeout_seconds": 30, "max_interactions": 1})
    host = RecordingHost()
    host.judge_harness = forbidden
    result = evaluate_spec(fixed, host=host, judge=forbidden, feedback=forbidden)
    assert result["verdict"] == "accept" and len(host.requests) == 1
    assert sorted(host.sent) == [(branch, fixed.cases[0]["query"]) for branch in ("baseline", "candidate")]
    for branch in ("baseline", "candidate"):
        actual = result["cases"][0][branch]
        assert actual["ok"] is True and actual["completed"] is False
        assert actual["checklist_report"]["judge"] == "not_run"
        assert actual["checklist_report"]["all_satisfied"] is False
        assert actual["checklist_report"]["items"] == [] and actual["disclosures"] == []
        assert actual["interaction_turns"] == 1 and actual["total_tokens"] > 0
    assert valid_external_result(result, fixed.evaluation, fixed.cases[0])
    aggregated = aggregate_results([("first", result), ("second", copy.deepcopy(result))])
    assert aggregated["verdict"] == "accept"
    for branch in ("baseline", "candidate"):
        assert aggregated["checklist"][branch]["judge"] == "not_run"
        assert aggregated["checklist"][branch]["all_satisfied"] is False
        assert aggregated["checklist"][branch]["total"] == 0


def test_external_single_send_keeps_complete_internal_tool_trajectory_and_actual_usage():
    def forbidden(**kwargs):
        raise AssertionError("No auxiliary call is needed")

    fixed = replace(external_spec(), limits={"max_interactions": 1})
    host = RecordingHost(include_tools=True)
    result = evaluate_spec(fixed, host=host, judge=forbidden, feedback=forbidden)
    assert result["accepted"] and len(host.requests) == 1 and len(host.sent) == 2
    for branch in ("baseline", "candidate"):
        actual = result["cases"][0][branch]
        assert actual["interaction_turns"] == 1 and actual["api_calls"] == 3
        assert actual["tool_call_count"] == 2
        assert actual["total_tokens"] == (20 if branch == "baseline" else 10)
        assert len(actual["messages"]) == 5
        assert host.requests[0]["branches"][branch]["messages"] == actual["messages"]


@pytest.mark.parametrize("external", [False, True])
def test_configured_multiple_interactions_retain_existing_checklist_feedback_loop(external):
    calls = []

    def progressive(**kwargs):
        rounds = len(kwargs["interactions"])
        calls.append(rounds)
        return {"judge": "model", "all_satisfied": rounds == 2,
                "items": [{**item, "satisfied": rounds == 2, "evidence": "actual answer"}
                          for item in kwargs["checklist"]]}

    def feedback(**kwargs):
        return "Existing progressive feedback"

    fixed = replace(external_spec() if external else spec(), limits={"timeout_seconds": 30, "max_interactions": 2})
    host = RecordingHost()
    result = evaluate_spec(fixed, host=host, judge=progressive, feedback=feedback)
    assert sorted(calls) == [1, 1, 2, 2]
    assert result["verdict"] == "accept"
    assert len(host.requests) == int(external)
    for branch in ("baseline", "candidate"):
        assert [message for side, message in host.sent if side == branch] == [
            fixed.cases[0]["query"], "Existing progressive feedback",
        ]
        actual = result["cases"][0][branch]
        assert actual["ok"] is True and actual["completed"] is True
        assert actual["checklist_report"]["judge"] == "model"
        assert len(actual["disclosures"]) == 1


@pytest.mark.parametrize("failure", ["judge_unavailable", "judge_raise", "feedback_raise", "harness_raise"])
@pytest.mark.parametrize("external", [False, True])
def test_auxiliary_failures_only_stop_external_feedback_but_default_checklist_remains_strict(failure, external):
    secret = "auxiliary-secret-provider-text-must-not-be-returned"

    def auxiliary_judge(**kwargs):
        if failure == "judge_raise":
            raise RuntimeError("auxiliary Judge failure " + secret)
        if failure in {"judge_unavailable", "harness_raise"}:
            return {"judge": "unavailable", "all_satisfied": False}
        return {"judge": "model", "all_satisfied": False,
                "items": [{**item, "satisfied": False} for item in kwargs["checklist"]]}

    def auxiliary_feedback(**kwargs):
        raise RuntimeError("auxiliary feedback failure " + secret)

    def unavailable_harness():
        raise RuntimeError("auxiliary harness unavailable " + secret)

    fixed = replace(external_spec() if external else spec(), limits={"timeout_seconds": 30, "max_interactions": 2})
    host = RecordingHost()
    if failure == "harness_raise":
        host.judge_harness = unavailable_harness
    if failure == "harness_raise" and not external:
        with pytest.raises(RuntimeError, match="harness unavailable"):
            evaluate_spec(fixed, host=host, judge=auxiliary_judge, feedback=auxiliary_feedback)
        assert host.sent == [] and host.requests == []
        return
    result = evaluate_spec(fixed, host=host, judge=auxiliary_judge, feedback=auxiliary_feedback)
    assert result["verdict"] == ("accept" if external else "inconclusive")
    assert len(host.requests) == int(external)
    assert len(host.sent) == 2 and len(host.closed) == 2
    if external:
        assert secret not in json.dumps(result)
    for branch in ("baseline", "candidate"):
        actual = result["cases"][0][branch]
        assert actual["ok"] is external and actual["completed"] is False
        assert actual["final_response"] == "Answer" and actual["disclosures"] == []
        assert not actual["checklist_report"]["all_satisfied"]
        if external and failure in {"judge_raise", "feedback_raise", "harness_raise"}:
            assert actual["auxiliary_errors"][0]["stage"] == {
                "judge_raise": "judge", "feedback_raise": "feedback", "harness_raise": "harness",
            }[failure]


@pytest.mark.parametrize("host", [Host(), ScoringHost(fail=True), ScoringHost(scores=(80, float("nan")))])
def test_missing_failed_or_invalid_external_evaluator_never_falls_back(host):
    result = evaluate_spec(external_spec(), host=host, judge=judge)
    assert result["verdict"] == "inconclusive" and result["accepted"] is False
    assert result["external_evaluation"]["status"] == "unavailable"
    assert "secret-provider-error" not in json.dumps(result)
    assert len(host.closed) == 2


@pytest.mark.parametrize("failure", ["send", "close"])
def test_failed_real_business_or_cleanup_skips_external_scoring(failure):
    host = RecordingHost(failure=failure)
    fixed = replace(external_spec(), limits={"max_interactions": 1})
    result = evaluate_spec(fixed, host=host, judge=judge)
    assert result["verdict"] == "inconclusive" and not result["accepted"] and host.requests == []
    assert len(host.closed) == 2
    actual = result["cases"][0]["candidate"]
    assert actual["ok"] is False
    assert actual["error_code"] == ("REPLAY_FAILED" if failure == "send" else "REPLAY_CLEANUP_FAILED")


def test_external_single_interaction_still_requires_real_request_verification():
    baseline = inline_plan()
    fixed = replace(external_spec(), subject_kind="execution", subject_id="variable-rules", subject_ids=(),
                    baseline=ReplayTreatment("execution_plan", "1", execution_plan=baseline),
                    candidate=ReplayTreatment("execution_plan", "2", execution_plan=changed_inline_plan(baseline)),
                    cases=(inline_case(baseline),), limits={"max_interactions": 1})

    def post(endpoint, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", endpoint), json={
            "choices": [{"message": {"content": "Actual business reply"}}],
        })

    factory = ObservedHttpFactory({"endpoint": "https://model.invalid/chat"}, post=post)
    opened = factory.open
    closed = []

    def without_receipt(context):
        session = opened(context)
        send, close = session.send, session.close

        def tampered(message):
            return replace(send(message), execution_receipt=None)

        def cleanup():
            closed.append(context.treatment.branch)
            close()

        session.send, session.close = tampered, cleanup
        return session

    factory.open = without_receipt
    host = ScoringHost()
    host.resolve_replay_factory = lambda *_: factory
    result = evaluate_spec(fixed, host=host, judge=judge)
    assert result["verdict"] == "inconclusive" and not result["accepted"] and host.requests == []
    assert sorted(closed) == ["baseline", "candidate"]
    for branch in ("baseline", "candidate"):
        actual = result["cases"][0][branch]
        assert actual["ok"] is False and actual["execution_verification"] != "verified"


@pytest.mark.parametrize("scores,settings,expected", [
    ([(90, 80), (50, 90)], {}, "accept"),
    ([(80, 80)], {}, "inconclusive"),
    ([(90, 80)], {}, "reject"),
    ([(50, 60)], {"min_score": 70}, "reject"),
    ([(50, 60)], {"min_improvement": 11}, "reject"),
    ([(50, 60)], {"min_improvement": 10, "min_score": 60}, "accept"),
    ([(80, 80.1)], {"min_improvement": 0.1}, "accept"),
])
def test_external_decision_compares_all_case_means(scores, settings, expected):
    result = external_score_decision([report(*pair) for pair in scores], evaluation(**settings))
    assert result["verdict"] == expected


def test_aggregation_does_not_apply_old_per_case_reject_veto():
    fixed = external_spec()
    bad = evaluate_spec(fixed, host=ScoringHost(scores=(90, 80)), judge=judge)
    good = evaluate_spec(fixed, host=ScoringHost(scores=(50, 90)), judge=judge)
    assert bad["verdict"] == "reject"
    result = aggregate_results([("first", bad), ("second", good)])
    assert result["verdict"] == "accept"
    assert result["external_evaluation"]["baseline_score"] == 70
    assert result["external_evaluation"]["candidate_score"] == 85
    assert len(result["cases"]) == 2
    missing = copy.deepcopy(good)
    missing.pop("external_evaluation")
    assert aggregate_results([("first", bad), ("second", missing)])["verdict"] == "inconclusive"
    mismatched = copy.deepcopy(good)
    mismatched["evaluation_policy_hash"] = "another-policy"
    assert aggregate_results([("first", bad), ("second", mismatched)])["verdict"] == "inconclusive"
    failed = {"status": "failed", "evaluation": fixed.evaluation,
              "evaluation_policy_hash": evaluation_policy_hash(fixed.evaluation)}
    all_failed = aggregate_results([("first", failed), ("second", failed)])
    assert all_failed["verdict"] == "inconclusive" and all_failed["evaluation"] == fixed.evaluation
    assert all_failed["external_evaluation"]["evaluated_case_count"] == 0


def test_usage_is_explicit_and_not_counted_as_agent_efficiency():
    request = {"request_id": "id"}
    raw = {"baseline": {"score": 60, "reason": "old"}, "candidate": {"score": 80, "reason": "new"}}
    assert normalize_external_report(raw, request)["usage"] == {"available": False}
    assert normalize_external_report({**raw, "usage": {"total_tokens": 123}}, request)["usage"] == {
        "available": True, "total_tokens": 123,
    }
    invalid_usage = normalize_external_report({**raw, "usage": {"total_tokens": True}}, request)
    assert invalid_usage["candidate"]["score"] == 80
    assert invalid_usage["usage"] == {"available": False, "incomplete_reason": "invalid_usage"}
    assert external_score_decision([invalid_usage], evaluation())["verdict"] == "accept"
    with pytest.raises(ValueError):
        normalize_external_report({**raw, "request_id": "another-id"}, request)


def test_legacy_embedded_and_cli_paths_fail_closed_before_execution():
    fixed = external_spec()
    gateway = EmbeddedReplayGateway(lambda *args, **kwargs: pytest.fail("must not execute legacy evaluator"))
    with pytest.raises(ReplayUnsupported):
        gateway.evaluate(fixed)
    result = evaluate_job("legacy", job=skill_job_from_spec(fixed))
    assert result["status"] == "unsupported" and result["verdict"] == "inconclusive"


def test_http_transport_sends_one_pair_with_secret_header_and_idempotency(monkeypatch):
    seen = []
    real_client = httpx.Client
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"baseline": {"score": 60, "reason": "old"},
                                       "candidate": {"score": 80, "reason": "new"}})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    response = evaluate_external_http({"request_id": "pair-id", "branches": {}},
                                      evaluation()["external_agent"], "control-secret")
    assert response["candidate"]["score"] == 80 and len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer control-secret"
    assert seen[0].headers["Idempotency-Key"] == "pair-id"
    assert json.loads(seen[0].content) == {"request_id": "pair-id", "branches": {}}


@pytest.mark.parametrize("status,body", [(500, b"control-secret"), (200, b"x" * (MAX_RESPONSE_BYTES + 1)),
                                         (200, b"invalid-json-control-secret")])
def test_http_errors_and_oversized_responses_are_bounded_and_do_not_leak(monkeypatch, status, body):
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(RuntimeError, match="external evaluator request failed") as error:
        evaluate_external_http({"request_id": "id"}, evaluation()["external_agent"], "control-secret")
    assert "control-secret" not in str(error.value)


def test_external_policy_hash_changes_with_rubric_and_thresholds():
    assert evaluation_policy_hash(evaluation()) != evaluation_policy_hash(evaluation(min_score=10))
    assert evaluation_policy_hash(evaluation()) != evaluation_policy_hash(evaluation(rubric="different rubric"))
