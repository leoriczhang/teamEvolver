from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.integrations import dreamcycle as dreamcycle_integration
from teamEvolver.integrations.dreamcycle import (
    build_dreamcycle_env,
    parse_openviking_key,
)
from teamEvolver.proxy import ProxyServer


def _key(account: str, user: str) -> str:
    encode = lambda value: base64.b64encode(value.encode()).decode().rstrip("=")
    return f"{encode(account)}.{encode(user)}.secret"


def test_defaults_enable_full_evolution_loop(tmp_path) -> None:
    config = ConfigStore(tmp_path / "missing.yaml").to_config()

    assert config.proxy_port == 52010
    assert config.use_skills is True
    assert config.sharing_enabled is True
    assert config.sharing_backend == "viking"
    assert config.sharing_auto_pull_on_start is True
    assert config.sharing_viking_root_prefix == "team-skill-evolver"
    assert config.validation_enabled is True
    assert config.dreamcycle_enabled is True
    assert config.dreamcycle_auto_start is True


def test_dreamcycle_uses_team_key_target_and_personal_key_sources() -> None:
    team_key = _key("account-a", "team-space")
    personal_a = _key("account-a", "alice")
    personal_b = _key("account-a", "bob")
    config = TeamEvolverConfig(
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_team_api_key=team_key,
        sharing_viking_personal_api_key=personal_a,
        sharing_viking_personal_api_keys=[personal_a, personal_b],
        llm_api_key="llm-key",
        llm_model_id="model-a",
    )

    assert parse_openviking_key(team_key) == ("account-a", "team-space")
    env, missing = build_dreamcycle_env(config)

    assert missing == []
    assert env["OPENVIKING_API_KEY"] == team_key
    assert env["OPENVIKING_TEAM_USER"] == "team-space"
    assert env["OPENVIKING_ACCOUNT"] == "account-a"
    assert json.loads(env["OPENVIKING_SOURCE_API_KEYS"]) == [
        personal_a,
        personal_b,
    ]
    assert env["DREAMCYCLE_LLM_API_KEY"] == "llm-key"


def test_unified_dreamcycle_routes_are_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    monkeypatch.setattr(dreamcycle_integration, "_STATE_DIR", tmp_path)
    server = ProxyServer(
        TeamEvolverConfig(
            dreamcycle_enabled=False,
            dreamcycle_auto_start=False,
            sharing_enabled=False,
            sharing_skill_reload_mode="off",
        )
    )

    with TestClient(server.app) as client:
        status = client.get("/trigger-dreamcycle/status")
        trigger = client.post("/trigger-dreamcycle")

    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert trigger.status_code == 202
    assert trigger.json()["status"] == "disabled"


def test_agentshub_config_sync_merges_personal_sources(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    monkeypatch.setattr(
        dreamcycle_integration,
        "_STATE_DIR",
        tmp_path / "dreamcycle-state",
    )
    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file)
    data = store.load()
    existing = _key("acct", "alice")
    data["sharing"]["viking_personal_api_keys"] = [existing]
    store.save(data)
    server = ProxyServer(
        TeamEvolverConfig(
            _config_file=str(config_file),
            dreamcycle_enabled=False,
            dreamcycle_auto_start=False,
            sharing_enabled=False,
            sharing_skill_reload_mode="off",
        )
    )

    async def fake_reload(config):
        server.config = config

    monkeypatch.setattr(server, "_reload_openviking_integrations", fake_reload)
    team_key = _key("acct", "team-space")
    personal_b = _key("acct", "bob")
    with TestClient(server.app) as client:
        response = client.post(
            "/internal/agentshub/openviking-config",
            json={
                "endpoint": "https://openviking.example",
                "account": "ignored-account",
                "personal_api_key": personal_b,
                "team_api_key": team_key,
            },
        )

    assert response.status_code == 200
    assert response.json()["team_user"] == "team-space"
    assert response.json()["personal_source_count"] == 2
    saved = store.load()["sharing"]
    assert saved["viking_team_api_key"] == team_key
    assert saved["viking_account"] == "acct"
    assert saved["viking_user"] == "team-space"
    assert saved["viking_personal_api_keys"] == [existing, personal_b]
