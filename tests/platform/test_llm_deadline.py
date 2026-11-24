"""A model request shares one budget across queueing, retries and HTTP."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from teamEvolver import llm


@pytest.fixture
def model(monkeypatch):
    client = llm.AsyncLLMClient(api_key="test", tenant_id="deadline-test", max_concurrency=1)
    yield client
    client._client.close()
    llm._reset_dispatchers_for_testing()


def completion():
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="ok"), finish_reason="stop",
    )], usage=None, id="test")


@pytest.mark.asyncio
async def test_queue_consumes_budget_and_expired_request_never_reaches_http(model, monkeypatch):
    await model._dispatcher.acquire()
    calls = []
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kw: calls.append(kw) or completion())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(model.chat([], total_timeout_seconds=0.05), 0.5)
        assert time.monotonic() - started < 0.3
        assert model._dispatcher._pending_count == 1
        assert not calls
    finally:
        model._dispatcher.release()


@pytest.mark.asyncio
async def test_http_receives_remaining_budget_after_queueing(model, monkeypatch):
    await model._dispatcher.acquire()
    calls = []
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kw: calls.append(kw) or completion())
    task = asyncio.create_task(model.chat([], total_timeout_seconds=1))
    await asyncio.sleep(0.1)
    model._dispatcher.release()
    assert await task == "ok"
    assert "total_timeout_seconds" not in calls[0]
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert 0 < timeout.read < 0.9
    assert timeout.connect <= timeout.read


@pytest.mark.asyncio
async def test_retry_backoff_cannot_restart_total_budget(model, monkeypatch):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("model stalled")

    monkeypatch.setattr(model._client.chat.completions, "create", fail)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(model.chat([], total_timeout_seconds=0.05), 0.5)
    assert time.monotonic() - started < 0.3
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancelled_inflight_deadline_keeps_slot_until_thread_finishes(model, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def blocked(**kwargs):
        started.set()
        release.wait(timeout=2)
        return completion()

    monkeypatch.setattr(model._client.chat.completions, "create", blocked)
    task = asyncio.create_task(model.chat([], total_timeout_seconds=0.1))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(TimeoutError):
            await task
        assert model._dispatcher._active_count == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_dispatcher_does_not_start_http_when_thread_budget_is_expired(model):
    calls = []
    with pytest.raises(TimeoutError):
        await model._dispatcher.call(lambda **kw: calls.append(kw), deadline=time.monotonic() - 1)
    assert calls == []
    assert model._dispatcher._active_count == 0


@pytest.mark.asyncio
async def test_sdk_negotiation_and_stream_fallback_share_remaining_budget(model, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        time.sleep(0.02)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "unsupported max_completion_tokens"}})
        if len(requests) == 2:
            return httpx.Response(400, json={"error": {"message": "Stream must be set to true"}})
        chunk = {
            "id": "stream", "object": "chat.completion.chunk", "created": 0, "model": "test",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
        }
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    with model._client.with_options(http_client=httpx.Client(transport=httpx.MockTransport(handle))) as sdk:
        monkeypatch.setattr(model, "_client_for", lambda **kwargs: sdk)
        assert await model.chat([], total_timeout_seconds=10) == "ok"
    assert len(requests) == 3
    budgets = [request.extensions["timeout"]["read"] for request in requests]
    assert 0 < budgets[2] < budgets[1] < budgets[0] <= 9
    assert all("total_timeout_seconds" not in json.loads(request.content) for request in requests)
    assert json.loads(requests[-1].content)["stream"] is True
