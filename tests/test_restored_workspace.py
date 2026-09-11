from starlette.requests import Request

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.openviking_workspace import OpenVikingWorkspaceMixin, _scope_map
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant


class Workspace(OpenVikingWorkspaceMixin):
    def __init__(self, config):
        self.config = config



def test_mining_request_waits_for_already_started_child(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from teamEvolver.proxy.skillminer_bridge import SkillMinerBridgeMixin

    owner = SkillMinerBridgeMixin()
    owner._skillminer_proc = SimpleNamespace(poll=lambda: None)
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(owner, "_await_skillminer_ready", probe)
    request = Request({"type": "http", "path": "/api/mining/config", "headers": []})
    response = asyncio.run(owner._dispatch_skillminer_request(request))
    assert response.status_code == 503
    probe.assert_awaited_once()

def test_workspace_actor_uses_persisted_user_registry(monkeypatch):
    config = TeamEvolverConfig(storage_pg_enabled=True)
    owner = Workspace(config)
    user = {"id": "admin", "role": "admin"}
    seen = []

    def load(path, config=None):
        seen.append(config)
        return {"users": [user]}

    monkeypatch.setattr("teamEvolver.proxy.openviking_workspace._load_registry", load)
    request = Request({"type": "http", "state": {"console_user": user}})
    assert owner._workspace_actor(request, "admin")[0] == user
    assert seen == [config]


def test_workspace_headers_use_selected_account_and_service_credential():
    config = TeamEvolverConfig(storage_pg_enabled=True, sharing_viking_account="default",
                              sharing_viking_team_api_key="service-key")
    owner = Workspace(config)
    user = {"id": "alice", "team_space": {"viking_api_key": "another-account-key"}}
    token = set_current_tenant(TenantContext(tenant_id="account-a"))
    try:
        scope = _scope_map(config, "alice", is_admin=True)["team_memory"]
        headers = owner._workspace_headers(user, scope)
        assert headers["X-OpenViking-Account"] == "account-a"
        assert headers["X-API-Key"] == "service-key"
        assert config.sharing_viking_account == "default"
    finally:
        reset_current_tenant(token)


def test_mining_console_keeps_runtime_data_across_code_refresh(monkeypatch, tmp_path):
    from teamEvolver.proxy.skillminer_bridge import SkillMinerBridgeMixin

    source = tmp_path / "installed"
    (source / "web_console").mkdir(parents=True)
    (source / "web_console/server.py").write_text("# current code")
    (source / "data").mkdir()
    (source / "data/private.json").write_text("not deployment data")
    monkeypatch.setattr("teamEvolver.proxy.skillminer_bridge._SKILLMINER_ROOT", source)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TEAMEVOLVER_CUSTOMER_MODE", "1")
    owner = SkillMinerBridgeMixin()
    owner.config = TeamEvolverConfig(storage_pg_enabled=True)
    root = owner._skillminer_runtime_root()
    assert (root / "web_console/server.py").read_text() == "# current code"
    assert not (root / "data/private.json").exists()
    (root / "data/input/source.md").write_text("user input")
    (root / "web_console/server.py").write_text("# outdated code")
    assert owner._skillminer_runtime_root() == root
    assert (root / "web_console/server.py").read_text() == "# current code"
    assert (root / "data/input/source.md").read_text() == "user input"


def test_full_console_workspace_routes_stay_tenant_scoped(monkeypatch, tmp_path):
    import os
    import uuid
    import pytest
    from fastapi.testclient import TestClient
    from teamEvolver.proxy.server import ProxyServer
    from teamEvolver.proxy.users_admin import _save_registry
    from teamEvolver.storage.pg_pool import close_pg_runtimes

    dsn = os.environ.get("TE_PG_TEST_DSN")
    if not dsn:
        pytest.skip("TE_PG_TEST_DSN not set")
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "restore-ui-test-root-" + "x" * 32)
    monkeypatch.setenv("TEAMEVOLVER_SKILLMINER_ENABLED", "0")
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    monkeypatch.setattr("teamEvolver.proxy.server.sync_openviking_user", lambda *args: None)
    config = TeamEvolverConfig(storage_pg_enabled=True, storage_pg_dsn=dsn,
                              storage_pg_schema="restore_" + uuid.uuid4().hex[:12],
                              sharing_enabled=False, users_registry_path=str(tmp_path / "users.json"),
                              sharing_viking_endpoint="http://unused.example", sharing_viking_team_api_key="test")
    from pathlib import Path
    _save_registry(Path(config.users_registry_path), {"users": [{"id": "admin", "role": "admin"}]}, config)
    headers = {"Authorization": "Bearer " + os.environ["TEAMEVOLVER_ROOT_API_KEY"]}
    try:
        with TestClient(ProxyServer(config).app) as client:
            assert client.post("/api/tenants", json={"account_id": "account-a"}, headers=headers).status_code == 200
            scoped = {**headers, "X-Tenant-Id": "account-a"}
            users = client.get("/api/users", headers=scoped)
            assert users.status_code == 200, users.text
            assert users.json()["users"][0]["id"] == "admin"
            result = client.get("/api/openviking/workspace/config?user_id=admin", headers=scoped)
            assert result.status_code == 200, result.text
            assert {"personal_memory", "team_memory", "personal_skills", "team_skills", "platform_assets"} <= result.json()["scopes"].keys()
            assert client.get("/api/evolve-model", headers=scoped).status_code == 409
    finally:
        close_pg_runtimes()
