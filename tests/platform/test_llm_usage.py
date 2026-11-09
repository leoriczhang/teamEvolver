"""Provider-attempt accounting uses only local fake SDK responses."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from teamEvolver import llm, llm_usage
from teamEvolver.llm import AsyncLLMClient
from teamEvolver.llm_usage import LLMUsageBudgetExceeded, collect_llm_usage, provider_attempt


def _response(content="ok", *, total=7, finish="stop", usage=True):
    counters = SimpleNamespace(prompt_tokens=2, completion_tokens=5, total_tokens=total) if usage else None
    return SimpleNamespace(
        id="fake-response",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=counters,
    )


class _ProviderError(RuntimeError):
    def __init__(self, message="provider-secret", *, status=None, body=""):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status, text=body)


class _Stream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


def _event(content="", total=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))] if content else [],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=5, total_tokens=total) if total is not None else None,
    )


@pytest.fixture
def fake_client(monkeypatch):
    clients = []

    def build(results, *, max_retries=1):
        values = iter(results) if not callable(results) else None
        calls = []

        def create(**body):
            calls.append(body)
            value = results(body) if values is None else next(values)
            if isinstance(value, BaseException):
                raise value
            return value

        client = AsyncLLMClient(
            api_key="fake-secret-key",
            base_url="http://provider.invalid/v1",
            model="test-model",
            max_retries=max_retries,
            tenant_id=f"usage-test-{uuid.uuid4().hex}",
        )
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(client, "_client_for", lambda **_: sdk)
        clients.append(client)
        return client, calls

    yield build
    for client in clients:
        client._client.close()
    llm._reset_dispatchers_for_testing()


@pytest.mark.asyncio
async def test_default_client_behavior_without_collector(fake_client):
    client, calls = fake_client([_response()])
    assert await client.chat([{"role": "user", "content": "private-input"}]) == "ok"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_success_records_known_usage_and_only_safe_fields(fake_client):
    client, _ = fake_client([_response()])
    with collect_llm_usage("generation") as meter:
        assert await client.chat([{"role": "user", "content": "private-input"}]) == "ok"
    result = meter.snapshot()
    assert result == meter.snapshot()
    assert result["channel"] == "te_owned_llm"
    assert result["known_total_tokens"] == 7
    assert result["unknown_attempts"] == result["in_flight_attempts"] == 0
    assert result["attempt_count"] == 1
    assert result["complete"] is True
    entry = result["attempts"][0]
    assert entry["model"] == "test-model"
    assert entry["status"] == "succeeded"
    assert entry["usage"] == {"input_tokens": 2, "output_tokens": 5, "total_tokens": 7}
    assert entry["usage_known"] is True
    serialized = json.dumps(result, allow_nan=False)
    for private in ("private-input", "fake-secret-key", "provider.invalid", "fake-response", "messages", "api_key"):
        assert private not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [_response(usage=False), _response(total=None)])
async def test_missing_total_is_unknown_and_never_inferred(fake_client, response):
    client, _ = fake_client([response])
    with collect_llm_usage("judge") as meter:
        await client.chat([])
    result = meter.snapshot()
    assert result["known_total_tokens"] == 0
    assert result["unknown_attempts"] == 1
    assert result["complete"] is False
    assert result["attempts"][0]["usage_known"] is False


@pytest.mark.asyncio
async def test_explicit_zero_tokens_is_known(fake_client):
    client, _ = fake_client([_response(total=0)])
    with collect_llm_usage("zero") as meter:
        await client.chat([])
    assert meter.snapshot()["complete"] is True
    assert meter.snapshot()["known_total_tokens"] == 0


@pytest.mark.asyncio
async def test_failed_attempt_stays_unknown_without_error_body(fake_client):
    client, _ = fake_client([_ProviderError(status=401, body="private-error-response")])
    with collect_llm_usage("failed") as meter:
        with pytest.raises(_ProviderError):
            await client.chat([])
    result = meter.snapshot()
    assert result["unknown_attempts"] == 1
    assert result["known_total_tokens"] == 0
    assert result["attempts"][0]["status"] == "failed"
    assert "provider-secret" not in json.dumps(result)
    assert "private-error-response" not in json.dumps(result)


@pytest.mark.asyncio
async def test_transport_retry_records_each_actual_attempt(fake_client, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    client, calls = fake_client([_ProviderError(), _response(total=9)], max_retries=2)
    with collect_llm_usage("retry") as meter:
        assert await client.chat([]) == "ok"
    result = meter.snapshot()
    assert len(calls) == result["attempt_count"] == 2
    assert result["known_total_tokens"] == 9
    assert result["unknown_attempts"] == 1
    assert len({entry["attempt_id"] for entry in result["attempts"]}) == 2


@pytest.mark.asyncio
async def test_empty_length_response_and_retry_both_count(fake_client):
    client, calls = fake_client([_response("", total=3, finish="length"), _response(total=7)])
    with collect_llm_usage("length-retry") as meter:
        assert await client.chat([]) == "ok"
    assert len(calls) == 2
    assert calls[1]["max_completion_tokens"] == calls[0]["max_completion_tokens"] * 2
    assert meter.snapshot()["known_total_tokens"] == 10
    assert meter.snapshot()["complete"] is True


@pytest.mark.asyncio
async def test_parameter_negotiation_failure_is_still_an_attempt(fake_client):
    client, calls = fake_client([
        _ProviderError(status=400, body="unsupported max_completion_tokens"), _response(total=5)
    ])
    with collect_llm_usage("negotiation") as meter:
        await client.chat([])
    assert len(calls) == meter.snapshot()["attempt_count"] == 2
    assert "max_tokens" in calls[1]
    assert meter.snapshot()["unknown_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("include_usage", [False, True])
async def test_stream_fallback_counts_failure_and_actual_stream(fake_client, include_usage):
    events = [_event("hello"), _event(" world")]
    if include_usage:
        events.append(_event(total=11))
    client, calls = fake_client([
        _ProviderError(status=400, body="Stream must be set to true"), _Stream(events)
    ])
    with collect_llm_usage("stream") as meter:
        assert await client.chat([]) == "hello world"
    result = meter.snapshot()
    assert len(calls) == result["attempt_count"] == 2
    assert calls[1]["stream"] is True
    assert result["unknown_attempts"] == (1 if include_usage else 2)
    assert result["known_total_tokens"] == (11 if include_usage else 0)
    assert result["attempts"][1]["usage_known"] is include_usage
    assert result["complete"] is False  # The initial rejected request has unknown cost.


@pytest.mark.asyncio
async def test_failed_stream_is_unknown_even_with_partial_usage(fake_client):
    client, _ = fake_client([_Stream([_event("partial", total=4), _ProviderError()])])
    with collect_llm_usage("stream-failure") as meter:
        with pytest.raises(_ProviderError):
            await client._chat_via_stream(client._client_for(), {"model": "test-model"})
    result = meter.snapshot()
    assert result["known_total_tokens"] == 4
    assert result["unknown_attempts"] == 1
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][0]["usage_known"] is False


@pytest.mark.asyncio
async def test_budget_stops_later_attempts_without_ordinary_retries(fake_client):
    client, calls = fake_client([_response(total=7), _response()], max_retries=6)
    with collect_llm_usage("bounded", budget_tokens=7) as meter:
        await client.chat([])
        with pytest.raises(LLMUsageBudgetExceeded):
            await client.chat([])
    assert len(calls) == meter.snapshot()["attempt_count"] == 1
    assert meter.snapshot()["budget_reached"] is True


@pytest.mark.asyncio
async def test_budget_applies_to_hidden_length_retry(fake_client):
    client, calls = fake_client([_response("", total=7, finish="length"), _response()], max_retries=6)
    with collect_llm_usage("bounded-length", budget_tokens=7) as meter:
        with pytest.raises(LLMUsageBudgetExceeded):
            await client.chat([])
    assert len(calls) == 1
    assert meter.snapshot()["known_total_tokens"] == 7


@pytest.mark.asyncio
async def test_zero_budget_denies_first_provider_call(fake_client):
    client, calls = fake_client([_response()])
    with collect_llm_usage("no-budget", budget_tokens=0) as meter:
        with pytest.raises(LLMUsageBudgetExceeded):
            await client.chat([])
    assert calls == []
    assert meter.snapshot()["attempt_count"] == 0


@pytest.mark.asyncio
async def test_async_child_tasks_share_but_parallel_scopes_are_isolated(fake_client):
    client, _ = fake_client(lambda _: _response(total=3))

    async def scope(label, count):
        with collect_llm_usage(label) as meter:
            await asyncio.gather(*(client.chat([], model=label) for _ in range(count)))
        return meter.snapshot()

    first, second = await asyncio.gather(scope("alpha", 3), scope("beta", 2))
    assert (first["attempt_count"], first["known_total_tokens"]) == (3, 9)
    assert (second["attempt_count"], second["known_total_tokens"]) == (2, 6)
    assert {entry["model"] for entry in first["attempts"]} == {"alpha"}
    assert {entry["model"] for entry in second["attempts"]} == {"beta"}
    assert first["complete"] is second["complete"] is True


@pytest.mark.asyncio
async def test_to_thread_inherits_shared_collector():
    def invoke():
        with provider_attempt("thread-model") as attempt:
            attempt.record_usage({"total_tokens": 2})

    with collect_llm_usage("threads") as meter:
        await asyncio.gather(*(asyncio.to_thread(invoke) for _ in range(25)))
    assert meter.snapshot()["attempt_count"] == 25
    assert meter.snapshot()["known_total_tokens"] == 50


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_drop_live_provider_attempt(fake_client):
    started, release = threading.Event(), threading.Event()

    def blocked(_):
        started.set()
        if not release.wait(3):
            raise RuntimeError("test provider was not released")
        return _response(total=13)

    client, _ = fake_client(blocked)
    try:
        with collect_llm_usage("cancelled") as meter:
            task = asyncio.create_task(client.chat([]))
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        pending = meter.snapshot()
        assert pending["in_flight_attempts"] == pending["unknown_attempts"] == 1
        assert pending["complete"] is False
        release.set()
        async with asyncio.timeout(2):
            while meter.snapshot()["in_flight_attempts"]:
                await asyncio.sleep(0.01)
        finished = meter.snapshot()
        assert finished["known_total_tokens"] == 13
        assert finished["unknown_attempts"] == 0
        assert finished["complete"] is True
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_dispatch_waiting_global_slot_cannot_look_complete(fake_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class BlockedSlot:
        def __enter__(self):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test slot was not released")

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(llm, "_GLOBAL_MODEL_SLOTS", BlockedSlot())
    client, calls = fake_client([_response(total=17)])
    try:
        with collect_llm_usage("queued-cancel") as meter:
            task = asyncio.create_task(client.chat([]))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        pending = meter.snapshot()
        assert calls == []  # The SDK has not run; this is not yet a provider attempt.
        assert pending["attempt_count"] == pending["in_flight_attempts"] == 0
        assert pending["pending_dispatches"] == 1
        assert pending["complete"] is False
        release.set()
        async with asyncio.timeout(2):
            while meter.snapshot()["pending_dispatches"]:
                await asyncio.sleep(0.01)
        finished = meter.snapshot()
        assert len(calls) == finished["attempt_count"] == 1
        assert finished["known_total_tokens"] == 17
        assert finished["complete"] is True
    finally:
        release.set()


@pytest.mark.asyncio
async def test_overloaded_dispatch_is_not_recorded_as_provider_attempt(fake_client):
    started, release = threading.Event(), threading.Event()

    def blocked(_):
        started.set()
        if not release.wait(3):
            raise RuntimeError("test provider was not released")
        return _response(total=3)

    client, calls = fake_client(blocked)
    client._dispatcher = llm._dispatcher_for(client._dispatcher.tenant_id, 1, 1)
    try:
        with collect_llm_usage("overloaded") as meter:
            first = asyncio.create_task(client.chat([]))
            assert await asyncio.to_thread(started.wait, 2)
            with pytest.raises(llm.LLMOverloadedError):
                await client.chat([])
            assert meter.snapshot()["pending_dispatches"] == 1
            release.set()
            assert await first == "ok"
        assert len(calls) == meter.snapshot()["attempt_count"] == 1
        assert meter.snapshot()["pending_dispatches"] == 0
        assert meter.snapshot()["complete"] is True
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, -1, 1.5])
async def test_invalid_provider_total_is_unknown_before_int_conversion(fake_client, invalid):
    client, _ = fake_client([_response(total=invalid)])
    with collect_llm_usage("invalid-provider-total") as meter:
        await client.chat([])
    assert meter.snapshot()["known_total_tokens"] == 0
    assert meter.snapshot()["unknown_attempts"] == 1


def test_usage_settles_after_cancelled_waiters_event_loop_has_closed(fake_client):
    started, release = threading.Event(), threading.Event()

    def blocked(_):
        started.set()
        if not release.wait(3):
            raise RuntimeError("test provider was not released")
        return _response(total=19)

    client, _ = fake_client(blocked)

    async def cancel_waiter():
        task = asyncio.create_task(client.chat([]))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        with collect_llm_usage("closed-loop") as meter:
            asyncio.run(cancel_waiter())
        assert meter.snapshot()["pending_dispatches"] == 1
        assert meter.snapshot()["complete"] is False
    finally:
        release.set()
        client._dispatcher.shutdown()
    finished = meter.snapshot()
    assert finished["known_total_tokens"] == 19
    assert finished["pending_dispatches"] == finished["in_flight_attempts"] == 0
    assert finished["complete"] is True


def test_nested_scopes_attribute_to_nearest_and_restore_outer():
    with collect_llm_usage("outer") as outer:
        with collect_llm_usage("inner") as inner:
            with provider_attempt("inner-model") as attempt:
                attempt.record_usage({"total_tokens": 3})
        with provider_attempt("outer-model") as attempt:
            attempt.record_usage({"total_tokens": 5})
    assert inner.snapshot()["known_total_tokens"] == 3
    assert outer.snapshot()["known_total_tokens"] == 5
    assert inner.snapshot()["attempt_count"] == outer.snapshot()["attempt_count"] == 1
    with provider_attempt("outside") as attempt:
        assert attempt is None


def test_bounded_ledger_keeps_totals_even_when_entries_are_omitted(monkeypatch):
    monkeypatch.setattr(llm_usage, "_MAX_ATTEMPT_RECORDS", 2)
    with collect_llm_usage("bounded-records") as meter:
        for _ in range(4):
            with provider_attempt("model") as attempt:
                attempt.record_usage({"total_tokens": 3, "messages": "do-not-retain", "secret": "private"})
    result = meter.snapshot()
    assert result["known_total_tokens"] == 12
    assert result["attempt_count"] == 4
    assert len(result["attempts"]) == result["omitted_attempts"] == 2
    assert result["truncated"] is True
    assert result["complete"] is True
    assert "do-not-retain" not in json.dumps(result)


@pytest.mark.parametrize("invalid", [True, -1, 1.5, float("inf"), float("nan"), "7"])
def test_invalid_counter_never_becomes_known_zero(invalid):
    with collect_llm_usage("invalid-usage") as meter:
        with provider_attempt("model") as attempt:
            attempt.record_usage({"total_tokens": invalid})
    result = meter.snapshot()
    assert result["known_total_tokens"] == 0
    assert result["unknown_attempts"] == 1
    assert result["complete"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "7"])
def test_invalid_budgets_are_rejected(invalid):
    with pytest.raises(ValueError, match="budget_tokens"):
        with collect_llm_usage("invalid-budget", budget_tokens=invalid):
            pass
