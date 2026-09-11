from __future__ import annotations

import pytest

from teamEvolver.integrations.agent_protocol import (
    AgentProtocolError,
    CAP_CONTEXT_WORKSPACE,
    CAP_REPLAY_BRANCH,
    CAP_SESSION_INGEST,
    normalize_registration,
    normalize_replay_turn_request,
    normalize_replay_turn_result,
    normalize_session_envelope,
    validate_endpoint_url,
)


def test_v1_registration_normalizes_capabilities() -> None:
    result = normalize_registration(
        {
            "schema_version": "teamevolver.agent-registration.v1",
            "protocol_version": "1.0",
            "agent_id": "demo:tenant-a",
            "runtime_type": "demo",
            "capabilities": {
                "session.ingest.v1": {"max_body_bytes": 1024},
                "replay.branch.v1": {"transport": "http"},
                "context.workspace.v1": {},
            },
        }
    )

    assert result["compatibility"] == "compatible"
    assert result["capability_ids"] == [
        CAP_CONTEXT_WORKSPACE,
        CAP_REPLAY_BRANCH,
        CAP_SESSION_INGEST,
    ]
    assert result["capability_details"][CAP_REPLAY_BRANCH] == {
        "transport": "http"
    }


def test_legacy_registration_preserves_legacy_capabilities() -> None:
    result = normalize_registration(
        {
            "agent_id": "agentshub:tenant-a",
            "runtime_type": "agentshub",
            "capabilities": ["session_ingest", "true_replay"],
        }
    )

    assert result["compatibility"] == "legacy"
    assert result["capabilities"] == ["session_ingest", "true_replay"]
    assert result["capability_ids"] == [
        CAP_REPLAY_BRANCH,
        CAP_SESSION_INGEST,
    ]


def test_registration_rejects_unknown_major_and_v1_storage() -> None:
    with pytest.raises(AgentProtocolError, match="PROTOCOL_VERSION_UNSUPPORTED"):
        normalize_registration(
            {
                "protocol_version": "2.0",
                "agent_id": "demo:tenant-a",
                "runtime_type": "demo",
            }
        )
    with pytest.raises(AgentProtocolError, match="cannot carry storage credentials"):
        normalize_registration(
            {
                "protocol_version": "1.0",
                "agent_id": "demo:tenant-a",
                "runtime_type": "demo",
                "storage": {"team_api_key": "secret"},
            }
        )


def test_v1_endpoint_validation_rejects_credentials_and_metadata() -> None:
    assert validate_endpoint_url("http://127.0.0.1:9000/replay") == (
        "http://127.0.0.1:9000/replay"
    )
    with pytest.raises(AgentProtocolError, match="credentials"):
        validate_endpoint_url("https://user:pass@example.com/replay")
    with pytest.raises(AgentProtocolError, match="forbidden"):
        validate_endpoint_url("http://169.254.169.254/latest")


def test_v1_session_requires_identity_and_context_usage_types() -> None:
    with pytest.raises(AgentProtocolError, match="runtime.integration_id"):
        normalize_session_envelope(
            {
                "schema_version": "teamevolver.agent-session.v1",
                "session_id": "session-1",
                "runtime": {"type": "demo"},
                "turns": [{"prompt_text": "hello"}],
            }
        )
    with pytest.raises(AgentProtocolError, match="memory_refs"):
        normalize_session_envelope(
            {
                "schema_version": "teamevolver.agent-session.v1",
                "session_id": "session-1",
                "runtime": {
                    "type": "demo",
                    "integration_id": "demo:tenant-a",
                },
                "turns": [
                    {
                        "prompt_text": "hello",
                        "context_usage": {"memory_refs": "forged"},
                    }
                ],
            }
        )


def test_v1_session_normalizes_runtime_protocol() -> None:
    result = normalize_session_envelope(
        {
            "schema_version": "teamevolver.agent-session.v1",
            "session_id": "session-1",
            "runtime": {
                "type": "Demo",
                "integration_id": "demo:tenant-a",
            },
            "turns": [
                {
                    "prompt_text": "hello",
                    "context_usage": {"skill_refs": []},
                }
            ],
        }
    )

    assert result["protocol_compatibility"] == "compatible"
    assert result["runtime"]["type"] == "demo"
    assert result["runtime"]["protocol_version"] == "1.0"


def _turn_request(**overrides) -> dict:
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


def test_replay_turn_request_normalizes_and_enforces_required_fields() -> None:
    result = normalize_replay_turn_request(
        _turn_request(
            context_snapshot={"snapshot_id": "s1"},
            skill={"name": "demo", "content": "# demo"},
        )
    )

    assert result["turn_num"] == 1
    assert result["limits"]["turn_timeout_seconds"] == 120
    assert result["skill"]["name"] == "demo"

    with pytest.raises(AgentProtocolError):
        normalize_replay_turn_request(_turn_request(prompt="  "))
    with pytest.raises(AgentProtocolError):
        normalize_replay_turn_request(_turn_request(turn_num=0))
    with pytest.raises(AgentProtocolError):
        normalize_replay_turn_request(_turn_request(branch="eval"))
    with pytest.raises(AgentProtocolError):
        normalize_replay_turn_request(
            _turn_request(limits={"turn_timeout_seconds": 10})
        )


def test_replay_turn_result_requires_metrics_on_success() -> None:
    result = normalize_replay_turn_result(
        {
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "request_id": "replay_abc",
            "turn_num": 2,
            "status": "succeeded",
            "final_response": "done",
            "messages": [{"role": "assistant", "content": "step"}],
            "metrics": {"tool_call_count": 2, "total_tokens": 90},
            "artifacts": [{"path": "out.txt", "size": 10}],
        },
        expected_request_id="replay_abc",
        expected_turn_num=2,
    )

    assert result["status"] == "succeeded"
    assert result["metrics"]["total_tokens"] == 90

    # Fail-closed: a succeeded turn without counts is invalid.
    with pytest.raises(AgentProtocolError) as excinfo:
        normalize_replay_turn_result(
            {
                "schema_version": "teamevolver.replay-turn-result.v1",
                "protocol_version": "1.0",
                "request_id": "replay_abc",
                "turn_num": 1,
                "status": "succeeded",
                "metrics": {"tool_call_count": 1},
            },
            expected_request_id="replay_abc",
            expected_turn_num=1,
        )
    assert "total_tokens" in str(excinfo.value)

    with pytest.raises(AgentProtocolError):
        normalize_replay_turn_result(
            {
                "schema_version": "teamevolver.replay-turn-result.v1",
                "protocol_version": "1.0",
                "request_id": "other",
                "turn_num": 1,
                "status": "succeeded",
                "metrics": {"tool_call_count": 1, "total_tokens": 1},
            },
            expected_request_id="replay_abc",
            expected_turn_num=1,
        )


def test_replay_turn_result_defaults_error_for_failures() -> None:
    result = normalize_replay_turn_result(
        {
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "request_id": "replay_abc",
            "turn_num": 1,
            "status": "unsupported",
        },
        expected_request_id="replay_abc",
        expected_turn_num=1,
    )

    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "EXECUTION_FAILED"
    assert result["error"]["retryable"] is False
