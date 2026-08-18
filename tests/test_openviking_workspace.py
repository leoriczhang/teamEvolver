from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.openviking_workspace import (
    OpenVikingWorkspaceMixin,
    _expand_cli_workspace_args,
    _normalize_cli_argv,
    _scope_map,
    _scope_regular_cli_search,
    _validate_uri,
    _validate_regular_cli_scope,
)
from teamEvolver.proxy.users_admin import _save_registry
from teamEvolver.proxy.memory_debug import MemoryDebugMixin


class _WorkspaceOwner(OpenVikingWorkspaceMixin, MemoryDebugMixin):
    def __init__(self, config: TeamEvolverConfig) -> None:
        self.config = config


def _user(user_id: str, role: str = "user") -> dict:
    return {
        "id": user_id,
        "display_name": user_id.title(),
        "email": "",
        "role": role,
        "password_hash": "hash",
        "personal_space": {"backend": "viking", "viking_api_key": "personal-key"},
        "team_space": {"backend": "viking", "viking_api_key": "team-key"},
        "created_at": "",
        "updated_at": "",
    }


def _client(tmp_path, *, current: dict, users: list[dict] | None = None):
    registry = tmp_path / "users.json"
    _save_registry(registry, {"users": users or [current]})
    config = TeamEvolverConfig(
        users_registry_path=str(registry),
        sharing_enabled=True,
        sharing_viking_deployment="local",
        sharing_viking_endpoint="http://127.0.0.1:1933",
        sharing_viking_root_prefix="team-skill-evolver",
        sharing_viking_account="workspace-a",
    )
    owner = _WorkspaceOwner(config)
    app = FastAPI()

    @app.middleware("http")
    async def inject_user(request: Request, call_next):
        request.state.console_user = current
        return await call_next(request)

    owner._register_openviking_workspace_routes(app)
    owner._register_memory_debug_routes(app)
    return TestClient(app), owner


def test_scope_map_uses_native_personal_memory_and_shared_team_roots() -> None:
    config = TeamEvolverConfig(
        sharing_viking_root_prefix="team-skill-evolver",
        sharing_viking_user="team-space",
    )
    scopes = _scope_map(
        config,
        "alice",
        is_admin=False,
        personal_user="personal-space",
    )

    assert scopes["personal_memory"].root_uri == "viking://user/personal-space/memories"
    assert scopes["personal_skills"].root_uri == (
        "viking://resources/team-skill-evolver/peers/alice/skills"
    )
    assert scopes["team_memory"].root_uri == (
        "viking://user/team-space/memories"
    )
    assert scopes["team_skills"].root_uri == (
        "viking://resources/team-skill-evolver/skills"
    )
    assert scopes["personal_memory"].can_write is True
    assert scopes["team_memory"].can_write is False


def test_validate_uri_rejects_escape_and_root_mutation() -> None:
    scope = _scope_map(TeamEvolverConfig(), "alice", is_admin=True)["personal_memory"]

    assert _validate_uri(scope, f"{scope.root_uri}/preferences/editor.md").endswith(
        "/preferences/editor.md"
    )
    with pytest.raises(Exception) as outside:
        _validate_uri(scope, "viking://user/bob/memories/private.md")
    assert outside.value.status_code == 403
    with pytest.raises(Exception) as traversal:
        _validate_uri(scope, f"{scope.root_uri}/../skills")
    assert traversal.value.status_code == 400
    with pytest.raises(Exception) as root:
        _validate_uri(scope, scope.root_uri, allow_root=False)
    assert root.value.status_code == 400


def test_workspace_config_exposes_all_scopes_and_local_studio(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, _owner = _client(tmp_path, current=alice)

    response = client.get("/api/openviking/workspace/config?user_id=alice")

    assert response.status_code == 200
    body = response.json()
    assert body["deployment"] == "local"
    assert body["studio_url"] == "http://127.0.0.1:1933/studio/"
    assert body["scopes"]["team_workspace"]["can_write"] is True
    assert set(body["scopes"]) == {
        "personal_memory",
        "team_memory",
        "personal_skills",
        "team_skills",
        "personal_workspace",
        "team_workspace",
    }


def test_workspace_cli_binary_uses_explicit_image_path(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "ov"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("OPENVIKING_CLI_BIN", os.fspath(cli))

    assert OpenVikingWorkspaceMixin._workspace_cli_binary() == os.fspath(cli)


def test_workspace_list_normalizes_openviking_entries(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_request = AsyncMock(
        return_value=[
            {
                "uri": "viking://user/alice/memories/profile.md",
                "isDir": False,
                "size": 12,
            },
            {
                "uri": "viking://user/alice/memories/preferences",
                "isDir": True,
            },
        ]
    )

    response = client.get(
        "/api/openviking/workspace/list",
        params={
            "scope": "personal_memory",
            "user_id": "alice",
            "uri": "viking://user/alice/memories",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [entry["name"] for entry in body["entries"]] == ["preferences", "profile.md"]
    call = owner._workspace_request.await_args
    assert call.args[2:4] == ("GET", "/api/v1/fs/ls")
    assert call.kwargs["params"]["output"] == "original"


def test_workspace_tree_uses_recursive_openviking_tree_api(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_request = AsyncMock(
        return_value=[
            {
                "uri": "viking://user/alice/memories/preferences",
                "isDir": True,
                "rel_path": "preferences",
            },
            {
                "uri": "viking://user/alice/memories/preferences/editor.md",
                "isDir": False,
                "rel_path": "preferences/editor.md",
            },
        ]
    )

    response = client.get(
        "/api/openviking/workspace/tree",
        params={
            "scope": "personal_memory",
            "user_id": "alice",
            "uri": "viking://user/alice/memories",
        },
    )

    assert response.status_code == 200
    assert [item["relative_path"] for item in response.json()["entries"]] == [
        "preferences",
        "preferences/editor.md",
    ]
    call = owner._workspace_request.await_args
    assert call.args[2:4] == ("GET", "/api/v1/fs/tree")
    assert call.kwargs["params"]["node_limit"] == 10_000


def test_workspace_directory_level_reads_l0_and_l1(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_request = AsyncMock(
        side_effect=["Directory summary", "# Directory overview"]
    )

    l0 = client.get(
        "/api/openviking/workspace/level",
        params={
            "scope": "personal_memory",
            "user_id": "alice",
            "uri": "viking://user/alice/memories/preferences",
            "level": "l0",
        },
    )
    l1 = client.get(
        "/api/openviking/workspace/level",
        params={
            "scope": "personal_memory",
            "user_id": "alice",
            "uri": "viking://user/alice/memories/preferences",
            "level": "l1",
        },
    )

    assert l0.status_code == 200
    assert l0.json()["content"] == "Directory summary"
    assert l1.status_code == 200
    assert l1.json()["content"] == "# Directory overview"
    calls = owner._workspace_request.await_args_list
    assert calls[0].args[2:4] == ("GET", "/api/v1/content/abstract")
    assert calls[1].args[2:4] == ("GET", "/api/v1/content/overview")


def test_workspace_directory_level_rejects_unknown_level(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_request = AsyncMock(return_value="")

    response = client.get(
        "/api/openviking/workspace/level",
        params={
            "scope": "personal_memory",
            "user_id": "alice",
            "uri": "viking://user/alice/memories",
            "level": "l2",
        },
    )

    assert response.status_code == 400
    owner._workspace_request.assert_not_awaited()


def test_workspace_cli_parses_native_and_studio_style_commands() -> None:
    current = "viking://resources/team-skill-evolver"

    assert _normalize_cli_argv('ov find "release notes"') == [
        "find",
        "release notes",
    ]
    assert _normalize_cli_argv("/session create demo") == [
        "session",
        "new",
        "demo",
    ]
    assert _expand_cli_workspace_args(["ls"], current) == ["ls", current]
    assert _expand_cli_workspace_args(
        ["find", "agent", "--scope", "."],
        current,
    ) == ["find", "agent", "--uri", current]


def test_regular_user_cli_is_limited_to_selected_scope() -> None:
    scope = _scope_map(
        TeamEvolverConfig(),
        "alice",
        is_admin=False,
    )["personal_memory"]

    _validate_regular_cli_scope(
        ["read", f"{scope.root_uri}/preferences/editor.md"],
        scope,
    )
    with pytest.raises(Exception) as outside:
        _validate_regular_cli_scope(
            ["read", "viking://user/bob/memories/private.md"],
            scope,
        )
    assert outside.value.status_code == 403
    with pytest.raises(Exception) as admin:
        _validate_regular_cli_scope(["admin", "users"], scope)
    assert admin.value.status_code == 403
    with pytest.raises(Exception) as identity:
        _validate_regular_cli_scope(["--user", "bob", "status"], scope)
    assert identity.value.status_code == 403
    assert _scope_regular_cli_search(
        ["find", "agent"],
        scope.root_uri,
    ) == ["find", "agent", "--uri", scope.root_uri]


def test_workspace_cli_route_runs_with_current_scope(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_cli = AsyncMock(
        return_value={
            "ok": True,
            "exit_code": 0,
            "command": ["ov", "ls"],
            "stdout": "{}",
            "stderr": "",
            "truncated": False,
        }
    )

    response = client.post(
        "/api/openviking/workspace/cli",
        json={
            "scope": "personal_memory",
            "user_id": "alice",
            "current_uri": "viking://user/alice/memories",
            "command": "/ls",
        },
    )

    assert response.status_code == 200
    call = owner._workspace_cli.await_args
    assert call.args[2] == ["ls", "viking://user/alice/memories"]
    assert call.kwargs["is_admin"] is True


def test_regular_user_cannot_write_team_memory(tmp_path) -> None:
    alice = _user("alice")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_request = AsyncMock(return_value={})

    response = client.post(
        "/api/openviking/workspace/content",
        json={
            "scope": "team_memory",
            "user_id": "alice",
            "uri": "viking://user/default/memories/shared.md",
            "content": "shared",
        },
    )

    assert response.status_code == 403
    owner._workspace_request.assert_not_awaited()


def test_admin_write_uses_openviking_content_api_and_team_key(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)
    owner._workspace_request = AsyncMock(return_value={"uri": "saved"})

    response = client.post(
        "/api/openviking/workspace/content",
        json={
            "scope": "team_memory",
            "user_id": "alice",
            "uri": "viking://user/default/memories/shared.md",
            "content": "shared memory",
            "mode": "create",
        },
    )

    assert response.status_code == 200
    call = owner._workspace_request.await_args
    assert call.args[2:4] == ("POST", "/api/v1/content/write")
    assert call.kwargs["json"] == {
        "uri": "viking://user/default/memories/shared.md",
        "content": "shared memory",
        "mode": "create",
        "wait": False,
    }
    scope = _scope_map(owner.config, "alice", is_admin=True)["team_memory"]
    headers = owner._workspace_headers(alice, scope)
    assert headers["X-API-Key"] == "team-key"
    assert headers["X-OpenViking-Account"] == "workspace-a"
    assert headers["X-OpenViking-User"] == "default"


def test_personal_workspace_inherits_team_key_when_personal_key_is_unset(tmp_path) -> None:
    alice = _user("alice", "admin")
    alice["personal_space"].pop("viking_api_key")
    _client(tmp_path, current=alice)
    owner = _WorkspaceOwner(TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        sharing_viking_endpoint="http://127.0.0.1:1933",
    ))
    scope = _scope_map(owner.config, "alice", is_admin=True)["personal_memory"]

    headers = owner._workspace_headers(alice, scope)

    assert headers["X-API-Key"] == "team-key"


def test_memory_debug_searches_personal_and_team_with_agent_budget(tmp_path) -> None:
    alice = _user("alice", "admin")
    client, owner = _client(tmp_path, current=alice)

    async def search(_user, scope, _method, path, **_kwargs):
        assert path == "/api/v1/search/search"
        if scope.name == "personal_memory":
            return {"items": [{
                "uri": "viking://user/alice/memories/profile.md",
                "name": "profile.md",
                "abstract": "偏好使用中文回答",
                "score": 0.95,
            }]}
        return {"items": [{
            "uri": "viking://user/default/memories/team-rule.md",
            "name": "team-rule.md",
            "abstract": "团队发布需要 True Replay",
            "score": 0.88,
        }]}

    owner._workspace_request = AsyncMock(side_effect=search)
    response = client.post(
        "/api/openviking/memory/debug",
        json={
            "user_id": "alice",
            "query": "发布偏好",
            "max_items": 12,
            "max_chars": 16000,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["scope"] for item in body["items"]] == [
        "personal_memory",
        "team_memory",
    ]
    assert body["budget"]["used_items"] == 2
    assert "偏好使用中文回答" in body["agent_context"]
    assert "团队发布需要 True Replay" in body["agent_context"]
    assert "viking://" not in body["agent_context"]
    assert body["items"][0]["path_alias"].startswith("personal_memory:")
