from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations.agent_registry import (
    issue_agent_access_token,
    register_agent,
)
from teamEvolver.integrations.context_workspace import (
    ContextStateStore,
    verify_context_usage,
)
from teamEvolver.proxy.agent_context import AgentContextMixin
from teamEvolver.proxy.openviking_workspace import OpenVikingWorkspaceMixin
from teamEvolver.proxy.users_admin import _save_registry


class _ContextOwner(AgentContextMixin, OpenVikingWorkspaceMixin):
    def __init__(self, config: TeamEvolverConfig) -> None:
        self.config = config


def _agent_payload(agent_id: str = "demo:tenant-a") -> dict:
    return {
        "schema_version": "teamevolver.agent-registration.v1",
        "protocol_version": "1.0",
        "agent_id": agent_id,
        "runtime_type": "demo",
        "capabilities": [
            "session.ingest.v1",
            "context.workspace.v1",
            "memory.personal.write.v1",
        ],
    }


def _client(tmp_path):
    users_path = tmp_path / "users.json"
    _save_registry(
        users_path,
        {
            "users": [
                {
                    "id": "alice",
                    "display_name": "Alice",
                    "role": "user",
                    "agent_identities": {},
                    "agent_subjects": [
                        {
                            "integration_id": "demo:tenant-a",
                            "runtime_type": "demo",
                            "external_subject": "external-alice",
                        }
                    ],
                    "personal_space": {
                        "backend": "viking",
                        "viking_user": "alice-private",
                        "viking_api_key": "personal-key",
                    },
                    "team_space": {
                        "backend": "viking",
                        "viking_api_key": "team-key",
                    },
                }
            ]
        },
    )
    config = TeamEvolverConfig(
        users_registry_path=str(users_path),
        sharing_enabled=True,
        sharing_viking_deployment="local",
        sharing_viking_endpoint="http://127.0.0.1:1933",
        sharing_viking_root_prefix="team-skill-evolver",
        sharing_viking_account="workspace-a",
        sharing_viking_user="team-user",
    )
    register_agent(config, _agent_payload())
    _record, token = issue_agent_access_token(
        config,
        agent_id="demo:tenant-a",
    )
    owner = _ContextOwner(config)
    app = FastAPI()
    owner._register_agent_context_routes(app)
    return TestClient(app), owner, token, config


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_describe_requires_exact_subject_mapping(tmp_path) -> None:
    client, _owner, token, _config = _client(tmp_path)

    response = client.get(
        "/internal/agents/context/describe",
        params={"external_subject": "external-alice"},
        headers=_headers(token),
    )
    unmapped = client.get(
        "/internal/agents/context/describe",
        params={"external_subject": "alice"},
        headers=_headers(token),
    )

    assert response.status_code == 200
    assert set(response.json()["scopes"]) == {
        "personal_memory",
        "team_memory",
        "personal_skills",
        "team_skills",
        "personal_resources",
        "team_resources",
    }
    assert response.json()["scopes"]["team_memory"]["operations"] == [
        "resolve",
        "read",
    ]
    assert unmapped.status_code == 403
    assert unmapped.json()["detail"] == "SUBJECT_NOT_MAPPED"


def test_resolve_returns_opaque_refs_and_read_uses_bound_uri(tmp_path) -> None:
    client, owner, token, config = _client(tmp_path)

    async def fake_request(_user, scope, method, path, **kwargs):
        if path == "/api/v1/search/search":
            root = kwargs["json"]["target_uri"]
            return [
                {
                    "uri": f"{root}/result.md",
                    "name": f"{scope.name}-result",
                    "abstract": f"{scope.name} summary",
                    "overview": f"{scope.name} overview",
                    "sha256": f"sha-{scope.name}",
                }
            ]
        if path == "/api/v1/content/read":
            return {"content": "private expanded memory"}
        raise AssertionError((method, path, kwargs))

    owner._workspace_request = AsyncMock(side_effect=fake_request)
    response = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "integration_id": "demo:tenant-a",
            "external_subject": "external-alice",
            "context_session_id": "ctx-session",
            "query": "editor preferences",
            "scopes": ["personal_memory", "team_memory"],
            "max_items": 4,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "teamevolver.context-result.v1"
    assert len(body["items"]) == 2
    serialized = json.dumps(body, ensure_ascii=False)
    assert "viking://user/alice-private" not in serialized
    assert all(item["context_ref"].startswith("ctx_") for item in body["items"])
    snapshot = ContextStateStore(config).load_snapshot(
        body["snapshot_id"],
        agent_id="demo:tenant-a",
        user_id="alice",
    )
    assert snapshot is not None
    assert snapshot["manifest_hash"]

    personal = next(
        item for item in body["items"] if item["scope"] == "personal_memory"
    )
    read = client.post(
        "/internal/agents/context/read",
        headers=_headers(token),
        json={"context_ref": personal["context_ref"], "level": "full"},
    )

    assert read.status_code == 200
    assert read.json()["content"] == "private expanded memory"
    content_call = owner._workspace_request.await_args_list[-1]
    assert content_call.args[3] == "/api/v1/content/read"
    assert content_call.kwargs["params"]["uri"].startswith(
        "viking://user/alice-private/memories/"
    )
    updated_snapshot = ContextStateStore(config).load_snapshot(
        body["snapshot_id"],
        agent_id="demo:tenant-a",
        user_id="alice",
    )
    personal_snapshot = next(
        item
        for item in updated_snapshot["items"]
        if item["scope"] == "personal_memory"
    )
    assert personal_snapshot["expanded"]["full"] == "private expanded memory"


def test_resolve_interleaves_scopes_before_applying_global_item_limit(
    tmp_path,
) -> None:
    client, owner, token, _config = _client(tmp_path)

    async def fake_request(_user, scope, method, path, **kwargs):
        assert path == "/api/v1/search/search"
        root = kwargs["json"]["target_uri"]
        return [
            {
                "uri": f"{root}/result-{index}.md",
                "name": f"{scope.name}-result-{index}",
                "abstract": f"{scope.name} summary {index}",
            }
            for index in range(4)
        ]

    owner._workspace_request = AsyncMock(side_effect=fake_request)
    response = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "integration_id": "demo:tenant-a",
            "external_subject": "external-alice",
            "query": "shared release practices",
            "scopes": ["personal_memory", "team_memory"],
            "max_items": 4,
        },
    )

    assert response.status_code == 200
    scopes = [item["scope"] for item in response.json()["items"]]
    assert scopes == [
        "personal_memory",
        "team_memory",
        "personal_memory",
        "team_memory",
    ]


def _skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"


def _skill_search_owner(tmp_path, skills, mtimes=None):
    """Wire an owner whose search returns file-level skill hits.

    ``skills`` maps scope name -> list of ``(slug, name, description)`` so a
    single logical skill can surface as several file hits (SKILL.md plus
    versioned copies), exactly like OpenViking semantic search does.
    ``mtimes`` optionally maps scope name -> ISO modTime returned by fs/stat,
    letting a test drive latest-wins deterministically.
    """
    client, owner, token, config = _client(tmp_path)
    mtimes = mtimes or {}
    bodies: dict[str, str] = {}
    per_scope_hits: dict[str, list[dict]] = {}
    for scope_name, entries in skills.items():
        hits: list[dict] = []
        for slug, name, description in entries:
            for rel in ("SKILL.md", "versions/v1/SKILL.md"):
                uri = f"{{scope_root}}/{slug}/{rel}"
                hits.append({"uri": uri, "abstract": f"{name} abstract"})
            bodies[slug] = _skill_md(name, description)
        per_scope_hits[scope_name] = hits

    async def fake_request(_user, scope, method, path, **kwargs):
        if path == "/api/v1/search/search":
            root = scope.root_uri.rstrip("/")
            return [
                {**hit, "uri": hit["uri"].format(scope_root=root)}
                for hit in per_scope_hits.get(scope.name, [])
            ]
        if path == "/api/v1/fs/stat":
            return {"modTime": mtimes.get(scope.name, "")}
        if path in {
            "/api/v1/content/read",
            "/api/v1/content/abstract",
            "/api/v1/content/overview",
        }:
            uri = str(kwargs.get("params", {}).get("uri") or "")
            for slug, body in bodies.items():
                if f"/{slug}/" in uri or uri.endswith(f"/{slug}"):
                    return {"content": body}
            return {"content": ""}
        raise AssertionError((method, path, kwargs))

    owner._workspace_request = AsyncMock(side_effect=fake_request)
    return client, owner, token, config


def test_resolve_uses_skill_root_identity_not_filename(tmp_path) -> None:
    client, _owner, token, _config = _skill_search_owner(
        tmp_path,
        {
            "team_skills": [
                ("frontend-design", "frontend-design", "Craft distinct UIs."),
                ("html-ppt-methodology", "html-ppt-methodology", "Build decks."),
            ]
        },
    )

    response = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "query": "frontend design and decks",
            "scopes": ["team_skills"],
            "max_items": 12,
        },
    )

    assert response.status_code == 200
    skills = [item for item in response.json()["items"] if item["kind"] == "skill"]
    # Two distinct skills, each surfaced once by its root slug — never by the
    # shared ``SKILL.md`` filename, and never shadowing one another.
    assert {item["title"] for item in skills} == {
        "frontend-design",
        "html-ppt-methodology",
    }
    assert all(item["selected"] is True for item in skills)
    assert all("shadowed_by" not in item for item in skills)
    assert {item["qualified_skill_id"] for item in skills} == {
        "team:frontend-design",
        "team:html-ppt-methodology",
    }
    for item in skills:
        assert item["context_ref"]


def test_resolve_ignores_hidden_root_metadata_files(tmp_path) -> None:
    client, owner, token, _config = _client(tmp_path)

    async def fake_request(_user, scope, method, path, **kwargs):
        root = scope.root_uri.rstrip("/")
        if path == "/api/v1/search/search":
            # A real skill plus root-level hidden metadata files that search
            # also returns — these must never be treated as skills.
            return [
                {"uri": f"{root}/.overview.md", "abstract": "root overview"},
                {"uri": f"{root}/.abstract.md", "abstract": "root abstract"},
                {"uri": f"{root}/frontend-design/SKILL.md", "abstract": "fd"},
            ]
        if path == "/api/v1/fs/stat":
            return {"modTime": ""}
        return {"content": _skill_md("frontend-design", "Craft distinct UIs.")}

    owner._workspace_request = AsyncMock(side_effect=fake_request)
    response = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "query": "frontend",
            "scopes": ["team_skills"],
            "max_items": 12,
        },
    )

    assert response.status_code == 200
    skills = [item for item in response.json()["items"] if item["kind"] == "skill"]
    assert {item["title"] for item in skills} == {"frontend-design"}
    assert not any(item["title"].startswith(".") for item in skills)


def test_resolve_dedupes_same_skill_across_scopes_latest_wins(tmp_path) -> None:
    client, _owner, token, _config = _skill_search_owner(
        tmp_path,
        {
            "personal_skills": [
                ("frontend-design", "frontend-design", "Craft distinct UIs."),
            ],
            "team_skills": [
                ("frontend-design", "frontend-design", "Craft distinct UIs."),
            ],
        },
    )

    response = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "query": "frontend design",
            "scopes": ["personal_skills", "team_skills"],
            "max_items": 12,
        },
    )

    assert response.status_code == 200
    body = response.json()
    skills = [item for item in body["items"] if item["kind"] == "skill"]
    selected = [item for item in skills if item["selected"]]
    shadowed = [item for item in skills if not item["selected"]]
    # A duplicate (same name + description) resolves to exactly one selected
    # skill; the older team copy is shadowed by the latest personal one.
    assert len(selected) == 1
    assert selected[0]["scope"] == "personal_skills"
    assert shadowed and shadowed[0]["scope"] == "team_skills"
    assert shadowed[0]["shadowed_by"] == "personal:frontend-design"
    warnings = body.get("warnings") or []
    assert any(
        w.get("code") == "DUPLICATE_SKILL"
        and w.get("skill_name") == "frontend-design"
        for w in warnings
    )


def test_resolve_duplicate_latest_team_copy_wins_over_personal(tmp_path) -> None:
    client, _owner, token, _config = _skill_search_owner(
        tmp_path,
        {
            "personal_skills": [
                ("frontend-design", "frontend-design", "Craft distinct UIs."),
            ],
            "team_skills": [
                ("frontend-design", "frontend-design", "Craft distinct UIs."),
            ],
        },
        mtimes={
            "personal_skills": "2026-01-01T00:00:00Z",
            "team_skills": "2026-08-01T00:00:00Z",
        },
    )

    response = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "query": "frontend design",
            "scopes": ["personal_skills", "team_skills"],
            "max_items": 12,
        },
    )

    assert response.status_code == 200
    body = response.json()
    skills = [item for item in body["items"] if item["kind"] == "skill"]
    selected = [item for item in skills if item["selected"]]
    # The newer team copy wins even though a personal copy exists.
    assert len(selected) == 1
    assert selected[0]["scope"] == "team_skills"
    shadowed = [item for item in skills if not item["selected"]]
    assert shadowed and shadowed[0]["scope"] == "personal_skills"
    assert shadowed[0]["shadowed_by"] == "team:frontend-design"


def test_context_ref_is_bound_to_integration(tmp_path) -> None:
    client, owner, token, config = _client(tmp_path)
    owner._workspace_request = AsyncMock(
        return_value=[
            {
                "uri": "viking://user/alice-private/memories/profile.md",
                "abstract": "profile",
            }
        ]
    )
    resolved = client.post(
        "/internal/agents/context/resolve",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "query": "profile",
            "scopes": ["personal_memory"],
        },
    ).json()
    context_ref = resolved["items"][0]["context_ref"]

    register_agent(config, _agent_payload("demo:tenant-b"))
    _record, other_token = issue_agent_access_token(
        config,
        agent_id="demo:tenant-b",
    )
    response = client.post(
        "/internal/agents/context/read",
        headers=_headers(other_token),
        json={"context_ref": context_ref, "level": "l0"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "CONTEXT_REF_INVALID"


def test_remember_and_forget_are_limited_to_personal_memory(tmp_path) -> None:
    client, owner, token, _config = _client(tmp_path)
    owner._workspace_request = AsyncMock(return_value={})

    remembered = client.post(
        "/internal/agents/context/remember",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "content": "Prefer concise diffs.",
            "category": "preferences",
            "idempotency_key": "pref-1",
        },
    )
    assert remembered.status_code == 200
    context_ref = remembered.json()["context_ref"]
    calls = owner._workspace_request.await_args_list
    write = next(call for call in calls if call.args[3] == "/api/v1/content/write")
    assert write.kwargs["json"]["uri"].startswith(
        "viking://user/alice-private/memories/preferences/"
    )

    forgotten = client.post(
        "/internal/agents/context/forget",
        headers=_headers(token),
        json={"context_ref": context_ref},
    )
    assert forgotten.status_code == 200
    delete = owner._workspace_request.await_args_list[-1]
    assert delete.args[2:4] == ("DELETE", "/api/v1/fs")
    assert delete.kwargs["params"]["recursive"] == "false"


def test_context_session_append_and_commit_are_idempotent(tmp_path) -> None:
    client, owner, token, _config = _client(tmp_path)
    owner._workspace_request = AsyncMock(return_value={"ok": True})

    started = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-1",
        },
    )
    assert started.status_code == 200
    context_session_id = started.json()["context_session_id"]
    duplicate_start = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-1",
        },
    )
    assert duplicate_start.status_code == 200
    assert duplicate_start.json() == {
        "context_session_id": context_session_id,
        "created": False,
    }

    event = {
        "context_session_id": context_session_id,
        "event_id": "event-1",
        "sequence": 1,
        "role": "user",
        "content": "hello",
    }
    first = client.post(
        "/internal/agents/context/sessions/append",
        headers=_headers(token),
        json=event,
    )
    duplicate = client.post(
        "/internal/agents/context/sessions/append",
        headers=_headers(token),
        json=event,
    )
    commit = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json={"context_session_id": context_session_id},
    )
    duplicate_commit = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json={"context_session_id": context_session_id},
    )

    assert first.json() == {
        "appended": True,
        "duplicate": False,
        "sequence": 1,
    }
    assert duplicate.json()["duplicate"] is True
    assert commit.json()["duplicate"] is False
    assert duplicate_commit.json()["duplicate"] is True
    message_calls = [
        call
        for call in owner._workspace_request.await_args_list
        if call.args[3].endswith("/messages")
    ]
    commit_calls = [
        call
        for call in owner._workspace_request.await_args_list
        if call.args[3].endswith("/commit")
    ]
    create_calls = [
        call
        for call in owner._workspace_request.await_args_list
        if call.args[2:4] == ("POST", "/api/v1/sessions")
    ]
    assert len(create_calls) == 1
    assert len(message_calls) == 1
    assert len(commit_calls) == 1


def test_context_session_start_recovers_unconfirmed_openviking_create(
    tmp_path,
) -> None:
    client, owner, token, config = _client(tmp_path)
    state = ContextStateStore(config)
    pending, created = state.start_session(
        agent_id="demo:tenant-a",
        user_id="alice",
        external_session_id="conversation-pending",
    )
    assert created is True
    assert pending["openviking_created"] is False
    owner._workspace_request = AsyncMock(return_value={"ok": True})

    recovered = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-pending",
        },
    )

    assert recovered.status_code == 200
    assert recovered.json() == {
        "context_session_id": pending["context_session_id"],
        "created": False,
    }
    calls = owner._workspace_request.await_args_list
    assert len(calls) == 1
    assert calls[0].args[2:4] == (
        "GET",
        f"/api/v1/sessions/{pending['openviking_session_id']}",
    )
    stored = state.get_session(
        pending["context_session_id"],
        agent_id="demo:tenant-a",
    )
    assert stored["openviking_created"] is True


def test_context_skill_inventory_refs_commit_in_same_session(
    tmp_path,
) -> None:
    client, owner, token, _config = _client(tmp_path)

    async def fake_request(_user, scope, _method, path, **_kwargs):
        if path == "/api/v1/fs/tree":
            root = scope.root_uri.rstrip("/")
            return [
                {
                    "uri": f"{root}/demo-skill",
                    "is_dir": True,
                },
                {
                    "uri": f"{root}/demo-skill/SKILL.md",
                    "is_dir": False,
                },
            ]
        if path == "/api/v1/content/read":
            return {"content": "# Demo Skill"}
        return {"ok": True}

    owner._workspace_request = AsyncMock(side_effect=fake_request)
    started = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-skills",
        },
    )
    context_session_id = started.json()["context_session_id"]

    listed = client.get(
        "/internal/agents/context/skills",
        headers=_headers(token),
        params={
            "external_subject": "external-alice",
            "scope": "team",
            "context_session_id": context_session_id,
        },
    )
    skill_ref = listed.json()["skills"][0]["context_ref"]
    read = client.post(
        "/internal/agents/context/read",
        headers=_headers(token),
        json={"context_ref": skill_ref, "level": "full"},
    )
    committed = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json={
            "context_session_id": context_session_id,
            "used_context_refs": [skill_ref],
        },
    )

    assert read.status_code == 200
    assert committed.status_code == 200
    assert committed.json()["usage"] == {
        "contexts": 0,
        "skills": 1,
        "submitted": 1,
        "skipped": 0,
    }


def test_context_session_submits_used_refs_once_before_retrying_commit(tmp_path) -> None:
    client, owner, token, config = _client(tmp_path)
    commit_attempts = 0

    async def fake_request(_user, _scope, _method, path, **_kwargs):
        nonlocal commit_attempts
        if path.endswith("/commit"):
            commit_attempts += 1
            if commit_attempts == 1:
                raise HTTPException(status_code=503, detail="commit unavailable")
        return {"ok": True}

    owner._workspace_request = AsyncMock(side_effect=fake_request)
    started = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-used",
        },
    )
    context_session_id = started.json()["context_session_id"]
    state = ContextStateStore(config)
    memory_ref, _ = state.issue_ref(
        agent_id="demo:tenant-a",
        user_id="alice",
        session_id=context_session_id,
        scope="personal_memory",
        uri="viking://user/alice-private/memories/profile.md",
        kind="memory",
    )
    skill_ref, _ = state.issue_ref(
        agent_id="demo:tenant-a",
        user_id="alice",
        session_id=context_session_id,
        scope="team_skills",
        uri="viking://resources/team-skill-evolver/skills/demo",
        kind="skill",
    )
    state.save_snapshot(
        snapshot_id="ctxsnap-used",
        agent_id="demo:tenant-a",
        user_id="alice",
        session_id=context_session_id,
        items=[
            {"context_ref": memory_ref, "title": "profile"},
            {"context_ref": skill_ref, "title": "demo"},
        ],
    )
    state.record_snapshot_read(
        ref_id=memory_ref,
        agent_id="demo:tenant-a",
        level="full",
        value="profile details",
    )
    state.record_snapshot_read(
        ref_id=skill_ref,
        agent_id="demo:tenant-a",
        level="full",
        value={"SKILL.md": "# Demo"},
    )
    payload = {
        "context_session_id": context_session_id,
        "used_context_refs": [memory_ref, skill_ref, memory_ref],
    }

    failed = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json=payload,
    )
    succeeded = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json=payload,
    )
    duplicate = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json=payload,
    )

    assert failed.status_code == 503
    assert succeeded.status_code == 200
    assert succeeded.json()["usage"] == {
        "contexts": 1,
        "skills": 1,
        "submitted": 0,
        "skipped": 2,
    }
    assert duplicate.json()["duplicate"] is True
    used_calls = [
        call
        for call in owner._workspace_request.await_args_list
        if call.args[3].endswith("/used")
    ]
    assert len(used_calls) == 2
    assert used_calls[0].kwargs["json"] == {
        "contexts": ["viking://user/alice-private/memories/profile.md"]
    }
    assert used_calls[1].kwargs["json"] == {
        "skill": {"uri": "viking://resources/team-skill-evolver/skills/demo"}
    }
    stored = state.get_session(
        context_session_id,
        agent_id="demo:tenant-a",
    )
    assert len(stored["submitted_usage_keys"]) == 2


def test_context_session_rejects_used_ref_from_another_session(tmp_path) -> None:
    client, owner, token, config = _client(tmp_path)
    owner._workspace_request = AsyncMock(return_value={"ok": True})
    first = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-a",
        },
    ).json()["context_session_id"]
    second = client.post(
        "/internal/agents/context/sessions/start",
        headers=_headers(token),
        json={
            "external_subject": "external-alice",
            "external_session_id": "conversation-b",
        },
    ).json()["context_session_id"]
    state = ContextStateStore(config)
    foreign_ref, _ = state.issue_ref(
        agent_id="demo:tenant-a",
        user_id="alice",
        session_id=first,
        scope="personal_memory",
        uri="viking://user/alice-private/memories/profile.md",
        kind="memory",
    )
    state.save_snapshot(
        snapshot_id="ctxsnap-foreign",
        agent_id="demo:tenant-a",
        user_id="alice",
        session_id=first,
        items=[{"context_ref": foreign_ref, "title": "profile"}],
    )

    response = client.post(
        "/internal/agents/context/sessions/commit",
        headers=_headers(token),
        json={
            "context_session_id": second,
            "used_context_refs": [foreign_ref],
        },
    )

    assert response.status_code == 409
    assert "invalid for this session" in response.json()["detail"]


def test_context_usage_is_rebuilt_from_server_refs(tmp_path) -> None:
    _client_instance, _owner, _token, config = _client(tmp_path)
    state = ContextStateStore(config)
    context_ref, _receipt = state.issue_ref(
        agent_id="demo:tenant-a",
        user_id="alice",
        session_id="session-1",
        scope="personal_memory",
        uri="viking://user/alice-private/memories/profile.md",
        kind="memory",
        version="v1",
    )

    verified = verify_context_usage(
        config,
        agent_id="demo:tenant-a",
        user_id="alice",
        turns=[
            {
                "turn_num": 1,
                "context_usage": {
                    "memory_refs": [
                        {
                            "context_ref": context_ref,
                            "scope": "forged-team-scope",
                            "uri": "viking://user/other/private.md",
                            "operation": "injected",
                        }
                    ],
                    "skill_refs": [],
                },
            }
        ],
    )

    item = verified[0]["context_usage"]["memory_refs"][0]
    assert item["scope"] == "personal_memory"
    assert item["uri_hash"]
    assert "uri" not in item
    assert verified[0]["context_usage"]["verified"] is True

    with pytest.raises(ValueError, match="invalid or expired"):
        verify_context_usage(
            config,
            agent_id="demo:tenant-a",
            user_id="alice",
            turns=[
                {
                    "context_usage": {
                        "memory_refs": [{"context_ref": "ctx_forged"}],
                        "skill_refs": [],
                    }
                }
            ],
        )
