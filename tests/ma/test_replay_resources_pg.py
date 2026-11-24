"""Discovery reuses source references without creating infrastructure or secrets."""

import asyncio
import copy
import json

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.discovery import discover_connection, replay_capability
from team_ma.replay_resources import effective_replay_config
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient

pytestmark = pytest.mark.postgres


def test_discovery_reuses_source_without_remote_writes_or_reading_credentials(pg_env):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
    source = {"id": "business", "config": {"type": "cloud", "networking": {"type": "unrestricted"},
              "packages": {"pip": ["httpx==0.28.1"]}, "setup_script": "echo ready",
              "env": {"PROFILE": "test"}, "tos": {"bucket": "production", "prefix": "shared"}}}
    original = copy.deepcopy(source)
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path.endswith("/agents/root"):
            return httpx.Response(200, json={"id": "root", "version": 1})
        if request.url.path.endswith("/environments/business"):
            return httpx.Response(200, json=source)
        raise AssertionError(str(request.url))

    async def run():
        config = validate_config({
            "display_name": "Replay", "entry_agent_id": "root", "environment_id": "business",
            "vault_ids": ["source"], "ma_secret_ref": "existing", "tenant_token_ref": "existing",
            "replay_enabled": True,
            # Previously configured resources no longer override source facts.
            "replay_environment_id": "old-copy", "replay_vault_ids": ["old-empty-vault"],
        })
        async with pg_env.tx.tenant_tx("alpha") as conn:
            connection = await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            await enqueue(conn, services, tenant_id="alpha", connection_id="one",
                          kind="ma.discover", operation_id="discover")
            task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.discover"])
            operation = await repo.operation(conn, "discover")
        async with MAClient("https://ma.example/api/v3", "secret",
                            transport=httpx.MockTransport(respond)) as client:
            await discover_connection(task, services, client, connection, operation)
            await discover_connection(task, services, client, connection, operation)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            live = await repo.connection(conn, "one")
            snapshot = await repo.discovery_snapshot(conn, "one", 1)
            resources = snapshot["replay_resources"]
            assert live["revision"] == 1 and live["config"] == config
            assert resources == {"status": "ready", "environment_id": "business", "vault_ids": ["source"]}
            assert snapshot["replay_environment"] == source == original
            assert live["detail"]["replay_capability"]["supported"]
            assert await repo.effects(conn, "one") == []
            fixed = effective_replay_config(config, resources)
            assert fixed["replay_vault_ids"] == ["source"]
            assert fixed["replay_environment_id"] == "business"
            assert "secret_value" not in json.dumps(snapshot)
        assert all("vault" not in path for _, path in requests)
    asyncio.run(run())


def test_source_references_allowed_while_real_executor_constraints_remain():
    config = validate_config({
        "display_name": "Replay", "entry_agent_id": "root", "environment_id": "business",
        "vault_ids": ["source"], "ma_secret_ref": "existing", "tenant_token_ref": "existing",
        "replay_enabled": True, "replay_environment_id": "business", "replay_vault_ids": ["source"],
    })
    graph, environment = {"agents": {"root": {}}}, {"id": "business", "config": {"type": "cloud"}}
    assert replay_capability(config, graph, environment)["supported"]
    graph["agents"]["root"]["mcp_servers"] = [{"url": "https://example.com"}]
    assert replay_capability(config, graph, environment)["reason"] == "MCP isolation not configured"
