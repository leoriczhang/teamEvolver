"""Platform openviking contracts."""

from __future__ import annotations

import httpx
import pytest

from teamEvolver.openviking import CompileState
from teamEvolver.openviking.client import OpenVikingClient, OpenVikingError
from teamEvolver.openviking.compile import CompileClient


def _stub_client(handler) -> OpenVikingClient:
    client = OpenVikingClient(
        endpoint="https://ov.example",
        api_key="root-key",
        account_id="acct-a",
        user="team",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_compile_submit_sends_identity_and_idempotency_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"task_id": "t1", "status": "running"})

    compile = CompileClient(_stub_client(handler))
    task = await compile.submit(kind="miner.compile", payload={"uri": "x"}, idempotency_key="idem-1")
    assert task.task_id == "t1"
    assert task.state == CompileState.RUNNING
    assert captured["headers"]["x-api-key"] == "root-key"
    assert captured["headers"]["authorization"] == "Bearer root-key"
    assert captured["headers"]["x-openviking-account"] == "acct-a"
    assert captured["headers"]["idempotency-key"] == "idem-1"
    assert captured["url"].endswith("/api/v1/compile")


@pytest.mark.asyncio
async def test_compile_status_normalizes_terminal_state():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"task_id": "t1", "status": "completed", "result": {"ok": 1}})

    compile = CompileClient(_stub_client(handler))
    task = await compile.status("t1")
    assert task.state == CompileState.SUCCEEDED
    assert task.terminal


@pytest.mark.asyncio
async def test_compile_cancel_acknowledgement_preserves_pending_state():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"task_id": "t1", "status": "running"})

    compile = CompileClient(_stub_client(handler))
    task = await compile.cancel("t1")
    assert task.state == CompileState.RUNNING
    assert not task.terminal


@pytest.mark.asyncio
async def test_openviking_maps_4xx_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad uri"}})

    compile = CompileClient(_stub_client(handler))
    with pytest.raises(OpenVikingError, match="HTTP 400"):
        await compile.submit(kind="x", payload={}, idempotency_key="k")


@pytest.mark.asyncio
async def test_openviking_retries_on_transient_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"task_id": "t9", "status": "running"})

    client = OpenVikingClient(
        endpoint="https://ov.example",
        api_key="root-key",
        account_id="a",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.request("GET", "/api/v1/tasks/t9")
    assert result.ok
    assert calls["n"] == 2
