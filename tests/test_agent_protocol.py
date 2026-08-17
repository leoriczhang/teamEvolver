from __future__ import annotations

import pytest

from teamEvolver.integrations.agent_protocol import (
    AgentProtocolError,
    CAP_CONTEXT_WORKSPACE,
    CAP_REPLAY_BRANCH,
    CAP_SESSION_INGEST,
    normalize_registration,
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
