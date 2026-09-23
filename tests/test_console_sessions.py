"""Browser login persistence across reloads, deployments and service restarts."""

from dataclasses import replace
from http.cookiejar import CookieJar
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer, routes
from teamEvolver.storage import admin_kv


@pytest.fixture(params=["file", "postgres"])
def session_backend(request, monkeypatch):
    if request.param == "file":
        return
    # Exercise the production PG persistence path, with only the object
    # transport replaced. User registries stay in isolated local files.
    objects = {}

    def get_object(key):
        if key not in objects:
            raise FileNotFoundError(key)
        return BytesIO(objects[key])

    store = SimpleNamespace(get_object=get_object, put_object=lambda key, payload: objects.update({key: payload}))
    monkeypatch.setattr(
        admin_kv, "_pg_store", lambda config, tenant_id: store if config.storage_pg_enabled else None,
    )
    load, save = routes._load_console_sessions, routes._save_console_sessions
    monkeypatch.setattr(routes, "_load_console_sessions", lambda config: load(replace(config, storage_pg_enabled=True)))
    monkeypatch.setattr(
        routes, "_save_console_sessions",
        lambda config, sessions: save(replace(config, storage_pg_enabled=True), sessions),
    )


def _server(path: Path) -> ProxyServer:
    return ProxyServer(
        TeamEvolverConfig(
            users_registry_path=str(path / "users.json"),
            skills_dir=str(path / "skills"),
            sharing_enabled=False,
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


def test_legacy_cookie_migrates_and_logout_stays_logged_out(tmp_path, session_backend):
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    cookie = next(iter(client.cookies.jar))
    token = cookie.value
    client.cookies.clear()
    client.cookies.set(routes._SESSION_COOKIE, token, domain=cookie.domain, path="/")
    _assert_user(client)
    assert routes._SESSION_COOKIE not in client.cookies
    assert len(client.cookies) == 1

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is False
    restarted = TestClient(_server(tmp_path).app, base_url=str(client.base_url))
    restarted.cookies.set(cookie.name, token, domain=cookie.domain, path="/")
    assert restarted.get("/api/auth/status").json()["authenticated"] is False


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


def test_idle_session_expires_and_frequent_requests_do_not_rewrite_storage(tmp_path, monkeypatch):
    server = _server(tmp_path)
    client = TestClient(server.app, base_url="http://192.0.2.10:1080")
    _bootstrap(client)
    expires_at = next(iter(server._console_sessions.values()))["expires_at"]
    writes = []
    save = routes._save_console_sessions

    def track_save(config, sessions):
        writes.append(True)
        save(config, sessions)

    monkeypatch.setattr(routes, "_save_console_sessions", track_save)
    for _ in range(3):
        _assert_user(client)
    assert not writes

    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: expires_at + 1))
    assert client.get("/api/auth/status").json()["authenticated"] is False
    assert not server._console_sessions
    assert routes._load_console_sessions(server.config) == {}


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
