"""Tests for the OpenViking account user browse + import feature.

Covers three layers:

* :func:`teamEvolver.proxy.users_admin.list_openviking_account_users` — the
  fail-open ROOT-API proxy that reflects an account's existing users.
* :func:`teamEvolver.proxy.users_admin.import_openviking_account_users` — the
  additive, idempotent import into the local registry.
* The admin-only ``/api/openviking-accounts/{account}/users`` and
  ``/import-users`` routes wired through a real :class:`ProxyServer`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.proxy import users_admin
from teamEvolver.skills.manager import SkillManager


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """Minimal stand-in for ``httpx.Client`` used as a context manager."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.requested_url = ""

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.requested_url = url
        return self._response

    def post(self, url: str, headers=None, json=None) -> _FakeResponse:  # noqa: A002
        return _FakeResponse(200, {"status": "ok", "result": {}})


def _config(tmp_path: Path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_viking_endpoint="http://openviking.local",
        sharing_viking_api_key="root-key",
        sharing_viking_account="acme",
    )


def _install_users_response(monkeypatch, users: list[dict[str, Any]]) -> None:
    response = _FakeResponse(200, {"status": "ok", "result": users})
    monkeypatch.setattr(
        users_admin.httpx,
        "Client",
        lambda *args, **kwargs: _FakeClient(response),
    )


# --------------------------------------------------------------------------- #
# list_openviking_account_users                                                #
# --------------------------------------------------------------------------- #


def test_list_account_users_maps_roles(tmp_path: Path, monkeypatch) -> None:
    _install_users_response(
        monkeypatch,
        [
            {"user_id": "carol", "role": "user"},
            {"user_id": "dave", "role": "admin"},
            {"user_id": "root-user", "role": "root"},
            {"user_id": "", "role": "user"},  # skipped: empty id
        ],
    )
    result = users_admin.list_openviking_account_users(_config(tmp_path), "acme")

    assert result["source"] == "openviking"
    assert result["account"] == "acme"
    by_id = {row["user_id"]: row for row in result["users"]}
    assert by_id["carol"]["role"] == "user"
    assert by_id["dave"]["role"] == "admin"
    # OpenViking root maps to a teamEvolver admin, but the raw role is kept.
    assert by_id["root-user"]["role"] == "admin"
    assert by_id["root-user"]["openviking_role"] == "root"
    assert "" not in by_id
    # Sorted lexicographically by user_id.
    assert [row["user_id"] for row in result["users"]] == ["carol", "dave", "root-user"]


def test_list_account_users_fail_open_without_credentials(tmp_path: Path) -> None:
    config = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"))
    result = users_admin.list_openviking_account_users(config, "acme")

    assert result["users"] == []
    assert result["source"] == "fallback"
    assert "error" in result


def test_list_account_users_fail_open_on_http_error(tmp_path: Path, monkeypatch) -> None:
    response = _FakeResponse(500, "boom")
    monkeypatch.setattr(
        users_admin.httpx,
        "Client",
        lambda *args, **kwargs: _FakeClient(response),
    )
    result = users_admin.list_openviking_account_users(_config(tmp_path), "acme")

    assert result["users"] == []
    assert result["source"] == "fallback"
    assert result["error"].startswith("HTTP 500")


# --------------------------------------------------------------------------- #
# import_openviking_account_users                                              #
# --------------------------------------------------------------------------- #


def test_import_is_additive_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _install_users_response(
        monkeypatch,
        [
            {"user_id": "carol", "role": "user"},
            {"user_id": "dave", "role": "admin"},
        ],
    )
    # Seed an existing local user whose role must not be reset by import.
    path = users_admin._registry_path(config)
    users_admin._save_registry(
        path,
        {"users": [{"id": "carol", "role": "admin", "created_at": "seed", "updated_at": "seed"}]},
    )

    report = users_admin.import_openviking_account_users(
        config, "acme", ["carol", "dave", "ghost"]
    )
    assert report["imported"] == ["dave"]
    assert report["skipped_existing"] == ["carol"]
    assert report["missing"] == ["ghost"]

    data = users_admin._load_registry(path)
    by_id = {u["id"]: u for u in data["users"]}
    # carol keeps her original admin role (not overwritten by the OV user role).
    assert by_id["carol"]["role"] == "admin"
    assert by_id["carol"]["created_at"] == "seed"
    # dave imported with the OpenViking admin role.
    assert by_id["dave"]["role"] == "admin"

    # Re-running the same import changes nothing new.
    again = users_admin.import_openviking_account_users(config, "acme", ["carol", "dave"])
    assert again["imported"] == []
    assert sorted(again["skipped_existing"]) == ["carol", "dave"]


# --------------------------------------------------------------------------- #
# Routes via ProxyServer + TestClient                                          #
# --------------------------------------------------------------------------- #


def _make_server(tmp_path: Path) -> ProxyServer:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    config = _config(tmp_path)
    config.skills_dir = str(skills_dir)
    manager = SkillManager(str(skills_dir))
    return ProxyServer(config, skill_manager=manager)


def _admin_client(server: ProxyServer) -> TestClient:
    client = TestClient(server.app)
    resp = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "display_name": "Admin", "password": "pw123456"},
    )
    assert resp.status_code == 200
    return client


def test_routes_require_admin(tmp_path: Path, monkeypatch) -> None:
    _install_users_response(monkeypatch, [{"user_id": "carol", "role": "user"}])
    server = _make_server(tmp_path)
    client = _admin_client(server)

    # As admin, the listing works and carol is not yet imported locally.
    listing = client.get("/api/openviking-accounts/acme/users")
    assert listing.status_code == 200
    body = listing.json()
    assert body["users"][0]["user_id"] == "carol"
    assert body["users"][0]["imported"] is False

    # Registering carol logs the session in as her (a regular, non-admin user).
    client.post("/api/auth/register", json={"username": "carol", "password": "pw"})

    # A regular (non-admin) session must be rejected on both routes.
    assert client.get("/api/openviking-accounts/acme/users").status_code == 403
    forbidden = client.post(
        "/api/openviking-accounts/acme/import-users",
        json={"user_ids": ["carol"]},
    )
    assert forbidden.status_code == 403


def test_import_route_creates_new_users(tmp_path: Path, monkeypatch) -> None:
    _install_users_response(
        monkeypatch,
        [
            {"user_id": "carol", "role": "user"},
            {"user_id": "dave", "role": "admin"},
        ],
    )
    server = _make_server(tmp_path)
    client = _admin_client(server)

    resp = client.post(
        "/api/openviking-accounts/acme/import-users",
        json={"user_ids": ["carol", "dave"]},
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == ["carol", "dave"]

    users = client.get("/api/users").json()["users"]
    ids = {u["id"] for u in users}
    assert {"carol", "dave"}.issubset(ids)


def test_import_route_rejects_non_list_body(tmp_path: Path, monkeypatch) -> None:
    _install_users_response(monkeypatch, [])
    server = _make_server(tmp_path)
    client = _admin_client(server)

    resp = client.post(
        "/api/openviking-accounts/acme/import-users",
        json={"user_ids": "carol"},
    )
    assert resp.status_code == 400
