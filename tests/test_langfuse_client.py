from __future__ import annotations

import httpx
import pytest

from session_ingestion.adapters._shared import langfuse_client
from session_ingestion.adapters._shared.langfuse_client import (
    LangfuseClient,
    LangfuseError,
)


def _client(handler) -> LangfuseClient:
    client = LangfuseClient(
        "https://langfuse.test",
        "public-key",
        "secret-key",
    )
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_transient_server_errors_retry_until_success(monkeypatch) -> None:
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"data": [], "meta": {}})

    monkeypatch.setattr(langfuse_client.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(langfuse_client.time, "sleep", delays.append)
    client = _client(handler)
    try:
        result = client.health()
    finally:
        client.close()

    assert result["ok"] is True
    assert len(calls) == 3
    assert delays == [0.5, 1.0]
    assert client.retry_count == 2


def test_network_errors_are_retried(monkeypatch) -> None:
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"data": [], "meta": {}})

    monkeypatch.setattr(langfuse_client.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(langfuse_client.time, "sleep", delays.append)
    client = _client(handler)
    try:
        assert client.health()["ok"] is True
    finally:
        client.close()

    assert len(calls) == 2
    assert delays == [0.5]
    assert client.retry_count == 1


def test_retry_after_is_preferred_and_capped(monkeypatch) -> None:
    calls = []
    delays = []

    def handler(_request):
        calls.append(True)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json={"data": [], "meta": {}})

    monkeypatch.setattr(langfuse_client.time, "sleep", delays.append)
    client = _client(handler)
    try:
        assert client.health()["ok"] is True
    finally:
        client.close()

    assert delays == [30.0]
    assert client.retry_count == 1


def test_authentication_error_is_not_retried(monkeypatch) -> None:
    calls = []
    delays = []

    def handler(_request):
        calls.append(True)
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(langfuse_client.time, "sleep", delays.append)
    client = _client(handler)
    try:
        with pytest.raises(LangfuseError, match="authentication failed"):
            client.health()
    finally:
        client.close()

    assert len(calls) == 1
    assert delays == []
    assert client.retry_count == 0


def test_invalid_json_is_not_retried(monkeypatch) -> None:
    calls = []
    delays = []

    def handler(_request):
        calls.append(True)
        return httpx.Response(200, text="not-json")

    monkeypatch.setattr(langfuse_client.time, "sleep", delays.append)
    client = _client(handler)
    try:
        with pytest.raises(LangfuseError, match="invalid JSON"):
            client.health()
    finally:
        client.close()

    assert len(calls) == 1
    assert delays == []
