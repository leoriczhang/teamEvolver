"""Replay judges account for real SDK attempts without changing their requests."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from team_replay import judging
from teamEvolver import llm
from teamEvolver.llm_usage import LLMUsageBudgetExceeded, collect_llm_usage

_HARNESS = {
    "model": "fake-replay-model",
    "base_url": "http://provider.invalid/v1",
    "api_key": "fake-private-key",
    "max_tokens": 123,
}
_CHECKS = [{"id": "requirement-a", "text": "保留已完成内容", "kind": "output"}]


def _response(content="{}", *, total=7, usage=True, finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=5, total_tokens=total) if usage else None,
    )


class _ProviderError(RuntimeError):
    def __init__(self, status, body):
        super().__init__("private-provider-error")
        self.response = SimpleNamespace(status_code=status, text=body)


@pytest.fixture
def fake_provider(monkeypatch):
    state = SimpleNamespace(calls=[], clients=[], thread_names=[], lock=threading.Lock(), result=None)
    tenant_id = "replay-usage-test-" + uuid.uuid4().hex
    monkeypatch.setattr(llm, "_current_tenant_id", lambda: tenant_id)

    class FakeOpenAI:
        def __init__(self, **options):
            self.options = options
            self.closed = False
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            state.clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()
            return False

        def close(self):
            self.closed = True

        def create(self, **body):
            with state.lock:
                state.calls.append(copy.deepcopy(body))
                state.thread_names.append(threading.current_thread().name)
                value = state.result
            value = value(body) if callable(value) else value
            if isinstance(value, BaseException):
                raise value
            return value

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    def configure(result):
        state.result = result
        return state

    yield configure
    llm._reset_dispatchers_for_testing()


def _complete(*, harness=None, payload=None, system="private-system", schema=None, temperature=0.0):
    return judging.complete_json(
        harness=harness or _HARNESS,
        system=system,
        payload=payload or {"query": "private-user-input"},
        schema=schema or {"type": "object"},
        temperature=temperature,
    )


@pytest.mark.asyncio
async def test_complete_json_preserves_exact_request_and_accounts_for_sdk_usage(fake_provider):
    state = fake_provider(_response('{"answer":"完成"}', total=17))
    harness = {**_HARNESS, "model": "ccr/frozen-model", "api_key": "Bearer fake-private-key"}
    payload = {"query": "private-user-input", "locale": "中文"}
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    with collect_llm_usage("replay") as meter:
        result = await asyncio.to_thread(
            _complete, harness=harness, payload=payload, schema=schema, temperature=0.125,
        )
    assert result == {"answer": "完成"}
    assert state.calls == [{
        "model": "ccr/frozen-model",
        "messages": [
            {"role": "system", "content": "private-system\nJSON Schema: " + json.dumps(schema)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.125,
        "max_tokens": 123,
        "response_format": {"type": "json_object"},
    }]
    assert state.clients[0].options == {
        "api_key": "Bearer fake-private-key", "base_url": _HARNESS["base_url"],
        "timeout": 120, "max_retries": 0,
    }
    assert state.clients[0].closed is True
    assert state.thread_names[0].startswith("te-llm-")
    usage = meter.snapshot()
    assert usage["known_total_tokens"] == 17
    assert usage["attempt_count"] == 1
    assert usage["unknown_attempts"] == usage["pending_dispatches"] == 0
    assert usage["complete"] is True
    assert usage["attempts"][0]["model"] == "ccr/frozen-model"
    for private in ("private-user-input", "private-system", "fake-private-key", "provider.invalid"):
        assert private not in json.dumps(usage)


@pytest.mark.asyncio
@pytest.mark.parametrize("total,has_usage,known", [(0, True, True), (None, True, False), (7, False, False)])
async def test_zero_is_known_but_missing_provider_usage_stays_unknown(fake_provider, total, has_usage, known):
    state = fake_provider(_response(total=total, usage=has_usage))
    with collect_llm_usage("usage-shape") as meter:
        assert await asyncio.to_thread(_complete) == {}
    usage = meter.snapshot()
    assert len(state.calls) == usage["attempt_count"] == 1
    assert usage["known_total_tokens"] == 0
    assert usage["complete"] is known
    assert usage["unknown_attempts"] == int(not known)
    assert usage["attempts"][0]["usage_known"] is known


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (400, "unsupported max_tokens; use max_completion_tokens"),
    (400, "Stream must be set to true"),
    (503, "private-error-response"),
])
async def test_provider_failures_are_unknown_without_new_retries(fake_provider, status, body):
    state = fake_provider(_ProviderError(status, body))
    with collect_llm_usage("failed-judge") as meter:
        with pytest.raises(_ProviderError):
            await asyncio.to_thread(_complete)
    usage = meter.snapshot()
    assert len(state.calls) == usage["attempt_count"] == 1
    assert usage["known_total_tokens"] == 0
    assert usage["unknown_attempts"] == 1
    assert usage["complete"] is False
    assert usage["attempts"][0]["status"] == "failed"
    assert state.clients[0].closed is True
    assert "private-provider-error" not in json.dumps(usage)
    assert body not in json.dumps(usage)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,error", [("{invalid", json.JSONDecodeError), ("[]", ValueError)])
async def test_invalid_json_keeps_successful_provider_cost(fake_provider, content, error):
    state = fake_provider(_response(content, total=19))
    with collect_llm_usage("invalid-json") as meter:
        with pytest.raises(error):
            await asyncio.to_thread(_complete)
    usage = meter.snapshot()
    assert len(state.calls) == usage["attempt_count"] == 1
    assert usage["known_total_tokens"] == 19
    assert usage["unknown_attempts"] == 0
    assert usage["attempts"][0]["status"] == "succeeded"
    assert usage["complete"] is True


@pytest.mark.asyncio
async def test_empty_length_response_keeps_original_single_attempt_policy(fake_provider):
    state = fake_provider(_response("", total=3, finish="length"))
    with collect_llm_usage("empty-length") as meter:
        assert await asyncio.to_thread(_complete) == {}
    assert len(state.calls) == meter.snapshot()["attempt_count"] == 1
    assert meter.snapshot()["known_total_tokens"] == 3
    assert state.calls[0]["max_tokens"] == 123


@pytest.mark.asyncio
async def test_judge_and_feedback_real_completion_paths_share_cost_scope(fake_provider):
    def respond(body):
        if body["messages"][0]["content"].startswith(judging.JUDGE_SYSTEM):
            return _response(json.dumps({
                "items": [{"id": "requirement-a", "satisfied": True, "evidence": "回复中保留了已完成内容"}],
                "all_satisfied": True, "positive_observations": ["已完成内容得到保留"],
            }), total=11)
        return _response(json.dumps({"message": "已完成内容已经保留，请继续补充说明。"}), total=13)

    state = fake_provider(respond)
    with collect_llm_usage("judge-and-feedback") as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[{"prompt": "请保留内容", "response": "已完成内容得到保留"}], messages=[], artifacts=[],
        )
        feedback = await asyncio.to_thread(
            judging.render_user_feedback, harness=_HARNESS, response="已完成内容得到保留",
            positive_observations=report["positive_observations"], selected_items=_CHECKS,
            disclosed_requirements=[], round_number=1,
        )
    assert report["judge"] == "model"
    assert report["all_satisfied"] is True
    assert feedback == "已完成内容已经保留，请继续补充说明。"
    usage = meter.snapshot()
    assert len(state.calls) == usage["attempt_count"] == 2
    assert usage["known_total_tokens"] == 24
    assert usage["complete"] is True
    assert state.calls[0]["temperature"] == 0.0
    assert state.calls[1]["temperature"] == 0.2
    assert all(client.closed for client in state.clients)


@pytest.mark.asyncio
async def test_judge_unavailable_still_records_failed_provider_attempt(fake_provider):
    state = fake_provider(_ProviderError(503, "private-error-response"))
    with collect_llm_usage("judge-unavailable") as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[], messages=[], artifacts=[],
        )
    assert report["judge"] == "unavailable"
    assert report["all_satisfied"] is False
    assert len(state.calls) == meter.snapshot()["attempt_count"] == 1
    assert meter.snapshot()["unknown_attempts"] == 1
    assert meter.snapshot()["complete"] is False


@pytest.mark.asyncio
async def test_judge_budget_denies_next_sdk_attempt_without_retry(fake_provider):
    state = fake_provider(_response(total=7))
    with collect_llm_usage("budget", budget_tokens=7) as meter:
        await asyncio.to_thread(_complete)
        with pytest.raises(LLMUsageBudgetExceeded):
            await asyncio.to_thread(_complete)
    assert len(state.calls) == meter.snapshot()["attempt_count"] == 1
    assert meter.snapshot()["known_total_tokens"] == 7
    assert meter.snapshot()["budget_reached"] is True


@pytest.mark.asyncio
async def test_concurrent_branch_scopes_remain_isolated_across_model_loop(fake_provider):
    barrier = threading.Barrier(2)

    def respond(body):
        barrier.wait(timeout=3)
        return _response(total={"baseline": 3, "candidate": 5}[body["model"]])

    state = fake_provider(respond)

    async def branch(name):
        with collect_llm_usage(name) as meter:
            for _ in range(2):
                await asyncio.to_thread(_complete, harness={**_HARNESS, "model": name})
        return meter.snapshot()

    baseline, candidate = await asyncio.gather(branch("baseline"), branch("candidate"))
    assert len(state.calls) == 4
    assert (baseline["attempt_count"], baseline["known_total_tokens"]) == (2, 6)
    assert (candidate["attempt_count"], candidate["known_total_tokens"]) == (2, 10)
    assert {item["model"] for item in baseline["attempts"]} == {"baseline"}
    assert {item["model"] for item in candidate["attempts"]} == {"candidate"}
    assert {item["attempt_id"] for item in baseline["attempts"]}.isdisjoint(
        item["attempt_id"] for item in candidate["attempts"]
    )
    assert baseline["complete"] is candidate["complete"] is True


@pytest.mark.asyncio
async def test_replay_and_platform_clients_share_global_provider_slots(fake_provider, monkeypatch):
    started, waiting, release = threading.Event(), threading.Event(), threading.Event()

    class OneGlobalSlot:
        def __init__(self):
            self.semaphore = threading.BoundedSemaphore(1)
            self.lock = threading.Lock()
            self.active = self.maximum = 0

        def __enter__(self):
            with self.lock:
                if self.active:
                    waiting.set()
            self.semaphore.acquire()
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)

        def __exit__(self, *_):
            with self.lock:
                self.active -= 1
            self.semaphore.release()

    slots = OneGlobalSlot()
    monkeypatch.setattr(llm, "_GLOBAL_MODEL_SLOTS", slots)

    def respond(_):
        started.set()
        if not release.wait(3):
            raise RuntimeError("fake provider was not released")
        return _response('{"message":"ok"}', total=7)

    state = fake_provider(respond)
    client = llm.AsyncLLMClient(
        api_key="fake-private-key", base_url=_HARNESS["base_url"], model=_HARNESS["model"],
        max_retries=1, tenant_id="other-tenant-" + uuid.uuid4().hex,
    )
    tasks = []
    try:
        with collect_llm_usage("shared-slots") as meter:
            tasks.append(asyncio.create_task(asyncio.to_thread(_complete)))
            assert await asyncio.to_thread(started.wait, 2)
            tasks.append(asyncio.create_task(client.chat([])))
            assert await asyncio.to_thread(waiting.wait, 2)
            assert len(state.calls) == 1
            assert meter.snapshot()["complete"] is False
            release.set()
            await asyncio.gather(*tasks)
        assert len(state.calls) == meter.snapshot()["attempt_count"] == 2
        assert meter.snapshot()["known_total_tokens"] == 14
        assert meter.snapshot()["complete"] is True
        assert slots.maximum == 1
        assert all(name.startswith("te-llm-") for name in state.thread_names)
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        client._client.close()


@pytest.mark.asyncio
async def test_timeout_keeps_live_sdk_cost_pending_until_real_completion(fake_provider, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(judging, "_RESULT_TIMEOUT_SECONDS", 0.05)
    submission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(judging, "_submission_slots", submission)

    def respond(_):
        started.set()
        if not release.wait(3):
            raise RuntimeError("fake provider was not released")
        return _response(total=23)

    state = fake_provider(respond)
    try:
        with collect_llm_usage("timed-out") as meter:
            task = asyncio.create_task(asyncio.to_thread(_complete))
            assert await asyncio.to_thread(started.wait, 2)
            with pytest.raises(TimeoutError):
                await task
        pending = meter.snapshot()
        assert pending["attempt_count"] == pending["in_flight_attempts"] == 1
        assert pending["pending_dispatches"] == pending["unknown_attempts"] == 1
        assert pending["known_total_tokens"] == 0
        assert pending["complete"] is False
        assert state.clients[0].closed is False
        assert submission.acquire(blocking=False) is False
        release.set()
        async with asyncio.timeout(2):
            while not meter.snapshot()["complete"] or not state.clients[0].closed:
                await asyncio.sleep(0.01)
            while not submission.acquire(blocking=False):
                await asyncio.sleep(0.01)
        submission.release()
        finished = meter.snapshot()
        assert len(state.calls) == finished["attempt_count"] == 1
        assert finished["known_total_tokens"] == 23
        assert finished["unknown_attempts"] == finished["pending_dispatches"] == 0
    finally:
        release.set()


def _format_output():
    return {"items": [{"id": "requirement-a", "satisfied": True, "evidence": "实际回复保留了内容"},
                      {"id": "requirement-b", "satisfied": False, "evidence": "未给出要求的产物"}],
            "all_satisfied": False, "positive_observations": ["已保留内容"]}


def _format_judge(raw, calls):
    def completion(**request):
        calls.append(request)
        return copy.deepcopy(raw)
    return judging.judge_checklist(
        harness={**_HARNESS, "temperature": 0.125},
        checklist=[*_CHECKS, {"id": "requirement-b", "text": "提供产物", "kind": "artifact"}],
        interactions=[{"prompt": "请求", "response": "实际回复保留了内容"}], messages=[], artifacts=[],
        completion=completion,
    )


def test_judge_format_budget_keeps_all_exact_ids_and_frozen_single_request_parameters():
    calls = []
    result = _format_judge(_format_output(), calls)
    assert result["judge"] == "model" and result["all_satisfied"] is False
    assert result["satisfied_ids"] == ["requirement-a"] and result["unmet_ids"] == ["requirement-b"]
    assert len(calls) == 1 and calls[0]["temperature"] == 0.125
    assert calls[0]["harness"] == {**_HARNESS, "temperature": 0.125}
    schema = calls[0]["schema"]
    assert schema["properties"]["items"]["minItems"] == schema["properties"]["items"]["maxItems"] == 2
    assert schema["properties"]["items"]["items"]["properties"]["id"]["enum"] == ["requirement-a", "requirement-b"]
    assert schema["properties"]["items"]["items"]["properties"]["evidence"]["maxLength"] == 320
    assert schema["properties"]["positive_observations"]["maxItems"] == 3
    assert "每个 id 恰好一次" in calls[0]["system"] and "不输出判断过程" in calls[0]["system"]
    assert calls[0]["payload"]["checklist"][1]["text"] == "提供产物"


@pytest.mark.parametrize("fault,code", [
    ("inconsistent_all", "all_satisfied_consistency"), ("boolean_string", "item_boolean"),
    ("all_string", "items_shape"), ("duplicate_id", "item_id_coverage"),
    ("missing_item", "items_shape"), ("unknown_id", "item_id_coverage"),
    ("evidence_object", "item_evidence"), ("evidence_oversized", "item_evidence"),
    ("positive_object", "positive_observations"), ("positive_oversized", "positive_observations"),
    ("too_many_positives", "positive_observations"), ("extra_reasoning", "object_fields"),
    ("empty_true_evidence", "item_evidence"),
])
def test_invalid_judge_format_is_unavailable_after_exactly_one_format_retry_without_repair(fault, code):
    raw, calls = _format_output(), []
    if fault == "inconsistent_all":
        raw["all_satisfied"] = True
    elif fault == "boolean_string":
        raw["items"][0]["satisfied"] = "true"
    elif fault == "all_string":
        raw["all_satisfied"] = "false"
    elif fault == "duplicate_id":
        raw["items"][1]["id"] = raw["items"][0]["id"]
    elif fault == "missing_item":
        raw["items"].pop()
    elif fault == "unknown_id":
        raw["items"][0]["id"] = "another-id"
    elif fault == "evidence_object":
        raw["items"][0]["evidence"] = {"quote": "实际内容"}
    elif fault == "evidence_oversized":
        raw["items"][0]["evidence"] = "证" * 321
    elif fault == "positive_object":
        raw["positive_observations"] = [{"text": "内容"}]
    elif fault == "positive_oversized":
        raw["positive_observations"] = ["证" * 161]
    elif fault == "too_many_positives":
        raw["positive_observations"] *= 4
    elif fault == "extra_reasoning":
        raw["reasoning"] = "Private reasoning must not be serialized"
    else:
        raw["items"][0]["evidence"] = "   "
    result = _format_judge(raw, calls)
    assert len(calls) == result["attempt_count"] == 2 and result["judge"] == "unavailable"
    assert calls[0] == calls[1]
    assert result["format_policy"] == "checklist-format-retry-v1"
    assert [item["format_diagnostic"]["schema_error"] for item in result["format_attempts"]] == [code, code]
    assert result["all_satisfied"] is False and result["satisfied_count"] == 0
    assert result["format_diagnostic"]["schema_error"] == code
    assert result["positive_observations"] == []
    assert "Private reasoning" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_json", [True, False])
async def test_length_finish_and_json_failure_keep_cost_and_safe_diagnostic(fake_provider, valid_json):
    import hashlib

    if valid_json:
        content = json.dumps({"items": [{"id": "requirement-a", "satisfied": True, "evidence": "已完成"}],
                              "all_satisfied": True, "positive_observations": []})
    else:
        content = '{"private_raw":"' + "private-response-secret" * 100
    state = fake_provider(_response(content, total=23, finish="length"))
    with collect_llm_usage("judge-format-diagnostic") as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[], messages=[], artifacts=[],
        )
    assert report["judge"] == "unavailable" and report["all_satisfied"] is False
    diagnostic = report["format_diagnostic"]
    assert diagnostic["finish_reason"] == "length"
    assert diagnostic["response_chars"] == len(content)
    assert diagnostic["response_utf8_bytes"] == len(content.encode())
    assert diagnostic["response_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert diagnostic["schema_error"] == ("incomplete_completion" if valid_json else "invalid_json")
    assert len(state.calls) == report["attempt_count"] == meter.snapshot()["attempt_count"] == 2
    assert state.calls[0] == state.calls[1]
    assert meter.snapshot()["known_total_tokens"] == 46 and meter.snapshot()["complete"] is True
    assert len({attempt["attempt_id"] for attempt in meter.snapshot()["attempts"]}) == 2
    assert meter.snapshot()["attempts"][0]["status"] == "succeeded"
    for secret in ("private-response-secret", "fake-private-key", "provider.invalid", "private_raw"):
        assert secret not in json.dumps(report) and secret not in json.dumps(meter.snapshot())


@pytest.mark.asyncio
async def test_provider_private_error_is_not_serialized_as_judge_error(fake_provider):
    state = fake_provider(_ProviderError(503, "Bearer private-key private-user-message"))
    with collect_llm_usage("private-provider-error") as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[], messages=[], artifacts=[],
        )
    assert report["error"] == "Checklist judge unavailable: judge_completion_unavailable"
    assert report["judge"] == "unavailable" and len(state.calls) == 1
    assert meter.snapshot()["unknown_attempts"] == 1
    for secret in ("private-provider-error", "private-key", "private-user-message"):
        assert secret not in json.dumps(report)


def test_false_all_satisfied_cannot_be_normalized_into_a_passing_judgment():
    raw, calls = _format_output(), []
    raw["items"][1]["satisfied"] = True
    result = _format_judge(raw, calls)
    assert len(calls) == 2 and result["judge"] == "unavailable"
    assert result["all_satisfied"] is False
    assert result["format_diagnostic"]["schema_error"] == "all_satisfied_consistency"


def _single_judgment(satisfied=True):
    return {"items": [{"id": "requirement-a", "satisfied": satisfied, "evidence": "实际回复的简短证据"}],
            "all_satisfied": satisfied, "positive_observations": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["json", "schema", "length"])
@pytest.mark.parametrize("second_satisfied", [True, False])
async def test_format_retry_uses_identical_request_retains_first_failure_and_both_actual_usages(
    fake_provider, first_failure, second_satisfied,
):
    first = _single_judgment()
    if first_failure == "schema":
        first["items"][0]["kind"] = "output"
    content = '{"private_raw":"private-response-secret' if first_failure == "json" else json.dumps(first)
    responses = iter([
        _response(content, total=17, finish="length" if first_failure == "length" else "stop"),
        _response(json.dumps(_single_judgment(second_satisfied)), total=29),
    ])
    state = fake_provider(lambda _: next(responses))
    with collect_llm_usage("bounded-format-retry") as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[{"prompt": "private-user-input", "response": "private-agent-output"}],
            messages=[{"role": "user", "content": "private-conversation"}], artifacts=[{"path": "private-artifact"}],
        )
    assert report["judge"] == "model" and report["all_satisfied"] is second_satisfied
    assert report["attempt_count"] == len(state.calls) == 2
    assert report["format_policy"] == judging.CHECKLIST_FORMAT_POLICY
    assert state.calls[0] == state.calls[1]
    assert report["format_attempts"][0]["judge"] == "unavailable"
    assert report["format_attempts"][0]["format_diagnostic"]["schema_error"] == {
        "json": "invalid_json", "schema": "item_fields", "length": "incomplete_completion",
    }[first_failure]
    assert report["format_attempts"][1]["judge"] == "model"
    usage = meter.snapshot()
    assert usage["attempt_count"] == 2 and usage["known_total_tokens"] == 46
    assert usage["complete"] is True and usage["unknown_attempts"] == usage["pending_dispatches"] == 0
    assert {attempt["usage"]["total_tokens"] for attempt in usage["attempts"]} == {17, 29}
    assert len({attempt["attempt_id"] for attempt in usage["attempts"]}) == 2
    assert all(attempt["status"] == "succeeded" for attempt in usage["attempts"])
    for private in ("private_raw", "private-response-secret", "private-user-input", "private-agent-output",
                    "private-conversation", "private-artifact", "fake-private-key", "provider.invalid"):
        assert private not in json.dumps(report) and private not in json.dumps(usage)


@pytest.mark.asyncio
async def test_valid_negative_judgment_is_final_and_costs_one_provider_attempt(fake_provider):
    state = fake_provider(_response(json.dumps(_single_judgment(False)), total=11))
    with collect_llm_usage("negative-final") as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[], messages=[], artifacts=[],
        )
    assert report["judge"] == "model" and report["all_satisfied"] is False
    assert report["attempt_count"] == len(report["format_attempts"]) == len(state.calls) == 1
    assert meter.snapshot()["known_total_tokens"] == 11


@pytest.mark.parametrize("error", [RuntimeError("private-provider"), TimeoutError("private-timeout"),
                                    LLMUsageBudgetExceeded("private-budget"), ValueError("private-input")])
def test_non_format_exception_has_one_call_without_private_error_or_retry(error):
    calls = []

    def completion(**request):
        calls.append(request)
        raise error

    report = judging.judge_checklist(harness=_HARNESS, checklist=_CHECKS, interactions=[], messages=[],
                                    artifacts=[], completion=completion)
    assert report["judge"] == "unavailable" and report["attempt_count"] == len(calls) == 1
    import hashlib

    schema = copy.deepcopy(judging.JUDGE_SCHEMA)
    schema["properties"]["items"].update(minItems=1, maxItems=1)
    schema["properties"]["items"]["items"]["properties"]["id"]["enum"] = ["requirement-a"]
    schema_text = json.dumps(schema)
    system_message = judging.JUDGE_SYSTEM + "\nJSON Schema: " + schema_text
    assert report["format_diagnostic"] == {
        "schema_error": "judge_completion_unavailable",
        "request_pin": {
            "version": "checklist-judge-input-pin-v1", "scope": "intended_request",
            "effective_system_sha256": hashlib.sha256(judging.JUDGE_SYSTEM.encode()).hexdigest(),
            "json_schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest(),
            "system_message_sha256": hashlib.sha256(system_message.encode()).hexdigest(),
            "checklist_sha256": hashlib.sha256(json.dumps(_CHECKS, ensure_ascii=False).encode()).hexdigest(),
        },
    }
    assert "private-" not in json.dumps(report)


@pytest.mark.asyncio
async def test_format_retry_cannot_bypass_actual_collector_budget(fake_provider):
    state = fake_provider(_response("{invalid", total=7))
    with collect_llm_usage("format-budget", budget_tokens=7) as meter:
        report = await asyncio.to_thread(
            judging.judge_checklist, harness=_HARNESS, checklist=_CHECKS,
            interactions=[], messages=[], artifacts=[],
        )
    assert report["judge"] == "unavailable" and report["attempt_count"] == 2
    assert report["format_attempts"][0]["format_diagnostic"]["schema_error"] == "invalid_json"
    assert report["format_diagnostic"]["schema_error"] == "judge_completion_unavailable"
    assert len(state.calls) == meter.snapshot()["attempt_count"] == 1
    assert meter.snapshot()["budget_reached"] is True and meter.snapshot()["known_total_tokens"] == 7


@pytest.mark.parametrize("finish", ["content_filter", "tool_calls", "function_call"])
def test_filtered_or_tool_completion_is_not_a_retryable_length_failure(finish):
    calls = []

    def completion(**request):
        calls.append(request)
        return judging._JSONResult(_single_judgment(), {"finish_reason": finish})

    report = judging.judge_checklist(harness=_HARNESS, checklist=_CHECKS, interactions=[], messages=[],
                                    artifacts=[], completion=completion)
    assert report["judge"] == "unavailable" and len(calls) == 1


def test_invalid_checklist_never_calls_provider_or_retries():
    calls = []
    report = judging.judge_checklist(harness=_HARNESS, checklist=[_CHECKS[0], _CHECKS[0]],
                                    interactions=[], messages=[], artifacts=[],
                                    completion=lambda **request: calls.append(request))
    assert report["judge"] == "unavailable" and calls == [] and report["attempt_count"] == 1
    assert report["format_diagnostic"]["schema_error"] == "invalid_checklist_ids"


def test_format_retry_freezes_inputs_against_completion_side_effects():
    original = {"harness": _HARNESS, "checklist": _CHECKS,
                "interactions": [{"prompt": "请求", "response": "实际内容"}], "messages": [], "artifacts": []}
    before, calls = copy.deepcopy(original), []

    def completion(**request):
        calls.append(copy.deepcopy(request))
        request["payload"]["checklist"][0]["text"] = "不可泄入重试的改动"
        request["harness"]["model"] = "mutated"
        return {} if len(calls) == 1 else _single_judgment()

    report = judging.judge_checklist(**original, completion=completion)
    assert report["judge"] == "model" and len(calls) == 2 and calls[0] == calls[1]
    assert original == before
