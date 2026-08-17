from __future__ import annotations

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.dreamcycle.jobs.team_overview import TeamOverviewJob
from teamEvolver.integrations import dreamcycle as dreamcycle_integration
from teamEvolver.integrations.dreamcycle import (
    build_dreamcycle_env,
    parse_openviking_key,
)
from teamEvolver.integrations.dreamcycle_runtime import FullDreamCycleSupervisor
from teamEvolver.proxy import ProxyServer


def _key(account: str, user: str) -> str:
    encode = lambda value: base64.b64encode(value.encode()).decode().rstrip("=")
    return f"{encode(account)}.{encode(user)}.secret"


def test_defaults_enable_full_evolution_loop(tmp_path) -> None:
    config = ConfigStore(tmp_path / "missing.yaml").to_config()

    assert config.proxy_port == 52010
    assert config.team_display_name == "Team"
    assert config.use_skills is True
    assert config.sharing_enabled is True
    assert config.sharing_backend == "viking"
    assert config.sharing_auto_pull_on_start is True
    assert config.sharing_viking_root_prefix == "team-skill-evolver"
    assert config.validation_enabled is True
    # Native memory evolution stays opt-in by default.
    assert config.dreamcycle_enabled is False
    assert config.dreamcycle_auto_start is False
    assert config.dreamcycle_interval_seconds == 86400
    assert config.dreamcycle_max_source_items == 100
    assert config.dreamcycle_max_source_chars == 120000


def test_team_display_name_persists_and_environment_wins(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    store.set("team.display_name", "产品团队")

    assert store.to_config().team_display_name == "产品团队"

    monkeypatch.setenv("EVOLVE_TEAM_DISPLAY_NAME", "环境团队")
    assert store.to_config().team_display_name == "环境团队"


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
        sharing_viking_personal_user="alice",
        team_display_name="示例团队",
    )

    assert parse_openviking_key(team_key) == ("account-a", "team-space")
    env, missing = build_dreamcycle_env(config)

    assert missing == []
    assert env["OPENVIKING_API_KEY"] == team_key
    assert env["OPENVIKING_AGENT_ID"] == "team-space"
    assert env["OPENVIKING_ACCOUNT"] == "account-a"
    assert json.loads(env["OPENVIKING_SOURCE_API_KEYS"]) == [
        personal_a,
        personal_b,
    ]
    assert "alice" in json.loads(env["OPENVIKING_SOURCE_USERS"])
    assert env["DREAMCYCLE_LLM_API_KEY"] == "llm-key"
    assert env["DREAMCYCLE_TEAM_NAME"] == "示例团队"

    prompt = TeamOverviewJob(team_name=config.team_display_name).get_system_prompt()
    assert "已知团队名称：示例团队" in prompt
    assert "示例团队概况" in prompt
    assert "{团队名}" not in prompt


def test_dreamcycle_local_root_key_uses_explicit_users() -> None:
    config = TeamEvolverConfig(
        sharing_viking_endpoint="http://localhost:1933",
        sharing_viking_team_api_key="local-root",
        sharing_viking_personal_api_key="local-root",
        sharing_viking_account="default",
        sharing_viking_personal_user="single_evolve3",
        sharing_viking_user="team_evolve1",
        llm_api_key="llm-key",
        llm_model_id="model-a",
    )

    env, missing = build_dreamcycle_env(config)

    assert missing == []
    assert env["OPENVIKING_AGENT_ID"] == "team_evolve1"
    assert "single_evolve3" in json.loads(env["OPENVIKING_SOURCE_USERS"])


def test_unified_dreamcycle_routes_are_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    monkeypatch.setattr(dreamcycle_integration, "_STATE_DIR", tmp_path)
    server = ProxyServer(
        TeamEvolverConfig(
            dreamcycle_enabled=False,
            dreamcycle_auto_start=False,
            dreamcycle_state_dir=str(tmp_path),
                users_registry_path=str(tmp_path / "users.json"),
            sharing_enabled=False,
            sharing_skill_reload_mode="off",
        )
    )

    with TestClient(server.app) as client:
        assert client.post(
            "/api/auth/bootstrap",
            json={
                "username": "admin",
                "display_name": "Admin",
                "password": "secret",
            },
        ).status_code == 200
        status = client.get("/trigger-dreamcycle/status")
        trigger = client.post("/trigger-dreamcycle")
        changes = client.get("/trigger-dreamcycle/memory-changes")

    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert trigger.status_code == 202
    assert trigger.json()["status"] == "disabled"

    assert status.json()["engine"] == "teamEvolver-native-dreamcycle"
    assert status.json()["full_capabilities"] is True
    assert changes.status_code == 200
    assert changes.json() == {
        "schema_version": "teamevolver.memory-change-list.v1",
        "count": 0,
        "items": [],
    }

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


def test_generic_agent_registration_cannot_override_local_storage(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file)
    data = store.load()
    data["sharing"].update(
        {
            "viking_deployment": "local",
            "viking_endpoint": "",
            "viking_api_key": "local-root",
            "viking_personal_api_key": "local-root",
            "viking_team_api_key": "local-root",
        }
    )
    store.save(data)
    server = ProxyServer(
        store.to_config(),
    )

    async def fake_reload(config):
        server.config = config

    monkeypatch.setattr(server, "_reload_openviking_integrations", fake_reload)
    with TestClient(server.app) as client:
        response = client.post(
            "/internal/agents/register",
            json={
                "agent_id": "agentshub:tenant-a",
                "runtime_type": "agentshub",
                "capabilities": ["session_ingest", "true_replay"],
                "endpoints": {
                    "replay_url": "http://agentshub.test/replay",
                },
                "storage": {
                    "endpoint": "https://cloud-openviking.test",
                    "team_api_key": _key("acct", "team-space"),
                    "personal_api_keys": [_key("acct", "alice")],
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["storage_updated"] is False
    assert (
        response.json()["storage_ignored_reason"]
        == "local_deployment_is_authoritative"
    )
    saved = store.load()["sharing"]
    assert saved["viking_deployment"] == "local"
    assert saved["viking_endpoint"] == ""
    assert saved["viking_team_api_key"] == "local-root"
    agents = json.loads((tmp_path / "agents.json").read_text())["agents"]
    assert agents[0]["agent_id"] == "agentshub:tenant-a"
    assert agents[0]["capabilities"] == ["session_ingest", "true_replay"]


@pytest.mark.anyio
async def test_personal_source_sync_does_not_restart_evolve(monkeypatch) -> None:
    config = TeamEvolverConfig(
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_team_api_key=_key("acct", "team-space"),
        sharing_viking_account="acct",
        sharing_viking_user="team-space",
    )
    server = ProxyServer(config)
    server._embedded_evolve_server = SimpleNamespace(
        config=server._build_embedded_evolve_config(config)
    )
    calls: list[tuple[str, bool] | tuple[str]] = []

    monkeypatch.setattr(server._dreamcycle, "stop", lambda: None)
    monkeypatch.setattr(server, "_start_dreamcycle", lambda: calls.append(("dream",)))

    async def fake_stop(*, graceful: bool = False) -> None:
        calls.append(("evolve-stop", graceful))

    monkeypatch.setattr(server, "_stop_embedded_evolve", fake_stop)
    monkeypatch.setattr(
        server,
        "_start_embedded_evolve",
        lambda: calls.append(("evolve-start",)),
    )

    await server._reload_openviking_integrations(
        replace(
            config,
            sharing_viking_personal_api_keys=[_key("acct", "alice")],
        )
    )

    assert ("evolve-stop", True) not in calls
    assert ("evolve-start",) not in calls
    assert ("dream",) in calls


@pytest.mark.anyio
async def test_team_target_sync_restarts_evolve_gracefully(monkeypatch) -> None:
    config = TeamEvolverConfig(
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_team_api_key=_key("acct", "team-a"),
        sharing_viking_account="acct",
        sharing_viking_user="team-a",
    )
    server = ProxyServer(config)
    server._embedded_evolve_server = SimpleNamespace(
        config=server._build_embedded_evolve_config(config)
    )
    calls: list[tuple[str, bool] | tuple[str]] = []

    monkeypatch.setattr(server._dreamcycle, "stop", lambda: None)
    monkeypatch.setattr(server, "_start_dreamcycle", lambda: None)

    async def fake_stop(*, graceful: bool = False) -> None:
        calls.append(("evolve-stop", graceful))

    monkeypatch.setattr(server, "_stop_embedded_evolve", fake_stop)
    monkeypatch.setattr(
        server,
        "_start_embedded_evolve",
        lambda: calls.append(("evolve-start",)),
    )

    await server._reload_openviking_integrations(
        replace(
            config,
            sharing_viking_team_api_key=_key("acct", "team-b"),
            sharing_viking_user="team-b",
        )
    )

    assert ("evolve-stop", True) in calls
    assert ("evolve-start",) in calls


def test_team_settings_admin_round_trip_and_user_forbidden(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file)
    config = replace(
        store.to_config(),
        users_registry_path=str(tmp_path / "users.json"),
    )
    client = TestClient(ProxyServer(config).app)

    bootstrap = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "display_name": "Admin", "password": "secret"},
    )
    assert bootstrap.status_code == 200
    assert client.get("/api/team-settings").json()["display_name"] == "Team"

    saved = client.post(
        "/api/team-settings",
        json={"display_name": "产品团队"},
    )
    assert saved.status_code == 200
    assert saved.json()["display_name"] == "产品团队"
    assert store.get("team.display_name") == "产品团队"

    assert client.post("/api/auth/logout").status_code == 200
    registered = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "secret"},
    )
    assert registered.status_code == 200
    forbidden = client.post(
        "/api/team-settings",
        json={"display_name": "越权修改"},
    )
    assert forbidden.status_code == 403


def test_native_memory_evolution_settings_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file)
    config = store.to_config()
    config = replace(config, users_registry_path=str(tmp_path / "users.json"))
    server = ProxyServer(config)
    client = TestClient(server.app)
    login = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "display_name": "Admin", "password": "secret"},
    )
    assert login.status_code == 200

    dry_run = client.get("/trigger-dreamcycle/dry-run")
    assert dry_run.status_code == 200
    assert len(dry_run.json()["jobs"]) == 5
    assert "viking_merge" in dry_run.json()["tools"]

    reset_preview = client.post(
        "/trigger-dreamcycle/reset",
        json={"remote": False, "dry_run": True},
    )
    assert reset_preview.status_code == 200
    assert reset_preview.json()["status"] == "preview"
    assert "DRY-RUN" in reset_preview.json()["output"]

    reset_without_confirmation = client.post(
        "/trigger-dreamcycle/reset",
        json={"remote": False, "dry_run": False},
    )
    assert reset_without_confirmation.status_code == 400

    initial = client.get("/api/evolve-settings").json()["memory_maintenance"]
    jobs = initial["jobs"]
    assert len(jobs) == 5
    assert initial["maintained_space"] == "viking://user/memories/"
    assert "viking_read_many" in initial["tools"]
    jobs[0]["effective_prompt"] = "custom team overview prompt"
    jobs[0]["runtime"].update(
        {
            "model": "job-model",
            "temperature": 0.1,
            "max_tokens": 16000,
            "max_turns": 18,
            "max_errors": 2,
        }
    )

    response = client.post(
        "/api/evolve-settings",
        json={
            "memory_maintenance": {
                "enabled": True,
                "auto_start": False,
                "model": "memory-model",
                "base_url": "https://llm.example/v1",
                "llm_max_tokens": 32000,
                "temperature": 0.2,
                "customer_id": "customer-a",
                "embed_model": "embedding-model",
                "embed_base_url": "https://embed.example/v1",
                "dedup_merge_threshold": 0.9,
                "dedup_warn_threshold": 0.75,
                "scheduler": {
                    "active_start_hour": 1,
                    "active_end_hour": 7,
                    "rounds_per_window": 4,
                    "round_interval_minutes": 60,
                    "max_turns_per_job": 30,
                    "max_consecutive_errors": 4,
                    "retry_delay_seconds": 120,
                },
                "jobs": jobs,
            }
        },
    )

    assert response.status_code == 200
    memory = response.json()["memory_maintenance"]
    assert memory["engine"] == "teamEvolver-native-dreamcycle"
    assert memory["full_capabilities"] is True
    assert memory["scheduler"]["rounds_per_window"] == 4
    assert memory["llm_max_tokens"] == 32000
    assert memory["customer_id"] == "customer-a"
    assert memory["maintained_space"] == (
        "viking://user/peers/customer-a/memories/"
    )
    assert memory["embed_model"] == "embedding-model"
    assert memory["dedup_merge_threshold"] == 0.9
    assert memory["dedup_warn_threshold"] == 0.75
    assert memory["jobs"][0]["effective_prompt"] == "custom team overview prompt"
    assert memory["jobs"][0]["runtime"]["model"] == "job-model"
    assert memory["jobs"][0]["runtime"]["max_turns"] == 18
    assert memory["jobs"][1]["runtime"]["model"] == ""
    saved = store.load()["dreamcycle"]
    assert saved["job_prompts"]["team_overview"] == "custom team overview prompt"
    assert saved["job_settings"]["team_overview"]["model"] == "job-model"
    assert saved["llm_model"] == "memory-model"
    assert saved["customer_id"] == "customer-a"
    assert saved["embed_model"] == "embedding-model"

def test_full_native_dreamcycle_exposes_all_jobs_and_plans(tmp_path) -> None:
    engine = FullDreamCycleSupervisor(
        TeamEvolverConfig(
            sharing_viking_endpoint="http://openviking.test",
            sharing_viking_team_api_key=_key("acct", "team"),
            llm_api_key="llm-key",
            llm_model_id="model-a",
            dreamcycle_state_dir=str(tmp_path),
        )
    )

    plan = engine.dry_run()

    assert plan["engine"] == "teamEvolver-native-dreamcycle"
    assert [job["id"] for job in plan["jobs"]] == [
        "team_overview",
        "deduplication",
        "cleanup",
        "onboarding_check",
        "consolidate",
    ]
    assert all(job["tasks"] for job in plan["jobs"])
    assert engine.status()["full_capabilities"] is True
    assert set(engine._scheduler._build_tools().tool_names) == {
        "viking_search",
        "viking_read",
        "viking_read_many",
        "viking_browse",
        "viking_remember",
        "viking_forget",
        "viking_merge",
        "list_customers",
        "memory_audit",
        "memory_sanitize",
        "save_report",
        "shared_notes",
    }
