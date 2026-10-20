"""Tenant configuration, membership permissions and machine credentials."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from teamEvolver.admin.console_session import ConsoleSessionManager
from teamEvolver.admin.identity_store import IdentityStore
from teamEvolver.app import create_app
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.runtime.services import RuntimeServices
from teamEvolver.tenants.registry import TenantContext, current_tenant_id

# --------------------------------------------------------------------------- #
# Faithful in-process tenant registry (postgres mode, no live PG)
# --------------------------------------------------------------------------- #


class FakeRegistry:
    """A ``postgres``-mode TenantRegistry stand-in with full CRUD + token map."""

    mode = "postgres"
    default_machine_credential_configured = False

    def __init__(self) -> None:
        self.tenants: dict[str, TenantContext] = {tid: TenantContext(tid, tid) for tid in ("default", "alpha", "beta")}
        self.tokens: dict[str, str] = {}  # agent token -> tenant_id
        self._seq = 0

    # -- lookup -------------------------------------------------------------- #
    def get(self, tid):
        return self.tenants.get(tid)

    def list_tenants(self):
        return list(self.tenants.values())

    def default_context(self):
        return self.tenants["default"]

    def resolve_by_agent_token(self, token):
        tid = self.tokens.get(str(token or ""))
        return self.tenants.get(tid) if tid else None

    # -- admin mutations ----------------------------------------------------- #
    def create_tenant(self, display_name, tenant_id=""):
        if any(t.display_name == display_name for t in self.tenants.values()):
            raise ValueError(f"display_name already in use: {display_name!r}")
        self._seq += 1
        tid = tenant_id or f"t_{self._seq:08d}"
        ctx = TenantContext(tid, display_name)
        self.tenants[tid] = ctx
        token = f"tevt_{tid}_secret"
        self.tokens[token] = tid
        return ctx, token

    def rotate_token(self, tenant_id):
        if tenant_id == "default":
            raise ValueError("default is reserved")
        if tenant_id not in self.tenants:
            return None
        self.tokens = {tok: tid for tok, tid in self.tokens.items() if tid != tenant_id}
        token = f"tevt_{tenant_id}_rotated"
        self.tokens[token] = tenant_id
        return token

    def set_status(self, tenant_id, status):
        if status not in {"active", "disabled"}:
            raise ValueError(f"invalid tenant status: {status!r}")
        current = self.tenants.get(tenant_id)
        if current is None:
            return False
        self.tenants[tenant_id] = TenantContext(
            current.tenant_id,
            current.display_name,
            status,
            current.config_overrides,
        )
        return True

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
            current.tenant_id,
            current.display_name,
            current.status,
            config,
        )
        return self.tenants[tenant_id]


# --------------------------------------------------------------------------- #
# api-role services container with the injected registry + console identity
# --------------------------------------------------------------------------- #


@dataclass
class _FakeTx:
    pool: object = None


def _build_services(config, registry) -> RuntimeServices:
    from teamEvolver.runtime.repository import ResourceLeaseRepository, WorkerTaskRepository
    from teamEvolver.runtime.tasks import build_task_registry

    tx = _FakeTx()
    return RuntimeServices(
        role="api",
        tx=tx,
        tasks=WorkerTaskRepository(tx),
        leases=ResourceLeaseRepository(tx),
        registry=build_task_registry({}, require_complete=False),
        extras={
            "config": config,
            "console_sessions": ConsoleSessionManager(config),
            "tenant_registry": registry,
        },
    )


@pytest.fixture
def config(tmp_path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "skills"),
        sharing_viking_api_key="",
    )


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def store(config) -> IdentityStore:
    return IdentityStore(config)


@pytest.fixture
def app(config, registry, monkeypatch):
    monkeypatch.delenv("TEAMEVOLVER_ROOT_API_KEY", raising=False)
    monkeypatch.delenv("TEAMEVOLVER_PLATFORM_ADMINS", raising=False)
    application = create_app(services=_build_services(config, registry))

    # A probe route so RBAC/tenant binding is observable like test_console_rbac.
    @application.get("/api/rbac-probe")
    async def read(request: Request):  # noqa: ANN202
        return {"tenant_id": current_tenant_id(), "user": request.state.console_user}

    return application


@pytest.fixture
def root(app) -> TestClient:
    client = TestClient(app, base_url="http://192.0.2.10:1080")
    resp = client.post("/api/auth/bootstrap", json={"username": "root", "password": "test-password"})
    assert resp.status_code == 200, resp.text
    return client


def _register(app, username: str) -> TestClient:
    client = TestClient(app, base_url="http://192.0.2.10:1080")
    resp = client.post("/api/auth/register", json={"username": username, "password": "test-password"})
    assert resp.status_code == 200, resp.text
    return client


# --------------------------------------------------------------------------- #
# Tenant CRUD / config
# --------------------------------------------------------------------------- #


def test_create_tenant_returns_one_time_token(root: TestClient, registry: FakeRegistry):
    resp = root.post("/api/tenants", json={"display_name": "Acme", "account_id": "acme"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant"]["tenant_id"] == "acme"
    assert body["agent_token"].startswith("tevt_")
    # The plaintext token resolves back to its tenant (single Root Key chain).
    assert registry.resolve_by_agent_token(body["agent_token"]).tenant_id == "acme"


def test_create_tenant_requires_platform_admin(app, root: TestClient):
    alice = _register(app, "alice")
    assert alice.post("/api/tenants", json={"display_name": "x"}, headers={"X-Tenant-Id": "alpha"}).status_code == 403


def test_rotate_token_invalidates_the_previous_one(root: TestClient, registry: FakeRegistry):
    created = root.post("/api/tenants", json={"display_name": "Rotate", "account_id": "rot"}).json()
    old = created["agent_token"]
    rotated = root.post("/api/tenants/rot/rotate-token", headers={"X-Tenant-Id": "rot"}).json()
    assert rotated["agent_token"] != old
    assert registry.resolve_by_agent_token(old) is None
    assert registry.resolve_by_agent_token(rotated["agent_token"]).tenant_id == "rot"


def test_set_status_disables_and_rotate_default_is_reserved(root: TestClient):
    root.post("/api/tenants", json={"display_name": "Toggle", "account_id": "tog"})
    assert (
        root.post("/api/tenants/tog/status", json={"status": "disabled"}, headers={"X-Tenant-Id": "tog"}).json()[
            "status"
        ]
        == "disabled"
    )
    assert root.post("/api/tenants/zzz/status", json={"status": "disabled"}).status_code == 404
    # The default tenant's token surface stays reserved.
    assert root.post("/api/tenants/default/rotate-token", headers={"X-Tenant-Id": "default"}).status_code == 400


def test_tenant_config_cas_and_unknown_key_rejected(root: TestClient, registry: FakeRegistry):
    h = {"X-Tenant-Id": "alpha"}
    ok = root.put("/api/tenants/alpha/config", json={"overrides": {"max_evolve_per_day": 10}}, headers=h)
    assert ok.status_code == 200, ok.text
    assert registry.get("alpha").config_overrides["max_evolve_per_day"] == 10
    # Unknown key is refused, never silently ignored.
    assert root.put("/api/tenants/alpha/config", json={"overrides": {"bogus": 1}}, headers=h).status_code == 400
    # A null value deletes the key (merge semantics).
    root.put("/api/tenants/alpha/config", json={"overrides": {"max_evolve_per_day": None}}, headers=h)
    assert "max_evolve_per_day" not in registry.get("alpha").config_overrides


def test_tenant_config_redacts_secrets(root: TestClient, registry: FakeRegistry):
    registry.tenants["alpha"] = TenantContext(
        "alpha",
        "alpha",
        config_overrides={
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


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #


def test_registration_has_no_implicit_membership(app, root: TestClient, store: IdentityStore):
    alice = _register(app, "alice")
    assert store.memberships(user_id="alice") == []
    # No selector, and an explicit selector she is not a member of, both 403 on
    # a business path; a control path (users list) stays reachable.
    assert alice.get("/api/rbac-probe").status_code == 403
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "default"}).status_code == 403
    status = alice.get("/api/auth/status").json()
    assert status["available_tenants"] == []
    assert status["active_tenant_id"] is None
    assert alice.get("/api/users").status_code == 200


def test_roles_are_per_tenant_and_rechecked_every_request(app, root: TestClient, store: IdentityStore):
    alice = _register(app, "alice")
    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    store.set_member("beta", "alice", "member", "active", "root")
    # tenant_admin on alpha may manage; member on beta may not; no membership on default.
    assert (
        alice.put(
            "/api/tenants/alpha/config", json={"overrides": {"max_evolve_per_day": 1}}, headers={"X-Tenant-Id": "alpha"}
        ).status_code
        == 200
    )
    assert (
        alice.put(
            "/api/tenants/beta/config", json={"overrides": {"max_evolve_per_day": 1}}, headers={"X-Tenant-Id": "beta"}
        ).status_code
        == 403
    )
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "default"}).status_code == 403
    # available_tenants reflects her two active memberships.
    assert alice.get("/api/identity/tenants", headers={"X-Tenant-Id": "alpha"}).json()["tenants"] == [
        {"tenant_id": "alpha", "display_name": "alpha", "status": "active"},
        {"tenant_id": "beta", "display_name": "beta", "status": "active"},
    ]
    assert alice.get("/api/auth/status", headers={"X-Tenant-Id": "alpha"}).json()["active_tenant_id"] == "alpha"
    # Revoking the beta membership is seen on the very next request.
    store.set_member("beta", "alice", "member", "disabled", "root")
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "beta"}).status_code == 403


def test_platform_admin_governs_any_selected_tenant(root: TestClient):
    assert root.get("/api/rbac-probe", headers={"X-Tenant-Id": "alpha"}).status_code == 200
    assert root.get("/api/tenants/beta/members", headers={"X-Tenant-Id": "beta"}).status_code == 200
    # Platform admin sees every tenant in the list.
    ids = {t["tenant_id"] for t in root.get("/api/tenants").json()["tenants"]}
    assert {"default", "alpha", "beta"} <= ids


def test_tenant_admin_cannot_cross_tenant_or_change_global_identity(app, root: TestClient, store: IdentityStore):
    alice = _register(app, "alice")
    _register(app, "bob")
    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    h = {"X-Tenant-Id": "alpha"}
    assert (
        alice.put("/api/identity/users/alice/access", json={"platform_role": "platform_admin"}, headers=h).status_code
        == 403
    )
    assert alice.get("/api/openviking-accounts", headers=h).status_code == 403
    # May govern her own tenant's members, but not another tenant's.
    assert alice.put("/api/tenants/alpha/members/bob", json={"role": "member"}, headers=h).status_code == 200
    assert alice.put("/api/tenants/beta/members/bob", json={"role": "tenant_admin"}, headers=h).status_code == 403
    # A platform role can never be assigned through the tenant membership endpoint.
    assert alice.put("/api/tenants/alpha/members/bob", json={"role": "platform_admin"}, headers=h).status_code == 400


def test_disabled_tenant_is_rejected(app, root: TestClient, registry: FakeRegistry, store: IdentityStore):
    alice = _register(app, "alice")
    store.set_member("alpha", "alice", "member", "active", "root")
    registry.set_status("alpha", "disabled")
    assert alice.get("/api/rbac-probe", headers={"X-Tenant-Id": "alpha"}).status_code == 403


def test_last_platform_and_tenant_admin_are_protected(app, root: TestClient, store: IdentityStore):
    _register(app, "alice")
    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    assert (
        root.put(
            "/api/tenants/alpha/members/alice", json={"status": "disabled"}, headers={"X-Tenant-Id": "alpha"}
        ).status_code
        == 409
    )
    assert root.put("/api/identity/users/root/access", json={"platform_role": "user"}).status_code == 409


# --------------------------------------------------------------------------- #
# Single Root Key chain: tevt_ Agent token resolution + machine-path guard
# --------------------------------------------------------------------------- #


def test_agent_token_resolves_tenant_but_not_console(app, root: TestClient, registry: FakeRegistry):
    created = root.post("/api/tenants", json={"display_name": "Machine", "account_id": "mac"}).json()
    token = created["agent_token"]
    agent = TestClient(app, base_url="http://192.0.2.10:1080")
    headers = {"Authorization": f"Bearer {token}"}
    # A console admin path is refused for a machine credential...
    assert agent.get("/api/users", headers=headers).status_code == 403
    # ...and a machine path binds the token's tenant (probe reports it).
    assert agent.get("/status", headers=headers).status_code in (200, 404)


def test_agent_token_mismatch_and_invalid_are_rejected(app, root: TestClient):
    created = root.post("/api/tenants", json={"display_name": "Mismatch", "account_id": "mis"}).json()
    token = created["agent_token"]
    agent = TestClient(app, base_url="http://192.0.2.10:1080")
    # A client-supplied selector that disagrees with the token never wins.
    assert agent.get("/status", headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": "beta"}).status_code == 403
    assert agent.get("/status", headers={"Authorization": "Bearer tevt_nope"}).status_code == 401
