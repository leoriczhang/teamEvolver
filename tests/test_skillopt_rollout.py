from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.integrations import skillopt_rollout


class FakeHub:
    def __init__(self, bundle):
        self.bundle = bundle

    def read_version_bundle(self, _name, _version):
        return dict(self.bundle)


class FakeService:
    def __init__(self, bundle):
        self.hub = FakeHub(bundle)
        self.deliveries = []

    def record_delivery(self, event_id, consumer_id, delivery):
        self.deliveries.append((event_id, consumer_id, dict(delivery)))


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = {"success": True} if body is None else body
        self.text = text

    def json(self):
        return self._body


class FakeAsyncClient:
    responses = []
    calls = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, json):
        self.calls.append((url, json))
        return self.responses.pop(0)


def _event():
    return {
        "event_id": "event-1",
        "status": "pending",
        "skills": [{"name": "alpha", "version": 1}],
        "deliveries": {},
    }


def _config():
    return SimpleNamespace(
        skillopt_rollout_endpoint="http://upclaw.test",
        skillopt_rollout_workspace_prefix="rollout-",
    )


def test_invalid_frontmatter_dead_letters_without_http(monkeypatch) -> None:
    service = FakeService(
        {
            "SKILL.md": (
                b"---\nname: alpha\ndescription: folded\n"
                b"  continuation\n---\n\nbody\n"
            )
        }
    )
    FakeAsyncClient.calls = []
    monkeypatch.setattr(skillopt_rollout.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        skillopt_rollout.deliver_rollout(_config(), service, _event())
    )

    assert result["status"] == "dead_letter"
    assert result["failure_kind"] == "payload_validation"
    assert FakeAsyncClient.calls == []
    assert service.deliveries[-1][2]["retryable"] is False


def test_http_422_is_non_retryable_and_captures_body(monkeypatch) -> None:
    service = FakeService(
        {"SKILL.md": b"---\nname: alpha\ndescription: valid\n---\n\nbody\n"}
    )
    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = [
        FakeResponse(422, {"error": "bad"}, '{"error":"bad"}')
    ]
    monkeypatch.setattr(skillopt_rollout.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        skillopt_rollout.deliver_rollout(_config(), service, _event())
    )

    assert result["status"] == "dead_letter"
    assert service.deliveries[-1][2]["http_status"] == 422
    assert service.deliveries[-1][2]["response_excerpt"] == '{"error":"bad"}'


def test_http_500_remains_retryable(monkeypatch) -> None:
    service = FakeService(
        {"SKILL.md": b"---\nname: alpha\ndescription: valid\n---\n\nbody\n"}
    )
    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = [
        FakeResponse(500, {"error": "temporary"}, "temporary")
    ]
    monkeypatch.setattr(skillopt_rollout.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        skillopt_rollout.deliver_rollout(_config(), service, _event())
    )

    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert service.deliveries[-1][2]["status"] == "pending"


def test_valid_bundle_is_delivered(monkeypatch) -> None:
    service = FakeService(
        {
            "SKILL.md": b"---\nname: alpha\ndescription: valid\n---\n\nbody\n",
            "scripts/run.py": b"print('ok')\n",
        }
    )
    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = [
        FakeResponse(200, {"success": True, "filePath": "a"}),
        FakeResponse(200, {"success": True, "filePath": "b"}),
    ]
    monkeypatch.setattr(skillopt_rollout.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        skillopt_rollout.deliver_rollout(_config(), service, _event())
    )

    assert result["status"] == "synced"
    assert result["files_written"] == 2
    assert len(FakeAsyncClient.calls) == 2
