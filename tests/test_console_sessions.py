"""Browser login persistence across reloads, deployments and service restarts."""

from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer, routes
from teamEvolver.admin.identity_store import IdentityStore


@pytest.fixture
def session_backend():
    # SQLite exercises transactional authority and shared-worker semantics.
    # PostgreSQL is covered by test_console_identity_pg with a real isolated schema.
    return "sqlite"


def _server(path: Path) -> ProxyServer:
    return ProxyServer(
        TeamEvolverConfig(
            _config_file=str(path / "config.yaml"),
            skills_runtime_cache_dir=str(path / "skills"),
            sharing_skill_mirror_enabled=False,
        )
    )


def _bootstrap(client: TestClient, username: str = "admin") -> None:
    response = client.post(
        "/api/auth/bootstrap",
        json={"username": username, "password": "test-password"},
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is True


def _assert_user(client: TestClient, username: str = "admin") -> None:
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["user"]["id"] == username


@pytest.mark.parametrize("origin", ["http://192.0.2.10:1080", "https://console.example.test"])
def test_remote_login_survives_reload_and_restart(tmp_path, origin, session_backend):
    client = TestClient(_server(tmp_path).app, base_url=origin)
    _bootstrap(client)
    cookie = next(iter(client.cookies.jar))
    assert cookie.expires is not None
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.secure == origin.startswith("https:")
    _assert_user(client)

    restarted = TestClient(_server(tmp_path).app, base_url=origin, cookies=client.cookies)
    _assert_user(restarted)


def test_session_created_by_a_peer_instance_is_recognised(tmp_path, session_backend):
    """Two processes sharing one store must accept each other's logins.

    The peer is started *before* the login, so its in-process cache is stale;
    only the shared-store re-read on a cache miss can recognise the token.
    Without it every console request that lands on the peer 401s with
    "login or tenant token required" and the SPA bounces back to the login
    page (measured on a two-instance deployment).
    """
    origin = "http://192.0.2.10:1080"
    first = TestClient(_server(tmp_path).app, base_url=origin)
    peer = TestClient(_server(tmp_path).app, base_url=origin)
    # One browser jar, two instances behind the same authority.
    browser_cookies = CookieJar()
    first.cookies = browser_cookies
    peer.cookies = browser_cookies
    _bootstrap(first)
    _assert_user(first)
    _assert_user(peer)


def test_parallel_business_requests_share_one_authenticated_session(tmp_path, monkeypatch):
    server = _server(tmp_path)

    @server.app.get("/api/session-probe")
    async def session_probe(request: Request):
        return {"user_id": request.state.console_user["id"]}

    browser = TestClient(server.app)
    _bootstrap(browser)
    clients = [TestClient(server.app, cookies=browser.cookies) for _ in range(3)]
    barrier = threading.Barrier(len(clients))
    load_registry = IdentityStore.load_registry

    def synchronized_load_registry(store):
        data = load_registry(store)
        barrier.wait(timeout=5)
        return data

    monkeypatch.setattr(IdentityStore, "load_registry", synchronized_load_registry)
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        responses = list(pool.map(
            lambda client: client.get(
                "/api/session-probe",
                headers={"X-Tenant-Id": "default"},
            ),
            clients,
        ))

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [response.json()["user_id"] for response in responses] == ["admin"] * 3


def test_login_on_another_port_does_not_replace_existing_login(tmp_path):
    first = TestClient(_server(tmp_path / "first").app, base_url="http://192.0.2.10:1080")
    second = TestClient(_server(tmp_path / "second").app, base_url="http://192.0.2.10:1081")
    # Browsers share host cookies across ports, including HttpOnly cookies.
    browser_cookies = CookieJar()
    first.cookies = browser_cookies
    second.cookies = browser_cookies
    _bootstrap(first, "first-admin")
    _bootstrap(second, "second-admin")

    _assert_user(first, "first-admin")
    _assert_user(second, "second-admin")
    assert second.post("/api/auth/logout").status_code == 200
    _assert_user(first, "first-admin")
    assert second.get("/api/auth/status").json()["authenticated"] is False


def test_legacy_cookie_is_migrated_to_the_scoped_name(tmp_path):
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    token = "legacy-browser-session"
    IdentityStore(server.config).edit_users(
        lambda data: data["users"].append({
            "id": "admin",
            "display_name": "Admin",
            "platform_role": "platform_admin",
            "status": "active",
        }),
        actor="test",
    )
    IdentityStore(server.config).session(
        token,
        value={"user_id": "admin", "expires_at": routes.time.time() + 3600},
    )
    client.cookies.set(
        routes._SESSION_COOKIE,
        token,
        domain="192.0.2.10",
        path="/",
    )

    assert client.get("/api/auth/status").json()["authenticated"] is True
    cookie_names = {cookie.name for cookie in client.cookies.jar}
    assert routes._SESSION_COOKIE not in cookie_names
    assert any(name.startswith(f"{routes._SESSION_COOKIE}_") for name in cookie_names)


def test_active_login_renews_cookie_and_survives_restart(tmp_path, monkeypatch, session_backend):
    now = routes.time.time()
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    _bootstrap(client)

    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: now + routes._SESSION_TTL_SECONDS / 2))
    response = client.get("/api/auth/status")
    assert response.json()["authenticated"] is True
    assert "Max-Age=" in response.headers.get("set-cookie", "")

    # Restart after the original expiry, but before the renewed expiry.
    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: now + routes._SESSION_TTL_SECONDS + 1))
    restarted = TestClient(_server(tmp_path).app, base_url=str(client.base_url), cookies=client.cookies)
    _assert_user(restarted)


def test_login_and_register_issue_persistent_cookies(tmp_path):
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    client.post("/api/auth/logout")
    response = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    _assert_user(client)
    client.post("/api/auth/logout")
    response = client.post("/api/auth/register", json={"username": "alice", "password": "test-password"})
    assert response.status_code == 200
    _assert_user(client, "alice")
    assert all(cookie.expires is not None for cookie in client.cookies.jar)
    restarted = TestClient(_server(tmp_path).app, base_url=str(client.base_url), cookies=client.cookies)
    _assert_user(restarted, "alice")


@pytest.mark.parametrize("enabled", [False, True])
def test_root_key_registration_bypasses_policy_but_keeps_user_validation(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-service-root")
    server = _server(tmp_path)
    server.config.self_registration_enabled = enabled
    client = TestClient(server.app)
    _bootstrap(client)
    client.cookies.clear()
    assert client.get("/api/auth/status").json()["self_registration_enabled"] is enabled
    headers = {"Authorization": "Bearer test-service-root"}
    payload = {"username": "backend-user", "password": "test-password", "role": "admin"}
    response = client.post("/api/auth/register", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "user"
    assert client.post("/api/auth/register", headers=headers, json=payload).status_code == 409
    assert client.post("/api/auth/register", headers=headers, json={}).status_code == 400
    client.cookies.clear()
    assert client.post("/api/auth/login", json=payload).status_code == 200


@pytest.mark.parametrize("root_key", ["", "test-service-root"])
@pytest.mark.parametrize("admin_cookie", [False, True])
@pytest.mark.parametrize("bearer", ["", "invalid-key"])
def test_disabled_registration_requires_verified_root_key(tmp_path, monkeypatch, root_key, admin_cookie, bearer):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", root_key)
    server = _server(tmp_path)
    server.config.self_registration_enabled = False
    client = TestClient(server.app)
    _bootstrap(client)
    if not admin_cookie:
        client.cookies.clear()
    before = IdentityStore(server.config).load_registry()
    response = client.post(
        "/api/auth/register", json={"username": "blocked-user", "password": "test-password"},
        headers={"Authorization": f"Bearer {bearer}", "X-Role": "admin", "X-Service-Root-Authenticated": "true"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "SELF_REGISTRATION_DISABLED"
    assert IdentityStore(server.config).load_registry() == before


def test_idle_session_expires_and_frequent_requests_do_not_rewrite_storage(tmp_path, monkeypatch):
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    expires_at = next(iter(server._console_sessions.values()))["expires_at"]
    writes = []
    save = IdentityStore.session

    def track_save(store, token, **kwargs):
        if kwargs.get("value") is not None:
            writes.append(True)
        return save(store, token, **kwargs)

    monkeypatch.setattr(IdentityStore, "session", track_save)
    for _ in range(3):
        _assert_user(client)
    assert not writes

    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: expires_at + 1))
    assert client.get("/api/auth/status").json()["authenticated"] is False
    assert not server._console_sessions
    assert IdentityStore(server.config).sessions() == {}


def test_logout_during_renewal_does_not_set_the_cookie_again(tmp_path, monkeypatch):
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    now = routes.time.time()
    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: now + routes._SESSION_TTL_SECONDS / 2))
    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert len(client.cookies) == 0
    assert client.get("/api/auth/status").json()["authenticated"] is False
