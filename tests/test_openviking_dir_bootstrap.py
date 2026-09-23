"""OpenViking directory bootstrap (``teamEvolver.proxy.viking_dirs``).

The bootstrap runs on tenant creation and on every sharing-config save, so it
must be idempotent (existing directories are never touched), ordered (a parent
is always created before its children) and fail-open (an unreachable
OpenViking never blocks the caller).
"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.viking_dirs import (
    _dir_uris,
    _expand_ancestors,
    ensure_openviking_dirs,
    registered_personal_users,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeOpenViking:
    """Scripted stand-in for ``httpx.Client`` that records every mkdir."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.existing: set[str] = set()
        self.transport_failures: set[str] = set()
        self.scripted: dict[str, tuple[int, dict]] = {}

    @property
    def mkdir_calls(self) -> list[dict]:
        return [call for call in self.calls if call["url"].endswith("/api/v1/fs/mkdir")]

    def install(self, monkeypatch) -> None:
        owner = self

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args) -> bool:
                return False

            def post(self, url, headers=None, json=None):
                uri = str((json or {}).get("uri") or "")
                owner.calls.append({"url": url, "uri": uri, "headers": dict(headers or {})})
                if uri in owner.transport_failures:
                    raise httpx.ConnectError("transport down")
                if uri in owner.scripted:
                    status_code, payload = owner.scripted[uri]
                    return _FakeResponse(status_code, payload)
                if uri in owner.existing:
                    return _FakeResponse(
                        409,
                        {"error": {"code": "ALREADY_EXISTS", "message": "already exists"}},
                    )
                return _FakeResponse(200, {"status": "ok", "result": {"uri": uri}})

            def request(self, *args, **kwargs):
                # Availability probes (OpenVikingObjectStore) share this client.
                raise httpx.ConnectError("transport down")

        monkeypatch.setattr(httpx, "Client", _Client)


def _config(tmp_path: Path, **overrides) -> TeamEvolverConfig:
    base = dict(
        users_registry_path=str(tmp_path / "users.json"),
        skills_dir=str(tmp_path / "skills"),
        sharing_enabled=True,
        sharing_viking_deployment="local",
        sharing_viking_endpoint="http://ov.test:1933",
        sharing_viking_api_key="root-key",
        sharing_viking_account="acct",
        sharing_skill_mirror_enabled=False,
    )
    base.update(overrides)
    return TeamEvolverConfig(**base)


def _write_registry(tmp_path: Path, users: list[dict]) -> None:
    (tmp_path / "users.json").write_text(
        json.dumps({"users": users}), encoding="utf-8"
    )


def _uris(report: dict) -> list[str]:
    return list(report["created"]) + list(report["existing"])


# --------------------------------------------------------------------- #
# Layout                                                                #
# --------------------------------------------------------------------- #


def test_layout_parents_precede_children_and_are_deduplicated(tmp_path):
    uris = _expand_ancestors(_dir_uris(_config(tmp_path), [("alice", "alice-v")]))

    assert len(uris) == len(set(uris)), "duplicate mkdir for one directory"
    for uri in uris:
        parent = uri.rsplit("/", 1)[0]
        if parent.count("/") >= 3:  # ignore the viking://<scope> roots
            assert parent in uris, f"{parent} missing for {uri}"
            assert uris.index(parent) < uris.index(uri), f"{uri} precedes its parent"

    # Canonical skeleton from docs/zh/concepts/09-storage-layout.md.
    assert "viking://resources/team" in uris
    assert "viking://resources/team-skill-evolver/skills" in uris
    assert "viking://resources/team-skill-evolver/peers/alice/skills" in uris
    assert "viking://resources/shared-knowledge" in uris
    assert "viking://user/team/memories" in uris
    assert "viking://user/team/skills" in uris
    assert "viking://user/alice-v/resources" in uris
    # Scope roots belong to OpenViking itself and are never mkdir'ed.
    assert "viking://resources" not in uris
    assert "viking://user" not in uris


def test_layout_follows_configured_prefixes_and_team_user(tmp_path):
    config = _config(
        tmp_path,
        sharing_viking_root_prefix="a/b",
        aggregation_shared_knowledge_prefix="kb",
        sharing_viking_user="product_agent",
    )
    uris = _expand_ancestors(_dir_uris(config, []))

    assert "viking://resources/a" in uris  # intermediate segment of the prefix
    assert "viking://resources/a/b/skills" in uris
    assert "viking://resources/kb" in uris
    assert "viking://user/product_agent/memories" in uris


# --------------------------------------------------------------------- #
# Registry users                                                        #
# --------------------------------------------------------------------- #


def test_registered_personal_users_split_peer_and_viking_user(tmp_path):
    _write_registry(
        tmp_path,
        [
            {"id": "alice", "personal_space": {"viking_user": "alice-ov"}},
            {"id": "bob", "personal_space": {}},
            {"id": "alice", "personal_space": {"viking_user": "other"}},  # duplicate id
            "not-a-record",
        ],
    )
    assert registered_personal_users(_config(tmp_path)) == [
        ("alice", "alice-ov"),
        ("bob", "bob"),
    ]


def test_registered_personal_users_is_fail_open(tmp_path):
    # A directory where the registry file is expected: unreadable as JSON.
    (tmp_path / "users.json").mkdir()
    assert registered_personal_users(_config(tmp_path)) == []


# --------------------------------------------------------------------- #
# ensure_openviking_dirs                                                #
# --------------------------------------------------------------------- #


def test_existing_directories_are_kept_and_missing_ones_created(tmp_path, monkeypatch):
    fake = _FakeOpenViking()
    fake.existing = {
        "viking://resources/team-skill-evolver",
        "viking://resources/team-skill-evolver/skills",
    }
    fake.install(monkeypatch)

    report = ensure_openviking_dirs(_config(tmp_path))

    assert report["action"] == "ok"
    assert report["errors"] == []
    assert set(report["existing"]) == fake.existing
    assert "viking://resources/team-skill-evolver/peers" in report["created"]
    assert "viking://resources/team-skill-evolver/skills" not in report["created"]
    assert report["checked"] == len(fake.calls)
    # Idempotent by contract: only POST fs/mkdir is ever issued.
    assert {call["url"] for call in fake.calls} == {"http://ov.test:1933/api/v1/fs/mkdir"}


def test_transport_failure_is_reported_not_raised(tmp_path, monkeypatch):
    fake = _FakeOpenViking()
    fake.transport_failures = {"viking://resources/shared-knowledge"}
    fake.install(monkeypatch)

    report = ensure_openviking_dirs(_config(tmp_path))

    assert report["action"] == "partial"
    assert [item["uri"] for item in report["errors"]] == [
        "viking://resources/shared-knowledge"
    ]
    assert "transport error" in report["errors"][0]["error"]
    assert "viking://resources/shared-knowledge" not in _uris(report)
    # A transport failure marks the endpoint unreachable: the loop stops
    # instead of paying the timeout once per remaining directory.
    assert fake.mkdir_calls[-1]["uri"] == "viking://resources/shared-knowledge"
    assert len(fake.mkdir_calls) < report["checked"]


def test_missing_api_key_fails_without_any_request(tmp_path, monkeypatch):
    fake = _FakeOpenViking()
    fake.install(monkeypatch)

    report = ensure_openviking_dirs(_config(tmp_path, sharing_viking_api_key=""))

    assert report["action"] == "failed"
    assert "API key" in report["error"]
    assert fake.mkdir_calls == []


def test_server_side_error_body_on_http_200_is_not_a_success(tmp_path, monkeypatch):
    """OpenViking reports failures in a ``{"status": "error"}`` body too."""
    fake = _FakeOpenViking()
    fake.scripted["viking://resources/team"] = (
        200,
        {"status": "error", "error": {"code": "PERMISSION_DENIED", "message": "forbidden"}},
    )
    fake.install(monkeypatch)

    report = ensure_openviking_dirs(_config(tmp_path))

    assert report["action"] == "partial"
    assert "viking://resources/team" not in report["created"]
    assert report["errors"] == [
        {"uri": "viking://resources/team", "error": "PERMISSION_DENIED: forbidden"}
    ]


def test_headers_follow_account_and_path_owner(tmp_path, monkeypatch):
    fake = _FakeOpenViking()
    fake.install(monkeypatch)

    ensure_openviking_dirs(_config(tmp_path), account_id="tenant-a", extra_users=["admin"])

    by_uri = {call["uri"]: call["headers"] for call in fake.calls}
    team_headers = by_uri["viking://resources/team-skill-evolver/skills"]
    assert team_headers["X-OpenViking-Account"] == "tenant-a"
    assert team_headers["X-OpenViking-User"] == "team"
    assert team_headers["X-OpenViking-Agent"] == "team-skill-evolver"
    assert team_headers["X-API-Key"] == "root-key"

    user_headers = by_uri["viking://user/admin/memories"]
    assert user_headers["X-OpenViking-User"] == "admin"
    assert by_uri["viking://resources/team-skill-evolver/peers/admin/skills"][
        "X-OpenViking-User"
    ] == "team"


def test_extra_users_extend_registry_users(tmp_path, monkeypatch):
    _write_registry(tmp_path, [{"id": "alice", "personal_space": {}}])
    fake = _FakeOpenViking()
    fake.install(monkeypatch)

    ensure_openviking_dirs(_config(tmp_path), extra_users=["admin", "alice"])

    uris = [call["uri"] for call in fake.calls]
    assert "viking://resources/team-skill-evolver/peers/alice/skills" in uris
    assert "viking://resources/team-skill-evolver/peers/admin/skills" in uris
    assert "viking://user/admin/memories" in uris
    # The duplicate id (registry + extra) is not created twice.
    assert uris.count("viking://resources/team-skill-evolver/peers/alice/skills") == 1


def test_unsafe_segments_are_skipped(tmp_path, monkeypatch):
    _write_registry(tmp_path, [{"id": "../evil", "personal_space": {}}])
    fake = _FakeOpenViking()
    fake.install(monkeypatch)

    report = ensure_openviking_dirs(_config(tmp_path))

    assert report["action"] == "ok"
    assert all("evil" not in call["uri"] for call in fake.calls)


@pytest.mark.parametrize("scope_root", ["viking://resources", "viking://user"])
def test_scope_roots_are_never_created(scope_root):
    assert scope_root not in _expand_ancestors([f"{scope_root}/x/y"])
    assert f"{scope_root}/x" in _expand_ancestors([f"{scope_root}/x/y"])


# --------------------------------------------------------------------- #
# Manual route                                                          #
# --------------------------------------------------------------------- #


def _server(tmp_path: Path):
    from teamEvolver.proxy import ProxyServer

    return ProxyServer(
        TeamEvolverConfig(
            users_registry_path=str(tmp_path / "users.json"),
            skills_dir=str(tmp_path / "skills"),
            sharing_enabled=False,
            sharing_skill_mirror_enabled=False,
            sharing_viking_deployment="local",
            sharing_viking_endpoint="http://ov.test:1933",
            sharing_viking_api_key="root-key",
            sharing_viking_account="acct",
            sharing_local_root=str(tmp_path / "local_store"),
        )
    )


def test_manual_endpoint_creates_directories(tmp_path, monkeypatch):
    fake = _FakeOpenViking()
    fake.install(monkeypatch)
    client = TestClient(_server(tmp_path).app, base_url="http://192.0.2.10:1080")
    assert (
        client.post(
            "/api/auth/bootstrap",
            json={"username": "admin", "password": "test-password"},
        ).status_code
        == 200
    )

    response = client.post("/api/sharing-config/bootstrap-dirs", json={})

    assert response.status_code == 200
    report = response.json()["openviking_dirs"]
    assert report["action"] == "ok"
    assert report["account_id"] == "acct"
    assert "viking://resources/team-skill-evolver/skills" in report["created"]
    assert "viking://user/admin/memories" in report["created"]


def test_manual_endpoint_requires_admin(tmp_path, monkeypatch):
    fake = _FakeOpenViking()
    fake.install(monkeypatch)
    client = TestClient(_server(tmp_path).app, base_url="http://192.0.2.10:1080")

    response = client.post("/api/sharing-config/bootstrap-dirs", json={})

    assert response.status_code in {401, 403}
    assert fake.mkdir_calls == []