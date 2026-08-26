from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from teamEvolver.aggregation import MemoryAggregationService
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.proxy import ProxyServer, aggregation_routes


def _service(tmp_path) -> MemoryAggregationService:
    return MemoryAggregationService(
        TeamEvolverConfig(
            aggregation_shared_knowledge_prefix="shared-knowledge",
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            sharing_viking_account="default",
            sharing_viking_endpoint="http://127.0.0.1:1933",
        )
    )


def test_run_target_uri_is_normalized_and_isolates_work_and_state(tmp_path) -> None:
    service = _service(tmp_path)

    default_run = service.new_run("default")
    run = service.new_run(
        "default",
        endpoint="https://openviking.example/",
        auth_mode="api_key",
        target_uri="  viking://resources/engineering/team-memory/  ",
    )

    assert default_run.task_id != run.task_id
    assert len(run.task_id) >= 32
    assert run.endpoint == "https://openviking.example"
    assert run.auth_mode == "api_key"
    assert default_run.target_uri == "viking://resources/shared-knowledge"
    assert run.target_uri == "viking://resources/engineering/team-memory"
    assert run.to_public()["target_uri"] == run.target_uri
    assert (
        service._work_root(run.target_uri)
        == "viking://resources/engineering/team-memory-staging"
    )
    assert (
        service._staging_uri("alice", run.target_uri)
        == "viking://resources/engineering/team-memory-staging/alice"
    )
    assert service._state_path("default") != service._state_path(
        "default",
        run.target_uri,
        run.endpoint,
        run.auth_mode,
    )


@pytest.mark.parametrize(
    "target_uri",
    [
        "viking://resources",
        "viking://user/alice/memories/team",
        "https://example.com/team-memory",
        "viking://resources/../private",
        "viking://resources/team memory",
        "viking://resources/team%20memory",
        "viking://resources/team%5Cmemory",
        "viking://resources/team?version=1",
    ],
)
def test_run_target_uri_rejects_unsafe_locations(tmp_path, target_uri: str) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="viking://resources/<path>"):
        service.new_run("default", target_uri=target_uri)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "openviking.example",
        "ftp://openviking.example",
        "http://user:pass@openviking.example",
        "http://openviking.example/base/../admin",
        "http://openviking.example/base?token=secret",
    ],
)
def test_run_rejects_invalid_openviking_endpoint(tmp_path, endpoint: str) -> None:
    service = MemoryAggregationService(
        TeamEvolverConfig(
            aggregation_state_dir=str(tmp_path),
            sharing_viking_endpoint="",
        )
    )

    with pytest.raises(ValueError, match="endpoint"):
        service.new_run("default", endpoint=endpoint)


def test_custom_endpoint_does_not_require_a_configured_default(tmp_path) -> None:
    service = MemoryAggregationService(
        TeamEvolverConfig(
            aggregation_state_dir=str(tmp_path),
            sharing_viking_endpoint="",
        )
    )

    run = service.new_run(
        "default",
        endpoint="https://openviking.example/base/",
    )

    assert run.endpoint == "https://openviking.example/base"
    assert service._state_path(
        run.account_id,
        run.target_uri,
        run.endpoint,
    ).parent == tmp_path


def test_config_store_removes_deprecated_aggregation_root_key(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    store = ConfigStore(config_path)

    store.save(
        {
            "aggregation": {
                "root_api_key": "legacy-secret",
                "shared_knowledge_prefix": "shared-knowledge",
            }
        }
    )

    assert store.get("aggregation.root_api_key") is None
    assert "legacy-secret" not in config_path.read_text(encoding="utf-8")


def test_service_uses_api_key_without_exposing_it(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    run = service.new_run("default")
    captured = {}

    def fake_run_inner(run, *, kinds, full, user_ids, api_key):
        captured["call"] = (run, kinds, full, user_ids)
        captured["api_key"] = api_key

    monkeypatch.setattr(service, "_run_inner", fake_run_inner)
    service.run(
        run,
        kinds=None,
        full=False,
        user_ids=["alice"],
        api_key="credential-secret",
    )

    assert captured["api_key"] == "credential-secret"
    assert run.status == "completed"
    assert "credential-secret" not in str(run.to_public())


def test_run_route_accepts_an_independent_target_uri(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config = TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=False,
        sharing_skill_reload_mode="off",
        sharing_viking_endpoint="http://127.0.0.1:1933",
        sharing_viking_team_api_key="configured-root-secret",
        sharing_viking_account="default",
        sharing_viking_user="team",
        aggregation_state_dir=str(tmp_path),
    )
    server = ProxyServer(config)
    service = server._aggregation_service()
    captured = {}

    async def fake_list_account_users(account_id, *, api_key, endpoint):
        captured["users_account_id"] = account_id
        captured["users_api_key"] = api_key
        captured["users_endpoint"] = endpoint
        return ["alice", "bob"]

    def fake_run(run, *, kinds, full, user_ids, api_key):
        captured["run_account_id"] = run.account_id
        captured["run_endpoint"] = run.endpoint
        captured["run_auth_mode"] = run.auth_mode
        captured["run_api_key"] = api_key
        captured["run_user_ids"] = user_ids
        captured["run_kinds"] = kinds
        captured["run_full"] = full

    monkeypatch.setattr(service, "list_account_users", fake_list_account_users)
    monkeypatch.setattr(service, "run", fake_run)

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(aggregation_routes.threading, "Thread", ImmediateThread)
    client = TestClient(server.app)
    assert client.get("/api/aggregation/runs").status_code == 401
    assert client.get("/api/aggregation/settings").status_code == 401

    missing_key = client.post(
        "/api/aggregation/run",
        json={"target_uri": "viking://resources/engineering/team-memory"},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"] == (
        "exactly one of root_key or admin_key is required"
    )
    assert client.post("/api/aggregation/users", json={}).json()["detail"] == (
        "exactly one of root_key or admin_key is required"
    )

    users = client.post(
        "/api/aggregation/users",
        json={
            "endpoint": "https://openviking.example/",
            "admin_key": "admin-secret",
        },
    )
    assert users.status_code == 200
    assert users.json() == {
        "endpoint": "https://openviking.example",
        "account_id": "default",
        "auth_mode": "api_key",
        "users": ["alice", "bob"],
    }
    assert captured["users_endpoint"] == "https://openviking.example"
    assert captured["users_api_key"] == "admin-secret"
    assert "admin-secret" not in users.text
    assert client.get("/api/aggregation/users").status_code == 405

    response = client.post(
        "/api/aggregation/run",
        json={
            "endpoint": "https://openviking.example/",
            "admin_key": "admin-secret",
            "target_uri": "viking://resources/engineering/team-memory/",
            "user_ids": ["alice"],
        },
    )
    assert response.json()["endpoint"] == "https://openviking.example"
    assert response.json()["auth_mode"] == "api_key"

    assert response.status_code == 202
    assert (
        response.json()["target_uri"]
        == "viking://resources/engineering/team-memory"
    )
    assert response.json()["account_id"] == "default"
    assert captured["run_endpoint"] == "https://openviking.example"
    assert captured["run_auth_mode"] == "api_key"
    assert captured["run_api_key"] == "admin-secret"
    assert captured["run_user_ids"] == ["alice"]
    assert "admin-secret" not in response.text
    status = client.get(
        f"/api/aggregation/status/{response.json()['task_id']}"
    )
    assert status.status_code == 200
    assert "admin-secret" not in status.text

    invalid = client.post(
        "/api/aggregation/run",
        json={
            "endpoint": "https://openviking.example",
            "admin_key": "admin-secret",
            "target_uri": "viking://user/alice/memories/team",
            "user_ids": ["alice"],
        },
    )
    assert invalid.status_code == 400
    assert "viking://resources/<path>" in invalid.json()["detail"]

    invalid_endpoint = client.post(
        "/api/aggregation/run",
        json={
            "endpoint": "file:///etc",
            "admin_key": "admin-secret",
        },
    )
    assert invalid_endpoint.status_code == 400
    assert invalid_endpoint.json()["detail"] == (
        "endpoint must be a valid HTTP(S) URL"
    )

    both_keys = client.post(
        "/api/aggregation/run",
        json={
            "root_key": "root-secret",
            "admin_key": "admin-secret",
        },
    )
    assert both_keys.status_code == 400
    assert both_keys.json()["detail"] == (
        "root_key and admin_key are mutually exclusive"
    )

    trusted = client.post(
        "/api/aggregation/users",
        json={"root_key": "external-root-secret"},
    )
    assert trusted.status_code == 200
    assert trusted.json()["auth_mode"] == "trusted"
    assert captured["users_api_key"] == "external-root-secret"
    assert "external-root-secret" not in trusted.text

    assert client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "display_name": "Admin", "password": "secret"},
    ).status_code == 200
    console = client.post("/api/aggregation/users", json={})
    assert console.status_code == 200
    assert console.json()["auth_mode"] == "trusted"
    assert captured["users_api_key"] == "configured-root-secret"
    assert "configured-root-secret" not in console.text
