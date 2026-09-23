"""OpenViking console workspace → local/NAS fallback.

Verifies the workspace bridge degrades to the built-in filesystem store when
OpenViking is unavailable, and — crucially — that a team-skill edit during an
outage lands on the SAME directory ``build_object_store`` falls back to, so the
console and the Skill backend share one NAS tree.
"""

from __future__ import annotations

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.openviking_workspace import (
    _LocalWorkspaceStore,
    _OpenVikingRequestError,
    OpenVikingWorkspaceMixin,
    _parse_viking_uri,
    _scope_fallback_root,
    _scope_map,
)
from teamEvolver.proxy.users_admin import _normalize_space
from teamEvolver.storage import _effective_fallback_root


def _config(tmp_path, **kwargs):
    return TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        skills_dir=str(tmp_path / "skills"),
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_viking_endpoint="http://ov.invalid:1933",
        sharing_viking_account="acct-x",
        sharing_local_root=str(tmp_path / "nas"),
        sharing_local_fallback_enabled=True,
        **kwargs,
    )


def test_parse_viking_uri_matches_object_store_split():
    ns, prefix, base = _parse_viking_uri("viking://resources/team-skill-evolver/skills")
    assert ns == "resources"
    assert prefix == "team-skill-evolver"
    assert base == "viking://resources/team-skill-evolver/"

    ns, prefix, base = _parse_viking_uri("viking://user/alice/memories")
    assert ns == "user"
    assert prefix == "alice"
    assert base == "viking://user/alice/"


def test_asset_workspaces_use_namespace_roots_with_compatible_fallbacks(tmp_path):
    config = _config(tmp_path)
    scopes = _scope_map(
        config,
        "alice",
        is_admin=True,
        personal_user="alice",
    )

    assert scopes["personal_workspace"].root_uri == "viking://user"
    assert scopes["team_workspace"].root_uri == "viking://resources"
    personal_root, personal_key_base = _scope_fallback_root(
        config,
        scopes["personal_workspace"],
        config.sharing_viking_endpoint,
    )
    assert personal_key_base == "viking://user/alice/"
    assert _scope_fallback_root(
        config,
        scopes["team_workspace"],
        config.sharing_viking_endpoint,
    )[1] == "viking://resources/team-skill-evolver/"

    local = _LocalWorkspaceStore(
        personal_root,
        personal_key_base,
        scope_root=scopes["personal_workspace"].root_uri,
    )
    uri = "viking://user/alice/memories/note.md"
    local.write(uri, "remember", "replace")
    assert [entry["uri"] for entry in local.entries("viking://user", recursive=True)] == [
        "viking://user/alice/memories",
        uri,
    ]


def test_workspace_uses_only_trusted_root_key_and_user_name(tmp_path):
    config = _config(
        tmp_path,
        sharing_viking_api_key="root-key",
        sharing_viking_team_api_key="legacy-root-key",
        sharing_viking_personal_api_key="legacy-personal-key",
    )
    scope = _scope_map(
        config,
        "alice",
        is_admin=False,
        personal_user="alice-name",
    )["personal_workspace"]

    class Owner(OpenVikingWorkspaceMixin):
        def _workspace_config(self):
            return config

    headers = Owner()._workspace_headers(
        {
            "id": "alice",
            "personal_space": {"viking_api_key": "user-key"},
            "team_space": {"viking_api_key": "team-key"},
        },
        scope,
    )

    assert headers["X-API-Key"] == "root-key"
    assert headers["Authorization"] == "Bearer root-key"
    assert headers["X-OpenViking-Account"] == "acct-x"
    assert headers["X-OpenViking-User"] == "alice-name"
    assert _normalize_space(
        {"viking_user": "alice-name", "viking_api_key": "must-not-persist"}
    ) == {
        "backend": "viking",
        "viking_user": "alice-name",
    }

    no_root = _config(tmp_path)
    no_root_scope = _scope_map(
        no_root,
        "alice",
        is_admin=False,
        personal_user="alice-name",
    )["personal_workspace"]

    class NoRootOwner(OpenVikingWorkspaceMixin):
        def _workspace_config(self):
            return no_root

    no_root_headers = NoRootOwner()._workspace_headers(
        {"personal_space": {"viking_api_key": "ignored-user-key"}},
        no_root_scope,
    )
    assert "X-API-Key" not in no_root_headers
    assert "Authorization" not in no_root_headers


def test_local_store_roundtrip(tmp_path):
    store = _LocalWorkspaceStore(
        str(tmp_path / "root"), "viking://resources/team-skill-evolver/"
    )
    uri = "viking://resources/team-skill-evolver/skills/demo/SKILL.md"
    store.write(uri, "hello", "replace")
    assert store.read(uri) == "hello"
    assert store.exists(uri)

    # append + create semantics
    store.write(uri, " world", "append")
    assert store.read(uri) == "hello world"
    try:
        store.write(uri, "no", "create")
    except _OpenVikingRequestError as exc:
        assert exc.status_code == 409
    else:  # pragma: no cover
        raise AssertionError("create over existing key must conflict")

    # listing synthesizes directory entries from key prefixes
    entries = store.entries("viking://resources/team-skill-evolver/skills", recursive=False)
    names = {e["name"]: e["isDir"] for e in entries}
    assert names == {"demo": True}

    tree = store.entries("viking://resources/team-skill-evolver/skills", recursive=True)
    tree_uris = {e["uri"] for e in tree}
    assert uri in tree_uris

    # not-found read surfaces as a 404 request error
    try:
        store.read("viking://resources/team-skill-evolver/skills/missing/SKILL.md")
    except _OpenVikingRequestError as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("missing key must raise 404")

    store.delete("viking://resources/team-skill-evolver/skills/demo")
    assert not store.exists(uri)


def test_team_skill_fallback_shares_backend_root(tmp_path):
    """The console fallback root must equal build_object_store's fallback root."""
    config = _config(tmp_path)
    scopes = _scope_map(config, "alice", is_admin=True)
    team_skills = scopes["team_skills"]

    root, key_base = _scope_fallback_root(
        config, team_skills, config.sharing_viking_endpoint
    )

    # Mirror the identity build_object_store derives for the team Skill backend.
    expected_root = _effective_fallback_root(
        config.sharing_local_root,
        endpoint=config.sharing_viking_endpoint,
        account=config.sharing_viking_account,
        user=config.sharing_viking_user,  # "team"
        root_prefix=config.sharing_viking_root_prefix,
        group_id="",
        namespace="resources",
    )
    assert root == expected_root

    # A write via the workspace store therefore lands under that shared root.
    local = _LocalWorkspaceStore(root, key_base)
    uri = "viking://resources/team-skill-evolver/skills/shared/SKILL.md"
    local.write(uri, "shared body", "replace")
    reread = _LocalWorkspaceStore(expected_root, "viking://resources/team-skill-evolver/")
    assert reread.read(uri) == "shared body"


def test_skill_scope_prefers_dedicated_skill_root(tmp_path):
    """Skill scopes honor sharing_skill_local_root (matching SkillHub)."""
    config = _config(tmp_path, sharing_skill_local_root=str(tmp_path / "skill-nas"))
    scopes = _scope_map(config, "alice", is_admin=True)
    root, _ = _scope_fallback_root(config, scopes["team_skills"], config.sharing_viking_endpoint)
    expected = _effective_fallback_root(
        str(tmp_path / "skill-nas"),
        endpoint=config.sharing_viking_endpoint,
        account=config.sharing_viking_account,
        user=config.sharing_viking_user,
        root_prefix=config.sharing_viking_root_prefix,
        group_id="",
        namespace="resources",
    )
    assert root == expected
