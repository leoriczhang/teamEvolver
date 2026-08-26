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
        )
    )


def test_run_target_uri_is_normalized_and_isolates_work_and_state(tmp_path) -> None:
    service = _service(tmp_path)

    default_run = service.new_run("default")
    run = service.new_run(
        "default",
        target_uri="  viking://resources/engineering/team-memory/  ",
    )

    assert default_run.task_id != run.task_id
    assert len(run.task_id) >= 32
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


def test_service_uses_admin_key_without_exposing_it(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    run = service.new_run("default")
    captured = {}

    def fake_run_inner(run, *, kinds, full, user_ids, admin_key):
        captured["call"] = (run, kinds, full, user_ids)
        captured["admin_key"] = admin_key

    monkeypatch.setattr(service, "_run_inner", fake_run_inner)
    service.run(
        run,
        kinds=None,
        full=False,
        user_ids=["alice"],
        admin_key="admin-secret",
    )

    assert captured["admin_key"] == "admin-secret"
    assert run.status == "completed"
    assert "admin-secret" not in str(run.to_public())


def test_run_route_accepts_an_independent_target_uri(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config = TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=False,
        sharing_skill_reload_mode="off",
        sharing_viking_endpoint="http://127.0.0.1:1933",
        sharing_viking_account="default",
        sharing_viking_user="team",
        aggregation_state_dir=str(tmp_path),
    )
    server = ProxyServer(config)
    service = server._aggregation_service()
    captured = {}

    async def fake_list_account_users(account_id, *, admin_key):
        captured["users_account_id"] = account_id
        captured["users_admin_key"] = admin_key
        return ["alice", "bob"]

    def fake_run(run, *, kinds, full, user_ids, admin_key):
        captured["run_account_id"] = run.account_id
        captured["run_admin_key"] = admin_key
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
    assert missing_key.json()["detail"] == "admin_key is required"
    assert client.post("/api/aggregation/users", json={}).json()["detail"] == (
        "admin_key is required"
    )

    users = client.post(
        "/api/aggregation/users",
        json={"admin_key": "admin-secret"},
    )
    assert users.status_code == 200
    assert users.json() == {
        "account_id": "default",
        "users": ["alice", "bob"],
    }
    assert captured["users_admin_key"] == "admin-secret"
    assert "admin-secret" not in users.text
    assert client.get("/api/aggregation/users").status_code == 405

    response = client.post(
        "/api/aggregation/run",
        json={
            "admin_key": "admin-secret",
            "target_uri": "viking://resources/engineering/team-memory/",
            "user_ids": ["alice"],
        },
    )

    assert response.status_code == 202
    assert (
        response.json()["target_uri"]
        == "viking://resources/engineering/team-memory"
    )
    assert response.json()["account_id"] == "default"
    assert captured["run_admin_key"] == "admin-secret"
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
            "admin_key": "admin-secret",
            "target_uri": "viking://user/alice/memories/team",
            "user_ids": ["alice"],
        },
    )
    assert invalid.status_code == 400
    assert "viking://resources/<path>" in invalid.json()["detail"]
