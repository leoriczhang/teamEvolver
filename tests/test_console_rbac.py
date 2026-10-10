"""Console boundaries exercised through the real authentication middlewares."""
import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from teamEvolver.admin.authorization import is_platform_admin, require_tenant_admin
from teamEvolver.admin.identity_store import IdentityStore
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.tenants.registry import TenantContext, current_tenant_id


class Tenants:
    mode = "postgres"
    default_machine_credential_configured = False

    def __init__(self):
        self.tenants = {tid: TenantContext(tid, tid) for tid in ("default", "alpha", "beta")}

    def get(self, tid):
        return self.tenants.get(tid)

    def list_tenants(self):
        return list(self.tenants.values())

    def default_context(self):
        return self.get("default")

    def update_tenant_config(self, tenant_id, updates):
        current = self.tenants.get(tenant_id)
        if current is None:
            return None
        config = dict(current.config_overrides)
        for key, value in updates.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        self.tenants[tenant_id] = TenantContext(
            current.tenant_id, current.display_name, current.status, config,
        )
        return self.tenants[tenant_id]


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.delenv("TEAMEVOLVER_ROOT_API_KEY", raising=False)
    monkeypatch.delenv("TEAMEVOLVER_PLATFORM_ADMINS", raising=False)
    cfg = TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"), skills_runtime_cache_dir=str(tmp_path / "skills"),
        sharing_skill_mirror_enabled=False, sharing_viking_api_key="",
    )
    server = ProxyServer(cfg)
    server._tenant_registry = Tenants()

    @server.app.get("/api/rbac-probe")
    async def read(request: Request):
        return {"tenant_id": current_tenant_id(), "user": request.state.console_user}

    @server.app.post("/api/rbac-probe")
    async def manage(request: Request):
        require_tenant_admin(request.state.console_user)
        return {"tenant_id": current_tenant_id()}

    root = TestClient(server.app)
    assert root.post("/api/auth/bootstrap", json={"username": "root", "password": "test-password"}).status_code == 200
    return server, root, IdentityStore(cfg)


def register(server, username):
    client = TestClient(server.app)
    response = client.post("/api/auth/register", json={"username": username, "password": "test-password"})
    assert response.status_code == 200, response.text
    return client


def test_registration_has_no_implicit_default_membership(system):
    server, root, store = system
    alice = register(server, "alice")
    assert store.memberships(user_id="alice") == []
    assert alice.get("/api/rbac-probe").status_code == 403
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "default"}).status_code == 403
    status = alice.get("/api/auth/status").json()
    assert status["available_tenants"] == []
    assert status["active_tenant_id"] is None
    assert alice.get("/api/users").status_code == 200
    assert alice.post("/api/auth/logout").status_code == 200


def test_roles_are_per_tenant_and_rechecked_on_every_request(system):
    server, root, store = system
    alice = register(server, "alice")
    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    store.set_member("beta", "alice", "member", "active", "root")
    for tid, expected in (("alpha", 200), ("beta", 403), ("default", 403)):
        headers = {"X-Tenant-Id": tid}
        assert alice.post("/api/rbac-probe", headers=headers).status_code == expected
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "beta"}).status_code == 200
    assert alice.get("/api/tenants").json()["tenants"] == [
        {"tenant_id": "alpha", "display_name": "alpha", "status": "active"},
        {"tenant_id": "beta", "display_name": "beta", "status": "active"},
    ]
    assert alice.get("/api/auth/status").json()["active_tenant_id"] is None
    assert alice.get("/api/auth/status", headers={"X-Tenant-Id": "alpha"}).json()["active_tenant_id"] == "alpha"
    store.set_member("beta", "alice", "member", "disabled", "root")
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "beta"}).status_code == 403


def test_platform_admin_can_govern_any_selected_tenant_and_is_audited(system):
    server, root, store = system
    headers = {"X-Tenant-Id": "alpha"}
    assert root.get("/api/rbac-probe", headers=headers).status_code == 200
    assert root.post("/api/rbac-probe", headers=headers).status_code == 200
    assert root.post("/api/rbac-probe", headers={"X-Tenant-Id": "beta"}).status_code == 200
    peer = TestClient(server.app)
    assert peer.post("/api/auth/login", json={"username": "root", "password": "test-password"}).status_code == 200
    assert peer.post("/api/rbac-probe", headers=headers).status_code == 200
    assert {"console.access", "console.result"} <= {e["action"] for e in store.audit_events()}


def test_tenant_config_recursively_redacts_secrets(system):
    server, root, _store = system
    server._tenant_registry.tenants["alpha"] = TenantContext(
        "alpha", "alpha", config_overrides={
            "llm_model_id": "model",
            "nested": {"api_token": "secret", "visible": "ok"},
            "connections": [{"password": "secret", "name": "primary"}],
        },
    )
    payload = root.get("/api/tenants/alpha/config", headers={"X-Tenant-Id": "alpha"}).json()
    assert payload["config_overrides"] == {
        "llm_model_id": "model",
        "nested": {"visible": "ok"},
        "connections": [{"name": "primary"}],
    }
    assert payload["secret_presence"]["nested.api_token"] is True
    assert payload["secret_presence"]["connections.0.password"] is True


def test_tenant_admin_cannot_change_global_identity_or_other_tenant(system):
    server, root, store = system
    alice = register(server, "alice")
    register(server, "bob")
    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    h = {"X-Tenant-Id": "alpha"}
    assert alice.post("/api/tenants", json={"display_name": "forbidden"}, headers=h).status_code == 403
    assert alice.put(
        "/api/identity/users/alice/access",
        json={"platform_role": "platform_admin"},
        headers=h,
    ).status_code == 403
    assert alice.put("/api/users/bob/profile", json={"password": "hijack"}, headers=h).status_code == 403
    assert alice.get("/api/openviking-accounts", headers=h).status_code == 403
    assert alice.post("/api/sharing-config", json={"account": "beta"}, headers=h).status_code == 403
    assert alice.put("/api/tenants/alpha/config", json={"overrides": {"sharing_viking_account": "beta"}},
                     headers=h).status_code == 403
    assert alice.put("/api/tenants/alpha/config", json={"overrides": {"max_evolve_per_day": 10}},
                     headers=h).status_code == 200
    assert alice.put("/api/tenants/beta/members/bob", json={"role": "tenant_admin"}, headers=h).status_code == 403
    assert alice.put("/api/tenants/alpha/members/bob", json={"role": "member"}, headers=h).status_code == 200
    assert alice.put("/api/tenants/alpha/members/bob", json={"role": "platform_admin"}, headers=h).status_code == 400


def test_disabled_tenant_and_user_are_rejected(system):
    server, root, store = system
    alice = register(server, "alice")
    store.set_member("alpha", "alice", "member", "active", "root")
    h = {"X-Tenant-Id": "alpha"}
    server._tenant_registry.tenants["alpha"] = TenantContext("alpha", status="disabled")
    assert alice.get("/api/rbac-probe", headers=h).status_code == 403
    assert root.put("/api/identity/users/alice/access", json={"status": "disabled"}).status_code == 200
    assert alice.get("/api/auth/status").json()["authenticated"] is False


def test_last_platform_and_tenant_admin_are_protected(system):
    server, root, store = system
    register(server, "alice")
    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    assert root.put(
        "/api/tenants/alpha/members/alice", json={"status": "disabled"},
        headers={"X-Tenant-Id": "alpha"},
    ).status_code == 409
    assert root.put("/api/identity/users/root/access", json={"platform_role": "user"}).status_code == 409


def test_platform_admin_role_never_depends_on_the_account_name():
    assert is_platform_admin({"id": "admin", "platform_role": "platform_admin"})
    assert not is_platform_admin({"id": "admin", "platform_role": "user"})
    assert not is_platform_admin({"id": "admin"})


def test_legacy_admin_is_migrated_only_to_tenant_admin(tmp_path, monkeypatch):
    monkeypatch.delenv("TEAMEVOLVER_PLATFORM_ADMINS", raising=False)
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": [{"id": "admin", "role": "admin"}]}))
    cfg = TeamEvolverConfig(_config_file=str(tmp_path / "config.yaml"))
    store = IdentityStore(cfg)
    user = store.load_registry()["users"][0]
    assert user["id"] == "admin"
    assert user["platform_role"] == "user"
    assert store.memberships(user_id="admin")[0]["role"] == "tenant_admin"
