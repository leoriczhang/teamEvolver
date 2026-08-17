from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from teamEvolver.integrations.agent_protocol import AgentProtocolError
from teamEvolver.integrations.agent_registry import (
    issue_agent_access_token,
    list_agents,
    public_agent_record,
    register_agent,
    resolve_runtime_agent,
    verify_agent_access_token,
)


def _config(tmp_path):
    return SimpleNamespace(users_registry_path=str(tmp_path / "users.json"))


def _registration(agent_id: str = "demo:tenant-a") -> dict:
    return {
        "schema_version": "teamevolver.agent-registration.v1",
        "protocol_version": "1.0",
        "agent_id": agent_id,
        "runtime_type": "demo",
        "runtime_version": "3.2",
        "capabilities": [
            "session.ingest.v1",
            "context.workspace.v1",
            "memory.personal.write.v1",
            "replay.branch.v1",
        ],
        "capability_details": {
            "replay.branch.v1": {
                "transport": "http",
                "max_interactions": 20,
                "api_key": "must-not-persist",
            }
        },
        "endpoints": {"replay_url": "http://127.0.0.1:9000/replay"},
        "metadata": {"region": "local", "secret": "must-not-persist"},
    }


def test_register_agent_persists_v1_without_secrets(tmp_path) -> None:
    config = _config(tmp_path)

    record = register_agent(config, _registration())

    assert record["protocol_version"] == "1.0"
    assert record["compatibility"] == "compatible"
    assert "api_key" not in record["capability_details"]["replay.branch.v1"]
    assert "secret" not in record["metadata"]
    registry = tmp_path / "agents.json"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    raw = registry.read_text(encoding="utf-8")
    assert "must-not-persist" not in raw


def test_failed_v1_registration_does_not_leave_record(tmp_path) -> None:
    config = _config(tmp_path)
    payload = _registration()
    payload["storage"] = {"team_api_key": "secret"}

    with pytest.raises(AgentProtocolError):
        register_agent(config, payload)

    assert list_agents(config) == []


def test_access_token_is_returned_once_hashed_and_scoped(tmp_path) -> None:
    config = _config(tmp_path)
    register_agent(config, _registration())

    record, token = issue_agent_access_token(
        config,
        agent_id="demo:tenant-a",
    )
    repeated, repeated_token = issue_agent_access_token(
        config,
        agent_id="demo:tenant-a",
    )

    assert token.startswith("tev1_")
    assert repeated_token == ""
    assert token not in json.dumps(list_agents(config))
    assert verify_agent_access_token(
        config,
        token,
        required_scope="context.resolve",
    )["agent_id"] == "demo:tenant-a"
    assert verify_agent_access_token(
        config,
        token,
        required_scope="context.remember",
    )["agent_id"] == "demo:tenant-a"
    public = public_agent_record(record)
    assert public["access_token_configured"] is True
    assert "access_auth" not in public
    assert repeated["access_auth"]["token_sha256"]


def test_token_rotation_invalidates_previous_token(tmp_path) -> None:
    config = _config(tmp_path)
    register_agent(config, _registration())
    _record, first = issue_agent_access_token(config, agent_id="demo:tenant-a")
    _record, second = issue_agent_access_token(
        config,
        agent_id="demo:tenant-a",
        rotate=True,
    )

    assert first != second
    assert verify_agent_access_token(config, first) is None
    assert verify_agent_access_token(config, second) is not None


def test_exact_integration_resolution_does_not_cross_tenant(tmp_path) -> None:
    config = _config(tmp_path)
    register_agent(config, _registration("demo:tenant-a"))
    register_agent(config, _registration("demo:tenant-b"))

    assert resolve_runtime_agent(
        config,
        runtime_type="demo",
        agent_id="demo:tenant-a",
        allow_runtime_fallback=False,
    )["agent_id"] == "demo:tenant-a"
    assert resolve_runtime_agent(
        config,
        runtime_type="demo",
        agent_id="demo:missing",
        allow_runtime_fallback=False,
    ) is None
