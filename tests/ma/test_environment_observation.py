"""Newest business Sessions supply the environment, independently of activity."""

import asyncio

import httpx
import pytest

from team_ma.environment_observation import latest_environment
from team_ma.schemas import digest, validate_config
from teamEvolver.managed_agents import MAClient, MAError


def test_latest_business_environment_skips_replays_deleted_and_other_agents():
    requested = []
    rows = [
        {"id": "old-active", "created_at": "2026-11-20T10:00:00Z", "updated_at": "2026-11-25T10:00:00Z"},
        {"id": "latest", "created_at": "2026-11-23T10:00:00Z"},
        {"id": "deleted", "created_at": "2026-11-23T11:00:00Z"},
        {"id": "other-agent", "created_at": "2026-11-23T12:00:00Z"},
        {"id": "tagged-replay", "created_at": "2026-11-23T13:00:00Z"},
        {"id": "owned-replay", "created_at": "2026-11-23T14:00:00Z"},
    ]

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        requested.append(path)
        if path == "sessions":
            assert dict(request.url.params) == {"limit": "50", "agent_id": "root", "order": "desc"}
            return httpx.Response(200, json={"data": rows})
        ident = path.split("/")[1]
        if ident == "deleted":
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return httpx.Response(200, json={
            "id": ident, "agent": {"id": "other" if ident == "other-agent" else "root"},
            "tags": [{"key": "te-replay", "value": "test"}] if ident == "tagged-replay" else [],
            "environment": {"id": "env-new", "config": {"type": "cloud"}},
        })

    async def run():
        async with MAClient("https://ma.example/api/v3", "fixture", transport=httpx.MockTransport(respond)) as client:
            observed = await latest_environment(client, "root", {"owned-replay"})
        assert observed == {
            "session_id": "latest", "created_at": "2026-11-23T10:00:00+00:00",
            "environment_id": "env-new", "environment_hash": digest({"type": "cloud"}),
        }
        assert "sessions/old-active" not in requested
        assert "sessions/owned-replay" not in requested
    asyncio.run(run())


def test_excluded_first_page_continues_and_accepts_environment_id_without_config():
    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        if path == "sessions":
            if "page" not in request.url.params:
                return httpx.Response(200, json={"data": [
                    {"id": "test", "created_at": "2026-11-24T00:00:00Z",
                     "tags": [{"key": "te-test", "value": "probe"}]},
                ], "next_page": "next"})
            return httpx.Response(200, json={"data": [
                None, {"id": "no-time"}, {"id": "valid", "created_at": "2026-11-23T08:00:00+08:00"},
            ]})
        assert path == "sessions/valid"
        return httpx.Response(200, json={"id": "valid", "agent": {"id": "root"},
                                        "environment": {"id": "env"}})

    async def run():
        async with MAClient("https://ma.example/api/v3", "fixture", transport=httpx.MockTransport(respond)) as client:
            observed = await latest_environment(client, "root", set())
        assert observed["environment_id"] == "env"
        assert observed["created_at"] == "2026-11-23T00:00:00+00:00"
        assert observed["environment_hash"] == ""
    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 503])
def test_observation_does_not_hide_auth_or_transport_errors(status):
    def respond(request):
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"data": [
                {"id": "source", "created_at": "2026-11-23T00:00:00Z"},
            ]})
        return httpx.Response(status, json={"error": {"code": "Unavailable"}})

    async def run():
        async with MAClient("https://ma.example/api/v3", "fixture", transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(MAError) as error:
                await latest_environment(client, "root", set())
            assert error.value.status == status
    asyncio.run(run())


def test_environment_can_be_omitted_at_connection_creation():
    config = validate_config({"display_name": "Automatic", "entry_agent_id": "root",
                              "ma_secret_ref": "ark", "tenant_token_ref": "tenant", "replay_enabled": True})
    assert config["environment_id"] == ""
