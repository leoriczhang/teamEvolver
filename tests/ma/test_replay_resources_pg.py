"""Replay resources are prepared by fenced Workers with tenant-owned receipts."""

import asyncio
import copy
import json

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.discovery import discover_connection, replay_capability
from team_ma.effects import UnknownEffect
from team_ma.replay_resources import effective_replay_config
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient
from teamEvolver.security import SecretBox, SecretRepository

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("missing", [False, True])
def test_worker_creates_reuses_and_freezes_replay_resources_without_leaking_secrets(pg_env, missing):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
    secrets = SecretRepository(pg_env.tx, SecretBox("test-key"))
    services.extras["secret_repo"] = secrets
    remote = {"environments": {}, "vaults": {}, "credentials": {}}
    writes = []
    source = {"id": "business", "config": {"type": "cloud", "networking": {"type": "unrestricted"},
              "packages": {"pip": ["httpx==0.28.1"]}, "setup_script": "echo ready",
              "env": {"PROFILE": "test"}, "tos": {"bucket": "production", "prefix": "shared"}}}

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        if request.method == "POST":
            body = json.loads(request.content)
            writes.append(path)
            ident = f"auto-{len(writes)}"
            if path in {"environments", "vaults"}:
                value = {"id": ident, **body}
                remote[path][ident] = value
            else:
                assert path.endswith("/credentials")
                assert body["auth"].pop("secret_value") == "private-ma-secret"
                value = {"id": ident, **body}
                remote["credentials"].setdefault(path, []).append(value)
            return httpx.Response(200, json=value)
        if path.startswith("agents/"):
            return httpx.Response(200, json={"id": path.split("/")[1], "version": 1})
        if path == "environments/business":
            return httpx.Response(200, json=source)
        if path == "vaults/source/credentials":
            return httpx.Response(200, json={"data": [
                {"auth": {"type": "environment_variable", "secret_name": "ARK_API_KEY",
                          "networking": {"type": "limited", "allowed_hosts": ["ma.example"]}}},
                *([{"auth": {"type": "environment_variable", "secret_name": "EXTERNAL_API_KEY"}}] if missing else []),
            ]})
        if path.endswith("/credentials"):
            return httpx.Response(200, json={"data": remote["credentials"].get(path, [])})
        if "/" not in path:
            return httpx.Response(200, json={"data": list(remote[path].values())})
        kind, ident = path.split("/")
        return httpx.Response(200, json=remote[kind][ident])

    async def run():
        async with MAClient("https://ma.example/api/v3", "private-ma-secret",
                            transport=httpx.MockTransport(respond)) as client:
            for tenant in ("alpha", "beta"):
                async with pg_env.tx.tenant_tx(tenant) as conn:
                    key = await secrets.store(conn, tenant_id=tenant, plaintext="private-ma-secret")
                    config = validate_config({"display_name": "Replay", "entry_agent_id": tenant,
                                              "environment_id": "business", "vault_ids": ["source"],
                                              "ma_secret_ref": key.ref, "tenant_token_ref": key.ref,
                                              "ma_base_url": "https://ma.example/api/v3", "replay_enabled": True})
                    connection = await repo.save(conn, tenant_id=tenant, connection_id="one", config=config)
                    await enqueue(conn, services, tenant_id=tenant, connection_id="one",
                                  kind="ma.discover", operation_id="discover")
                    task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.discover"])
                    operation = await repo.operation(conn, "discover")
                await discover_connection(task, services, client, connection, operation)
                count = len(writes)
                await discover_connection(task, services, client, connection, operation)
                assert len(writes) == count
                async with pg_env.tx.tenant_tx(tenant) as conn:
                    live = await repo.connection(conn, "one")
                    snapshot = await repo.discovery_snapshot(conn, "one", 1)
                    resources = snapshot["replay_resources"]
                    assert live["revision"] == 1 and live["config"]["replay_environment_id"] == ""
                    assert resources["environment_id"] != "business"
                    assert resources["vault_ids"] != ["source"]
                    assert resources["missing_credentials"] == (["EXTERNAL_API_KEY"] if missing else [])
                    assert live["detail"]["replay_capability"]["supported"] is not missing
                    fixed = effective_replay_config(config, resources)
                    assert fixed["replay_environment_id"] == resources["environment_id"]
                    assert fixed["replay_vault_ids"] == resources["vault_ids"]
                    assert fixed["replay_isolation"]["vaults"] is True
                    assert "tos" not in snapshot["replay_environment"]["config"]
                    ledger = await repo.effects(conn, "one")
                    assert {row["status"] for row in ledger} == {"confirmed"}
                    assert "private-ma-secret" not in json.dumps([ledger, snapshot, live], default=str)
            assert writes.count("environments") == 2 and writes.count("vaults") == 2
            assert len({row["metadata"]["te_owner"] for row in remote["environments"].values()}) == 2
            assert source["config"]["tos"]["bucket"] == "production"
    asyncio.run(run())


def test_unknown_create_outcome_reconciles_before_retrying_post(pg_env):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
    secrets = SecretRepository(pg_env.tx, SecretBox("test-key"))
    services.extras["secret_repo"] = secrets
    environments, vaults, credentials, posts = [], [], [], []
    hidden = [True]

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        if request.method == "POST":
            posts.append(path)
            value = {"id": "isolated" if path == "environments" else "vault", **json.loads(request.content)}
            if path == "environments":
                environments.append(value)
                raise httpx.ReadTimeout("response lost", request=request)
            if path == "vaults":
                vaults.append(value)
            else:
                value["auth"].pop("secret_value")
                credentials.append(value)
            return httpx.Response(200, json=value)
        if path == "agents/root":
            return httpx.Response(200, json={"id": "root", "version": 1})
        if path == "environments/business":
            return httpx.Response(200, json={"id": "business", "config": {"type": "cloud"}})
        if path == "environments":
            return httpx.Response(200, json={"data": [] if hidden[0] else environments})
        if path == "environments/isolated":
            return httpx.Response(200, json=environments[0])
        if path == "vaults":
            return httpx.Response(200, json={"data": vaults})
        if path == "vaults/vault":
            return httpx.Response(200, json=vaults[0])
        if path.endswith("/credentials"):
            return httpx.Response(200, json={"data": credentials})
        raise AssertionError(path)

    async def run():
        from teamEvolver.managed_agents import MAError
        async with pg_env.tx.tenant_tx("alpha") as conn:
            key = await secrets.store(conn, tenant_id="alpha", plaintext="secret")
            config = validate_config({"display_name": "Replay", "entry_agent_id": "root",
                                      "environment_id": "business", "ma_secret_ref": key.ref,
                                      "tenant_token_ref": key.ref, "ma_base_url": "https://ma.example/api/v3",
                                      "replay_enabled": True})
            connection = await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.discover",
                          operation_id="discover")
            task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.discover"])
            operation = await repo.operation(conn, "discover")
        async with MAClient("https://ma.example/api/v3", "secret", transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                await discover_connection(task, services, client, connection, operation)
            with pytest.raises(UnknownEffect):
                await discover_connection(task, services, client, connection, operation)
            assert posts == ["environments"]
            hidden[0] = False
            await discover_connection(task, services, client, connection, operation)
            assert posts.count("environments") == 1
            async with pg_env.tx.tenant_tx("alpha") as conn:
                assert (await repo.connection(conn, "one"))["detail"]["replay_capability"]["supported"]
            environments[0]["config"]["env"] = {"MUTATED": "true"}
            with pytest.raises(ValueError, match="readback mismatch"):
                await discover_connection(task, services, client, connection, operation)
    asyncio.run(run())


def test_production_ids_and_missing_runtime_credentials_never_pass():
    base = {"display_name": "Replay", "entry_agent_id": "root", "environment_id": "business",
            "vault_ids": ["production"], "ma_secret_ref": "ma", "tenant_token_ref": "tenant", "replay_enabled": True}
    for patch in ({"replay_environment_id": "business"}, {"replay_vault_ids": ["production"]}):
        with pytest.raises(ValueError, match="separate"):
            validate_config({**base, **patch})
    config = effective_replay_config(validate_config(base), {
        "status": "credentials_missing", "environment_id": "isolated", "vault_ids": ["replay"],
        "vaults_automatic": True, "missing_credentials": ["OPENVIKING_API_KEY"],
    })
    graph = {"agents": {"root": {}}}
    environment = {"id": "isolated", "config": {"type": "cloud"}}
    assert not replay_capability(config, graph, environment)["supported"]
    graph["agents"]["root"]["mcp_servers"] = [{"url": "https://example.com"}]
    config["replay_resources"]["status"] = "ready"
    assert replay_capability(config, graph, environment)["reason"] == "MCP isolation not configured"
    # Functions do not mutate administrator configuration.
    copied = copy.deepcopy(config)
    effective_replay_config(config, {})
    assert config == copied
