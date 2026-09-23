from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.platform_assets import (
    PlatformAssetsMixin,
    _configured_backend,
)
from teamEvolver.storage import LocalObjectStore


class _Host(PlatformAssetsMixin):
    def __init__(self, config):
        self.config = config


def _client(config: TeamEvolverConfig) -> TestClient:
    host = _Host(config)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.console_user = {"id": "admin", "role": "admin"}
        return await call_next(request)

    host._register_platform_assets_routes(app)
    return TestClient(app)


def test_platform_assets_read_pg_or_nas_only():
    config = TeamEvolverConfig(
        sharing_session_backend="viking",
        sharing_skill_backend="viking",
    )

    assert _configured_backend(config, "session") == ("viking", "local")
    assert _configured_backend(config, "skill") == ("viking", "local")


def test_platform_assets_merge_session_and_skill_stores(tmp_path):
    session_root = tmp_path / "session-store"
    skill_root = tmp_path / "skill-store"
    LocalObjectStore(session_root).put_object(
        "sessions/session-1.json",
        '{"session_id":"session-1","status":"queued"}',
    )
    LocalObjectStore(session_root).put_object(
        "unrelated.json",
        '{"hidden":true}',
    )
    LocalObjectStore(skill_root).put_object(
        "candidate_skills/job-1/SKILL.md",
        "# Candidate",
    )
    LocalObjectStore(skill_root).put_object(
        "manifest.json",
        '{"demo":{"version":2}}',
    )
    LocalObjectStore(skill_root).put_object(
        "skills/demo/SKILL.md",
        "# Published skill",
    )
    config = TeamEvolverConfig(
        sharing_enabled=False,
        sharing_session_backend="local",
        sharing_skill_backend="local",
        sharing_local_root=str(session_root),
        sharing_skill_local_root=str(skill_root),
        sharing_skill_mirror_enabled=False,
    )

    client = _client(config)
    response = client.get("/api/platform-assets/tree")

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert [source["backend"] for source in payload["sources"]] == ["local", "local"]
    uris = {entry["uri"] for entry in payload["entries"]}
    assert "platform://session/sessions/session-1.json" in uris
    assert "platform://skill/candidate_skills/job-1/SKILL.md" in uris
    assert "platform://skill/manifest.json" in uris
    assert all("unrelated.json" not in uri for uri in uris)
    assert all("/skills/" not in uri for uri in uris)

    content = client.get(
        "/api/platform-assets/content",
        params={"uri": "platform://session/sessions/session-1.json"},
    )
    assert content.status_code == 200
    assert content.json()["backend_label"] == "NAS / 本地存储"
    assert content.json()["content"] == (
        '{"session_id":"session-1","status":"queued"}'
    )


def test_platform_asset_content_rejects_unexposed_keys(tmp_path):
    root = tmp_path / "store"
    LocalObjectStore(root).put_object("skills/demo/SKILL.md", "# Hidden")
    config = TeamEvolverConfig(
        sharing_enabled=False,
        sharing_session_backend="local",
        sharing_skill_backend="local",
        sharing_local_root=str(root),
        sharing_skill_local_root=str(root),
        sharing_skill_mirror_enabled=False,
    )

    response = _client(config).get(
        "/api/platform-assets/content",
        params={"uri": "platform://skill/skills/demo/SKILL.md"},
    )

    assert response.status_code == 403
