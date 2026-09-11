from __future__ import annotations

import httpx

from teamEvolver.integrations.agent_protocol import (
    REPLAY_REQUEST_SCHEMA_V1,
    REPLAY_RESULT_SCHEMA_V1,
    REPLAY_TURN_REQUEST_SCHEMA_V1,
    REPLAY_TURN_RESULT_SCHEMA_V1,
)
from teamEvolver.integrations.replay_adapters import (
    HttpReplayAdapter,
    LegacyAgentsHubHttpAdapter,
    MappedHttpAdapter,
    TurnBasedReplayAdapter,
    extract_path,
    legacy_branch_projection,
    render_template,
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


def _turn_request(**overrides) -> dict:
    request = {
        "schema_version": REPLAY_TURN_REQUEST_SCHEMA_V1,
        "protocol_version": "1.0",
        "request_id": "replay_abc",
        "turn_num": 1,
        "branch": "candidate",
        "prompt": "do the task",
        "limits": {"turn_timeout_seconds": 120},
    }
    request.update(overrides)
    return request


def _turn_result(request: dict, **overrides) -> dict:
    result = {
        "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
        "protocol_version": "1.0",
        "request_id": request["request_id"],
        "turn_num": request["turn_num"],
        "runtime": {"type": "demo"},
        "status": "succeeded",
        "final_response": "step done",
        "messages": [{"role": "assistant", "content": "step"}],
        "metrics": {"tool_call_count": 1, "total_tokens": 40},
        "artifacts": [],
    }
    result.update(overrides)
    return result


def test_turn_adapter_posts_one_turn_with_caller_timeout() -> None:
    request = _turn_request()
    captured = {}

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response(_turn_result(request))

    result = TurnBasedReplayAdapter(
        endpoint="https://agent.example/turn",
        runtime_type="demo",
        post=post,
    ).call_turn(request)

    assert result["status"] == "succeeded"
    assert result["metrics"]["tool_call_count"] == 1
    assert captured["url"] == "https://agent.example/turn"
    assert captured["timeout"] == 150
    assert captured["json"]["limits"]["turn_timeout_seconds"] == 120
    assert captured["json"]["prompt"] == "do the task"


def test_turn_adapter_fails_closed_on_missing_turn_metrics() -> None:
    request = _turn_request()
    adapter = TurnBasedReplayAdapter(
        endpoint="https://agent.example/turn",
        runtime_type="demo",
        post=lambda *_args, **_kwargs: _Response(
            _turn_result(request, metrics={"tool_call_count": 1})
        ),
    )

    result = adapter.call_turn(request)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"
    assert "total_tokens" in result["error"]["message"]


def test_turn_adapter_rejects_turn_num_mismatch() -> None:
    request = _turn_request()
    adapter = TurnBasedReplayAdapter(
        endpoint="https://agent.example/turn",
        runtime_type="demo",
        post=lambda *_args, **_kwargs: _Response(
            _turn_result(request, turn_num=7)
        ),
    )

    result = adapter.call_turn(request)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"
    assert "turn_num" in result["error"]["message"]


def test_turn_adapter_returns_timeout_without_retrying() -> None:
    request = _turn_request()
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("deadline")

    result = TurnBasedReplayAdapter(
        endpoint="https://agent.example/turn",
        runtime_type="demo",
        post=post,
    ).call_turn(request)

    assert calls == 1
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIMEOUT"
    assert result["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# MappedHttpAdapter — zero agent-side awareness over a plain HTTP endpoint.
# ---------------------------------------------------------------------------


def test_render_template_whole_value_and_interpolation() -> None:
    values = {
        "prompt": "do it",
        "history": [{"turn_num": 1, "prompt": "p", "response": "r"}],
        "request_id": "replay_x",
        "turn_num": 2,
    }
    template = {
        "message": "{{prompt}}",
        "note": "turn {{turn_num}} of session {{request_id}}",
        "history": "{{history}}",
        "nested": {"items": ["{{prompt}}"], "n": 3},
    }
    body = render_template(template, values)

    assert body["message"] == "do it"
    assert body["note"] == "turn 2 of session replay_x"
    assert body["history"] == values["history"]
    assert body["nested"] == {"items": ["do it"], "n": 3}


def test_extract_path_dotted_and_index() -> None:
    data = {"usage": {"total_tokens": 42}, "items": [{"name": "a"}]}
    assert extract_path(data, "usage.total_tokens") == 42
    assert extract_path(data, "items.0.name") == "a"
    assert extract_path(data, "missing.path") is None


def _mapped_adapter(post) -> MappedHttpAdapter:
    return MappedHttpAdapter(
        endpoint="https://agent.example/chat",
        runtime_type="demo",
        request_template={
            "message": "{{prompt}}",
            "session_id": "{{request_id}}",
            "turn": "{{turn_num}}",
            "history": "{{history}}",
            "skill": "{{skill_content}}",
        },
        response_mapping={
            "final_response": "answer",
            "messages": "messages",
            "tool_call_count": "usage.tool_calls",
            "total_tokens": "usage.total_tokens",
            "status": "status",
            "error.message": "error.message",
        },
        post=post,
    )


def _mapped_turn_request(**overrides) -> dict:
    request = _turn_request()
    request["history"] = [
        {"turn_num": 1, "prompt": "do the task", "response": "resp-1"}
    ]
    request["skill"] = {"name": "demo", "content": "# demo"}
    request.update(overrides)
    return request


def test_mapped_adapter_renders_customer_request_and_extracts_response() -> None:
    request = _mapped_turn_request()
    captured = {}

    def post(url, **kwargs):
        captured.update({"url": url, "json": kwargs["json"], "timeout": kwargs["timeout"]})
        return _Response(
            {
                "answer": "done",
                "messages": [{"role": "assistant", "content": "step"}],
                "usage": {"tool_calls": 3, "total_tokens": 120},
            }
        )

    result = _mapped_adapter(post).call_turn(request)

    assert result["status"] == "succeeded"
    assert result["final_response"] == "done"
    assert result["metrics"] == {"tool_call_count": 3, "total_tokens": 120}
    assert result["metrics_incomplete"] is False
    # Template rendered into the customer's own request shape.
    assert captured["url"] == "https://agent.example/chat"
    assert captured["json"]["message"] == "do the task"
    assert captured["json"]["session_id"] == "replay_abc"
    assert captured["json"]["turn"] == 1
    assert captured["json"]["history"] == request["history"]
    assert captured["json"]["skill"] == "# demo"
    assert captured["timeout"] == 150


def test_mapped_adapter_missing_metrics_degrade_to_zero() -> None:
    result = _mapped_adapter(
        lambda *_args, **_kwargs: _Response({"answer": "done", "messages": []})
    ).call_turn(_mapped_turn_request())

    assert result["status"] == "succeeded"
    assert result["metrics"] == {"tool_call_count": 0, "total_tokens": 0}
    assert result["metrics_incomplete"] is True


def test_mapped_adapter_maps_agent_status_and_unsupported() -> None:
    failed = _mapped_adapter(
        lambda *_args, **_kwargs: _Response(
            {"status": "error", "error": {"message": "model overloaded"}}
        )
    ).call_turn(_mapped_turn_request())
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "EXECUTION_FAILED"
    assert "model overloaded" in failed["error"]["message"]

    unsupported = _mapped_adapter(
        lambda *_args, **_kwargs: _Response(
            {"status": "unsupported", "error": {"message": "needs live network"}}
        )
    ).call_turn(_mapped_turn_request())
    assert unsupported["status"] == "unsupported"
    assert unsupported["error"]["code"] == "REPLAY_EXTERNAL_TOOL_UNSUPPORTED"
