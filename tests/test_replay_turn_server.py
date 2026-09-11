from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "replay_turn_server.py"
)
_spec = importlib.util.spec_from_file_location("replay_turn_server", _SCRIPT)
rts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rts)


def _ok_handler(req: dict) -> dict:
    return {
        "final_response": f"resp-{req['turn_num']}",
        "messages": [{"role": "assistant", "content": req["prompt"]}],
        "metrics": {"tool_call_count": 1, "total_tokens": 10},
    }


def _request(**overrides) -> dict:
    request = {
        "schema_version": "teamevolver.replay-turn-request.v1",
        "protocol_version": "1.0",
        "request_id": "replay_abc",
        "turn_num": 1,
        "branch": "candidate",
        "prompt": "do the task",
        "limits": {"turn_timeout_seconds": 120},
    }
    request.update(overrides)
    return request


def test_turn_success_then_history_is_passed_to_next_turn() -> None:
    sessions: dict = {}
    seen_histories: list = []

    def handler(req: dict) -> dict:
        seen_histories.append(req["history"])
        return _ok_handler(req)

    first = rts.handle_turn_request(_request(), handler, sessions)
    assert first["status"] == "succeeded"
    assert first["schema_version"] == "teamevolver.replay-turn-result.v1"
    assert first["request_id"] == "replay_abc"
    assert seen_histories[0] == []

    second = rts.handle_turn_request(
        _request(turn_num=2, prompt="continue"), handler, sessions
    )
    assert second["status"] == "succeeded"
    assert second["turn_num"] == 2
    assert seen_histories[1] == [
        {
            "turn_num": 1,
            "prompt": "do the task",
            "response": "resp-1",
            "messages": [{"role": "assistant", "content": "do the task"}],
        }
    ]


def test_missing_metrics_fails_closed() -> None:
    def handler(_req: dict) -> dict:
        return {
            "final_response": "done",
            "messages": [],
            "metrics": {"tool_call_count": 1},  # total_tokens missing
        }

    result = rts.handle_turn_request(_request(), handler, {})
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"
    assert "total_tokens" in result["error"]["message"]


def test_unsupported_maps_to_fail_closed_code() -> None:
    def handler(_req: dict) -> dict:
        raise rts.ReplayUnsupportedError("external tool call cannot be replayed")

    result = rts.handle_turn_request(_request(), handler, {})
    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "REPLAY_EXTERNAL_TOOL_UNSUPPORTED"


def test_invalid_payload_rejected() -> None:
    result = rts.handle_turn_request(_request(prompt="  "), _ok_handler, {})
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"


def test_turn_two_without_session_is_rejected() -> None:
    result = rts.handle_turn_request(_request(turn_num=2), _ok_handler, {})
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_RESPONSE"
    assert "never executed" in result["error"]["message"]


def test_handler_crash_surfaces_as_execution_failed() -> None:
    def handler(_req: dict) -> dict:
        raise RuntimeError("agent exploded")

    result = rts.handle_turn_request(_request(), handler, {})
    assert result["status"] == "failed"
    assert result["error"]["code"] == "EXECUTION_FAILED"
    assert "agent exploded" in result["error"]["message"]


def test_example_handler_contract_roundtrip() -> None:
    sessions: dict = {}
    result = rts.handle_turn_request(
        _request(skill={"name": "demo", "content": "# demo"}),
        rts.example_handler,
        sessions,
    )
    assert result["status"] == "succeeded"
    assert result["metrics"]["tool_call_count"] == 1


def test_expired_session_is_cleaned_up() -> None:
    sessions = {"replay_old": {"history": [], "updated_at": 0.0}}
    result = rts.handle_turn_request(
        _request(request_id="replay_old", turn_num=2), _ok_handler, sessions
    )
    assert result["status"] == "failed"
    assert "replay_old" not in sessions
