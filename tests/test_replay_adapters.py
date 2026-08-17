from __future__ import annotations

import httpx

from teamEvolver.integrations.agent_protocol import (
    REPLAY_REQUEST_SCHEMA_V1,
    REPLAY_RESULT_SCHEMA_V1,
)
from teamEvolver.integrations.replay_adapters import (
    HttpReplayAdapter,
    LegacyAgentsHubHttpAdapter,
    legacy_branch_projection,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


def _request(branch: str = "candidate") -> dict:
    return {
        "schema_version": REPLAY_REQUEST_SCHEMA_V1,
        "protocol_version": "1.0",
        "request_id": f"request-{branch}",
        "job_id": "job-1",
        "case_index": 1,
        "branch": branch,
        "target_skill_name": "demo",
        "skill": {"name": "demo"},
        "source_session": {"runtime": {"type": "demo"}},
        "case": {"query": "perform task", "checklist": []},
        "context_snapshot": {"snapshot_id": "snapshot-1"},
        "execution_manifest": {"model": "model-a"},
        "limits": {"timeout_seconds": 120, "max_interactions": 4},
    }


def _v1_result(request: dict, **overrides) -> dict:
    result = {
        "schema_version": REPLAY_RESULT_SCHEMA_V1,
        "protocol_version": "1.0",
        "request_id": request["request_id"],
        "branch": request["branch"],
        "runtime": {"type": "demo"},
        "status": "succeeded",
        "metrics": {
            "interaction_turns": 2,
            "tool_call_count": 1,
            "total_tokens": 120,
        },
        "output": {"final_response": "done"},
        "trace": {"messages": [], "events": [], "interactions": []},
        "artifacts": [],
    }
    result.update(overrides)
    return result


def test_http_adapter_uses_exact_endpoint_and_caller_timeout() -> None:
    request = _request()
    captured = {}

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response(_v1_result(request))

    adapter = HttpReplayAdapter(
        endpoint="https://agent.example/replay/v1",
        runtime_type="demo",
        post=post,
    )
    result = adapter.execute_branch(request)

    assert result["status"] == "succeeded"
    assert captured["url"] == "https://agent.example/replay/v1"
    assert captured["timeout"] == 150
    assert captured["json"]["limits"]["timeout_seconds"] == 120


def test_http_adapter_fails_closed_on_missing_metrics() -> None:
    request = _request()
    adapter = HttpReplayAdapter(
        endpoint="https://agent.example/replay/v1",
        runtime_type="demo",
        post=lambda *_args, **_kwargs: _Response(
            _v1_result(
                request,
                metrics={"interaction_turns": 1, "tool_call_count": 0},
            )
        ),
    )

    result = adapter.execute_branch(request)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"
    assert "total_tokens" in result["error"]["message"]


def test_http_adapter_rejects_request_id_or_branch_mismatch() -> None:
    request = _request()
    adapter = HttpReplayAdapter(
        endpoint="https://agent.example/replay/v1",
        runtime_type="demo",
        post=lambda *_args, **_kwargs: _Response(
            _v1_result(request, request_id="other")
        ),
    )

    result = adapter.execute_branch(request)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"


def test_legacy_agentshub_adapter_converts_to_v1_and_projection() -> None:
    request = _request()
    captured = {}

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response(
            {
                "branch": "candidate",
                "runtime": "agentshub",
                "ok": True,
                "interaction_turns": 3,
                "tool_call_count": 2,
                "total_tokens": 300,
                "final_response": "complete",
                "messages": [],
            }
        )

    adapter = LegacyAgentsHubHttpAdapter(
        endpoint="http://127.0.0.1:5173/api/internal/team-evolver/replay",
        runtime_type="agentshub",
        post=post,
    )
    result = adapter.execute_branch(request)
    projected = legacy_branch_projection(result)

    assert result["schema_version"] == REPLAY_RESULT_SCHEMA_V1
    assert projected["ok"] is True
    assert projected["interaction_turns"] == 3
    assert captured["json"]["timeout_seconds"] == 120
    assert captured["json"]["instruction"] == "perform task"


def test_http_adapter_returns_timeout_without_retrying() -> None:
    request = _request()
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("deadline")

    result = HttpReplayAdapter(
        endpoint="https://agent.example/replay/v1",
        runtime_type="demo",
        post=post,
    ).execute_branch(request)

    assert calls == 1
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIMEOUT"
    assert result["error"]["retryable"] is False
