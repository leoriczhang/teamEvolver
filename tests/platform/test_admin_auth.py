"""Console bootstrap, login sessions, user administration and trusted access."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from teamEvolver.admin.console_session import ConsoleSessionManager
from teamEvolver.app import create_app
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.runtime.services import RuntimeServices

# --------------------------------------------------------------------------- #
# Minimal api-role services container (admin router only needs config+sessions)
# --------------------------------------------------------------------------- #


@dataclass
class _FakeTx:
    pool: object = None


def _build_api_services(config) -> RuntimeServices:
    """An api-role services container carrying the console identity in extras.

    The admin router consumes ``extras['config']`` and
    ``extras['console_sessions']``; it never touches ``tasks``/``leases``, so a
    stub transaction manager is sufficient here. Job APIs have their own tests.
    """
    from teamEvolver.runtime.repository import ResourceLeaseRepository, WorkerTaskRepository
    from teamEvolver.runtime.tasks import build_task_registry

    tx = _FakeTx()
    return RuntimeServices(
        role="api",
        tx=tx,
        tasks=WorkerTaskRepository(tx),
        leases=ResourceLeaseRepository(tx),
        registry=build_task_registry({}, require_complete=False),
        extras={"config": config, "console_sessions": ConsoleSessionManager(config)},
    )


@pytest.fixture
def config(tmp_path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "skills"),
    )


@pytest.fixture
def client(config) -> TestClient:
    app = create_app(services=_build_api_services(config))
    return TestClient(app, base_url="http://192.0.2.10:1080")


def _bootstrap(client: TestClient, username: str = "admin", password: str = "test-password") -> None:
    resp = client.post("/api/auth/bootstrap", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    assert resp.json()["authenticated"] is True


# --------------------------------------------------------------------------- #
# Auth + console session
# --------------------------------------------------------------------------- #


def test_status_reports_needs_setup_before_bootstrap(client: TestClient):
    body = client.get("/api/auth/status").json()
    assert body["authenticated"] is False
    assert body["needs_setup"] is True


def test_bootstrap_then_status_authenticated(client: TestClient):
    _bootstrap(client)
    body = client.get("/api/auth/status").json()
    assert body["authenticated"] is True
    assert body["user"]["id"] == "admin"
    assert body["user"]["platform_role"] == "platform_admin"
    assert body["needs_setup"] is False


def test_bootstrap_is_single_shot(client: TestClient):
    _bootstrap(client)
    # A second bootstrap without platform admin context is refused.
    client.cookies.clear()
    assert client.post("/api/auth/bootstrap", json={"username": "x", "password": "y"}).status_code == 409


def test_login_logout_cycle(client: TestClient):
    _bootstrap(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is False
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
    assert resp.status_code == 200
    assert client.get("/api/auth/status").json()["user"]["id"] == "admin"


def test_login_rejects_bad_credentials(client: TestClient):
    _bootstrap(client)
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401


def test_session_recognised_by_a_peer_replica(config):
    """Two apps over one identity store accept each other's logins (shared-store
    authoritative — no process-unique session cache)."""
    first = TestClient(create_app(services=_build_api_services(config)), base_url="http://192.0.2.10:1080")
    peer = TestClient(create_app(services=_build_api_services(config)), base_url="http://192.0.2.10:1080")
    from http.cookiejar import CookieJar

    jar = CookieJar()
    first.cookies = jar
    peer.cookies = jar
    _bootstrap(first)
    assert peer.get("/api/auth/status").json()["authenticated"] is True


def test_register_respects_self_registration_flag(config):
    config.self_registration_enabled = False
    client = TestClient(create_app(services=_build_api_services(config)), base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    client.cookies.clear()
    resp = client.post("/api/auth/register", json={"username": "bob", "password": "test-password"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "SELF_REGISTRATION_DISABLED"


def test_register_when_enabled_creates_a_plain_user(client: TestClient):
    _bootstrap(client)
    client.post("/api/auth/logout")
    resp = client.post("/api/auth/register", json={"username": "alice", "password": "test-password"})
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "user"
    assert client.get("/api/auth/status").json()["user"]["id"] == "alice"


def test_service_root_key_bypass(config, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "svc-root")
    config.self_registration_enabled = False
    client = TestClient(create_app(services=_build_api_services(config)), base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    client.cookies.clear()
    headers = {"Authorization": "Bearer svc-root"}
    resp = client.post("/api/auth/register", headers=headers, json={"username": "backend", "password": "test-password"})
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "user"


# --------------------------------------------------------------------------- #
# Login gate (authorizer parity)
# --------------------------------------------------------------------------- #


def test_business_route_requires_login(client: TestClient):
    # No session -> the injected authorizer blocks a non-public route.
    assert client.get("/api/users").status_code == 401


def test_health_route_is_public(client: TestClient):
    assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- #
# Users CRUD + RBAC
# --------------------------------------------------------------------------- #


def test_platform_admin_lists_and_creates_users(client: TestClient):
    _bootstrap(client)
    created = client.post("/api/users", json={"id": "carol", "display_name": "Carol"})
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "carol"
    ids = {u["id"] for u in client.get("/api/users").json()["users"]}
    assert {"admin", "carol"} <= ids


def test_non_admin_cannot_create_users(client: TestClient):
    _bootstrap(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"username": "dave", "password": "test-password"})
    # dave is a plain user: creating users is platform-admin only.
    assert client.post("/api/users", json={"id": "x"}).status_code == 403
    # ...and only sees themselves in the listing.
    ids = {u["id"] for u in client.get("/api/users").json()["users"]}
    assert ids == {"dave"}


def test_delete_user(client: TestClient):
    _bootstrap(client)
    client.post("/api/users", json={"id": "erin"})
    assert client.delete("/api/users/erin").json()["deleted"] is True
    ids = {u["id"] for u in client.get("/api/users").json()["users"]}
    assert "erin" not in ids


# --------------------------------------------------------------------------- #
# OpenViking accounts (stubbed admin HTTP)
# --------------------------------------------------------------------------- #


def test_openviking_accounts_requires_platform_admin(client: TestClient):
    _bootstrap(client)
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"username": "frank", "password": "test-password"})
    assert client.get("/api/openviking-accounts").status_code == 403


def test_openviking_accounts_fallback_when_unreachable(client: TestClient, config):
    _bootstrap(client)
    # No endpoint/key configured -> fail-open fallback to the configured account.
    body = client.get("/api/openviking-accounts").json()
    assert config.sharing_viking_account in body["accounts"]
    assert body["source"] in {"fallback", "trusted-local", "openviking"}


# --------------------------------------------------------------------------- #
# Skill publish requests
# --------------------------------------------------------------------------- #


def test_publish_requests_listing_is_scoped(client: TestClient):
    _bootstrap(client)
    # Admin sees the (empty) list with a pending count. A business (non-control)
    # path requires an explicit tenant selector (baseline membership
    # semantics); the single-tenant deployment exposes the ``default`` tenant.
    body = client.get("/api/skill-publish-requests", headers={"X-Tenant-Id": "default"}).json()
    assert body["requests"] == []
    assert body["pending_count"] == 0
