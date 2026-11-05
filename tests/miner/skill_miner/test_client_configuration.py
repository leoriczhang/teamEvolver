"""SkillMiner clients use the shared PG configuration and Secret Ref resolution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from team_miner.skill_miner.config import MiningSettings
from team_miner.skill_miner.platform_client import create_client
from teamEvolver.app import _wire_domain_contracts
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.security.secrets import SecretBox
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant

FACTORIES = ("skill_mining_client_factory", "miner_client_factory", "compile_client_factory")
MINER = {"account_id": "mining-account", "user_id": "mining-user"}


@pytest.fixture
def mining_tenant():
    token = set_current_tenant(TenantContext(
        tenant_id="mining-tenant",
        config_overrides={"sharing_viking_endpoint": "http://127.0.0.1:1934"},
    ))
    try:
        yield
    finally:
        reset_current_tenant(token)


def _pg_services(monkeypatch, *, secret_error=None, with_secret=True):
    master = "offline-test-master"
    monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", master)
    ciphertext = SecretBox(master).encrypt("resolved-test-root-key")
    events = []
    admin_connection = object()

    async def fetchrow(sql, secret_ref):
        assert ".secret_refs WHERE secret_ref = $1" in sql
        events.append(("resolve", secret_ref))
        if secret_error is not None:
            raise secret_error
        return {"ciphertext": ciphertext}

    secret_connection = SimpleNamespace(fetchrow=fetchrow)
    sharing = {
        "endpoint": "https://pg-openviking.test",
        "deployment": "local",
        "account": "global-account",
        "team_user": "global-user",
    }
    if with_secret:
        sharing["secret_ref"] = "sref_offline_test"

    @asynccontextmanager
    async def admin_conn():
        yield admin_connection

    @asynccontextmanager
    async def tenant_tx(tenant_id):
        events.append(("secret_tenant", tenant_id))
        yield secret_connection

    async def read_global(conn):
        assert conn is admin_connection
        events.append(("read_global",))
        return SimpleNamespace(config={"sharing": sharing})

    services = SimpleNamespace(
        tx=SimpleNamespace(
            pool=SimpleNamespace(admin_conn=admin_conn, schema="offline_test"),
            tenant_tx=tenant_tx,
        ),
        extras={
            "config": TeamEvolverConfig(
                sharing_viking_endpoint="http://127.0.0.1:1933",
                sharing_viking_deployment="local",
                sharing_viking_api_key="",
            ),
            "config_repo": SimpleNamespace(read_global=read_global),
        },
    )
    return services, events


@pytest.mark.asyncio
@pytest.mark.parametrize(("composition_factory", "with_secret"), [
    pytest.param(True, True, id="composition-root"),
    pytest.param(False, True, id="no-factory"),
    pytest.param(True, False, id="local-without-key"),
])
async def test_skillminer_requests_pg_endpoint_with_resolved_credentials(
    monkeypatch, mining_tenant, composition_factory, with_secret,
):
    services, events = _pg_services(monkeypatch, with_secret=with_secret)
    if composition_factory:
        _wire_domain_contracts(services)
    settings = MiningSettings(miner_revision=1, revision=1, config={}, miner_config=dict(MINER))
    calls = []

    def transport(request):
        calls.append(request)
        assert str(request.url) == "https://pg-openviking.test/api/v1/tasks/test-task"
        if with_secret:
            assert request.headers["authorization"] == "Bearer resolved-test-root-key"
            assert request.headers["x-api-key"] == "resolved-test-root-key"
        else:
            assert "authorization" not in request.headers
            assert "x-api-key" not in request.headers
        assert request.headers["x-openviking-account"] == MINER["account_id"]
        assert request.headers["x-openviking-user"] == MINER["user_id"]
        return httpx.Response(200, json={"status": "ok", "result": {"task_id": "test-task"}})

    client = await create_client(services, settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        result = await client.request("GET", "/api/v1/tasks/test-task")
        assert result.ok and result.data == {"task_id": "test-task"}
        assert len(calls) == 1
        expected_events = [("read_global",)]
        if with_secret:
            expected_events.extend([("secret_tenant", "default"), ("resolve", "sref_offline_test")])
        assert events == expected_events
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_name", FACTORIES)
@pytest.mark.parametrize("awaitable", [False, True], ids=["sync", "awaitable"])
async def test_client_factories_preserve_priority_and_accept_awaitable_results(factory_name, awaitable):
    expected_client = object()
    called = []

    async def resolved_client():
        return expected_client

    def selected_factory(miner):
        assert miner == MINER
        called.append(factory_name)
        return resolved_client() if awaitable else expected_client

    def lower_priority_factory(_miner):
        pytest.fail("a lower-priority client factory must not be called")

    extras = {name: lower_priority_factory for name in FACTORIES[FACTORIES.index(factory_name):]}
    extras[factory_name] = selected_factory
    services = SimpleNamespace(extras=extras)
    assert await create_client(services, {"miner": dict(MINER)}) is expected_client
    assert called == [factory_name]


@pytest.mark.asyncio
@pytest.mark.parametrize("composition_factory", [True, False], ids=["composition-root", "no-factory"])
async def test_secret_resolution_failure_does_not_fall_back_to_bootstrap(
    monkeypatch, mining_tenant, composition_factory,
):
    services, events = _pg_services(monkeypatch, secret_error=RuntimeError("Secret Ref unavailable"))
    if composition_factory:
        _wire_domain_contracts(services)
    with pytest.raises(RuntimeError, match="Secret Ref unavailable"):
        await create_client(services, {"miner": dict(MINER)})
    assert events == [("read_global",), ("secret_tenant", "default"), ("resolve", "sref_offline_test")]
