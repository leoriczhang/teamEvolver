from __future__ import annotations

import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from team_memory.maintenance.runtime import TeamMemorySupervisor
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.proxy import ProxyServer


def _key(account: str, user: str) -> str:
    encode = lambda value: base64.b64encode(value.encode()).decode().rstrip("=")
    return f"{encode(account)}.{encode(user)}.secret"


def _select_default_tenant(client: TestClient) -> None:
    client.headers["X-Tenant-Id"] = "default"


def test_defaults_enable_full_evolution_loop(tmp_path) -> None:
    config = ConfigStore(tmp_path / "missing.yaml").to_config()

    assert config.proxy_port == 52010
    assert config.team_display_name == "Team"
    assert config.skills_runtime_cache_dir.endswith("/.teamEvolver/runtime/skills")
    assert config.sharing_skill_backend == ""
    assert config.sharing_session_backend == ""
    assert config.sharing_viking_root_prefix == "team-skill-evolver"
    assert config.validation_enabled is True
    # Native memory evolution stays opt-in by default.
    assert config.memory_maintenance_enabled is False
    assert config.memory_maintenance_auto_start is False
    assert config.memory_maintenance_interval_seconds == 86400


def test_team_display_name_persists_and_environment_wins(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    store.set("team.display_name", "产品团队")

    assert store.to_config().team_display_name == "产品团队"

    monkeypatch.setenv("EVOLVE_TEAM_DISPLAY_NAME", "环境团队")
    assert store.to_config().team_display_name == "环境团队"


def test_memory_maintenance_routes_are_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    server = ProxyServer(
        TeamEvolverConfig(
            memory_maintenance_enabled=False,
            memory_maintenance_auto_start=False,
            _config_file=str(tmp_path / "config.yaml"),
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
        _select_default_tenant(client)
        status = client.get("/api/memory/maintenance/status")
        trigger = client.post("/api/memory/maintenance/run")
        changes = client.get("/api/memory/changes")

    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert trigger.status_code == 202
    assert trigger.json()["status"] == "disabled"

    assert status.json()["engine"] == "team-memory-compile"
    assert status.json()["full_capabilities"] is False
    assert changes.status_code == 200
    assert changes.json() == {
        "schema_version": "teamevolver.memory-change-list.v1",
        "count": 0,
        "items": [],
    }


def test_maintenance_supervisor_only_submits_openviking_compile() -> None:
    calls: list[tuple] = []
    run = SimpleNamespace(
        task_id="agg_test",
        status="pending",
        error="",
        to_public=lambda: {"task_id": "agg_test", "status": run.status},
    )

    class Service:
        def new_run(self, account_id, *, pipeline):
            calls.append(("new_run", account_id, pipeline))
            return run

        async def reconcile(self, selected, key):
            calls.append(("reconcile", selected.task_id, key))

        def run(self, selected, *, api_key):
            calls.append(("run", selected.task_id, api_key))
            selected.status = "completed"

        def resolve_target_uri(self):
            return "viking://resources/shared-knowledge"

        def _shared_skill_uri(self, stage):
            return f"viking://agent/skills/{stage}"

        def list_runs(self):
            return [run.to_public()]

    config = SimpleNamespace(
        memory_maintenance_enabled=True,
        memory_maintenance_auto_start=False,
        memory_maintenance_interval_seconds=86400,
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_api_key="root-key",
        sharing_viking_account="tenant-a",
    )
    supervisor = TeamMemorySupervisor(config, Service())

    assert supervisor.trigger()["status"] == "started"
    supervisor._trigger_thread.join(timeout=2)

    assert calls == [
        ("new_run", "tenant-a", "maintain"),
        ("reconcile", "agg_test", "root-key"),
        ("run", "agg_test", "root-key"),
    ]
    assert "jobs" not in supervisor.status()
    assert supervisor.status()["tools"] == ["ov_cp", "ov_compile"]


def test_memory_true_replay_routes_are_admin_only(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config = TeamEvolverConfig(
        memory_maintenance_enabled=False,
        memory_maintenance_auto_start=False,
        _config_file=str(tmp_path / "config.yaml"),
    )
    server = ProxyServer(config)
    captured: dict = {}

    def run_replay(**kwargs):
        captured.update(kwargs)
        return {
            "replay_id": "mrp-test",
            "change_id": kwargs["change_id"],
            "status": "evaluated",
            "verdict": "accept",
        }

    monkeypatch.setattr(
        server,
        "_run_memory_replay",
        run_replay,
    )
    monkeypatch.setattr(
        server,
        "_memory_replays",
        lambda **kwargs: {
            "schema_version": "teamevolver.memory-true-replay-list.v1",
            "change_id": kwargs["change_id"],
            "count": 1,
            "items": [{"replay_id": "mrp-test"}],
        },
    )

    with TestClient(server.app) as client:
        denied = client.post(
            "/api/memory/changes/mch-test/true-replay",
            json={"query": "task", "checklist": ["done"]},
        )
        assert denied.status_code == 401
        assert client.post(
            "/api/auth/bootstrap",
            json={
                "username": "admin",
                "display_name": "Admin",
                "password": "secret",
            },
        ).status_code == 200
        _select_default_tenant(client)
        replay = client.post(
            "/api/memory/changes/mch-test/true-replay",
            json={
                "query": "perform task",
                "checklist": "first requirement\nsecond requirement",
                "source_session_id": "session-1",
                "max_interactions": 3,
            },
        )
        history = client.get(
            "/api/memory/changes/mch-test/true-replays",
        )

    assert replay.status_code == 200
    assert replay.json()["verdict"] == "accept"
    assert captured["checklist"] == [
        "first requirement",
        "second requirement",
    ]
    assert captured["source_session_id"] == "session-1"
    assert captured["max_interactions"] == 3
    assert history.status_code == 200
    assert history.json()["count"] == 1


@pytest.mark.anyio
async def test_team_target_sync_restarts_evolve_gracefully(monkeypatch) -> None:
    config = TeamEvolverConfig(
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_api_key=_key("acct", "team-a"),
        sharing_viking_account="acct",
        sharing_viking_user="team-a",
    )
    server = ProxyServer(config)
    monkeypatch.setattr(
        server,
        "_embedded_evolve_config_snapshot",
        lambda: server._build_embedded_evolve_config(config),
    )
    calls: list[tuple[str, bool] | tuple[str]] = []

    monkeypatch.setattr(server._memory_maintenance, "stop", lambda: None)
    monkeypatch.setattr(server, "_start_memory_maintenance", lambda: None)

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
            sharing_viking_api_key=_key("acct", "team-b"),
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
    config = store.to_config()
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


def test_compile_memory_maintenance_settings_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file)
    config = store.to_config()
    server = ProxyServer(config)
    client = TestClient(server.app)
    login = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "display_name": "Admin", "password": "secret"},
    )
    assert login.status_code == 200
    _select_default_tenant(client)

    dry_run = client.get("/api/memory/maintenance/dry-run")
    assert dry_run.status_code == 200
    assert dry_run.json()["stages"] == ["snapshot", "maintenance"]
    assert dry_run.json()["maintained_space"].startswith("viking://resources/")

    initial = client.get("/api/skill-evolution/settings").json()["memory_maintenance"]
    assert initial["maintained_space"] == "viking://resources/shared-knowledge"
    assert initial["tools"] == ["ov_cp", "ov_compile"]

    response = client.post(
        "/api/skill-evolution/settings",
        json={
            "memory_maintenance": {
                "enabled": True,
                "auto_start": False,
                "interval_seconds": 3600,
            }
        },
    )

    assert response.status_code == 200
    memory = response.json()["memory_maintenance"]
    assert memory["engine"] == "team-memory-compile"
    assert memory["full_capabilities"] is False
    assert memory["maintained_space"] == "viking://resources/shared-knowledge"
    assert memory["interval_seconds"] == 3600
    assert memory["tools"] == ["ov_cp", "ov_compile"]
    saved = store.load()["memory"]["maintenance"]
    assert saved["interval_seconds"] == 3600
