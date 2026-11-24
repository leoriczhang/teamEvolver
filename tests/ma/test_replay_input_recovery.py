"""Replay must recover native URL inputs without losing model-visible modality."""

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import httpx

from team_ma.collection import materials
from team_ma.discovery import replay_capability
from team_ma.schemas import digest
from team_replay.datasets.materials import collect_session_materials
from teamEvolver.managed_agents import MAClient


def test_url_video_is_archived_without_credentials_and_keeps_modality(monkeypatch):
    raw = b"video bytes"

    def respond(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, content=raw)

    async def save(*args):
        return {"object_ref": "archive", "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    monkeypatch.setattr("team_ma.collection.save_object", save)

    async def run():
        async with MAClient("https://ma.example", "secret", transport=httpx.MockTransport(respond)) as client:
            return await materials(client, [{"type": "user.message", "content": [
                {"type": "video", "source": {"type": "url", "url": "https://media.example/a.mp4?token=secret",
                                           "fps": 2}},
            ]}], SimpleNamespace(), "tenant")

    items, missing = asyncio.run(run())
    assert missing == []
    assert items[0]["input_type"] == "video" and items[0]["fps"] == 2
    assert items[0]["media_type"] == "video/mp4"
    assert "secret" not in str(items)
    recovered = collect_session_materials([{"session_id": "s", "source_materials": items}])
    assert recovered[0]["input_type"] == "video" and recovered[0]["fps"] == 2
    assert base64.b64decode(recovered[0]["content_b64"]) == raw


def test_disabled_mcp_toolset_needs_no_external_isolation():
    graph = {"agents": {"root": {"mcp_servers": [{"name": "unused", "url": "https://mcp.example"}],
                                "tools": [{"type": "mcp_toolset", "mcp_server_name": "unused",
                                           "default_config": {"enabled": False}, "configs": []}]}}}
    assert replay_capability({"replay_enabled": True}, graph, {"config": {"type": "cloud"}})["supported"]


def test_unknown_enabled_mcp_still_requires_isolation():
    graph = {"agents": {"root": {"mcp_servers": [{"name": "unknown", "url": "https://mcp.example"}],
                                "tools": [{"type": "mcp_toolset", "mcp_server_name": "unknown",
                                           "default_config": {"enabled": False},
                                           "configs": [{"name": "write", "enabled": True}]}]}}}
    assert not replay_capability({"replay_enabled": True}, graph, {"config": {"type": "cloud"}})["supported"]


def test_readonly_mcp_policy_binds_exact_server_and_closed_toolset():
    server = {"name": "knowledge", "url": "https://mcp.example"}
    agent = {"mcp_servers": [server], "tools": [
        {"type": "mcp_toolset", "mcp_server_name": "knowledge", "default_config": {"enabled": False},
         "configs": [{"name": "read", "enabled": True}]},
    ]}
    config = {"replay_enabled": True, "replay_readonly_mcp": {digest(server): ["read"]}}
    check = lambda: replay_capability(config, {"agents": {"root": agent}}, {"config": {"type": "cloud"}})
    assert check()["supported"]
    agent["tools"][0]["configs"].append({"name": "write", "enabled": True})
    assert not check()["supported"]
    agent["tools"][0]["configs"].pop()
    server["url"] = "https://different.example"
    assert not check()["supported"]


def test_material_download_retries_temporary_failures_without_credentials(monkeypatch):
    from team_ma.collection import download_url
    attempts = []

    def respond(request):
        assert "authorization" not in request.headers
        attempts.append(str(request.url))
        return httpx.Response(503 if len(attempts) < 3 else 200, content=b"original")

    async def no_wait(*args):
        pass

    monkeypatch.setattr("team_ma.collection.asyncio.sleep", no_wait)

    async def run():
        async with MAClient("https://ma.example", "secret", transport=httpx.MockTransport(respond)) as client:
            return await download_url(client, "https://media.example/file", 100)

    assert asyncio.run(run()) == b"original"
    assert len(attempts) == 3
