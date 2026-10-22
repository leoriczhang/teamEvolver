"""Source requests resolve missing Miner identity from the global OpenViking settings."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.api import context as source_context
from team_miner.skill_miner.config import MiningSettings
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant


@pytest.fixture
def context_setup(monkeypatch):
    tenant = "miner-tenant"
    token = set_current_tenant(TenantContext(tenant_id=tenant))
    connection = object()

    @asynccontextmanager
    async def tenant_tx(tenant_id):
        assert tenant_id == tenant
        yield connection

    services = SimpleNamespace(
        tx=SimpleNamespace(tenant_tx=tenant_tx),
        extras={"config": SimpleNamespace(sharing_viking_account="old-account", sharing_viking_user="old-user")},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(console_user={"tenant_id": tenant, "capabilities": ["tenant.manage"]})
    )

    def prepare(miner):
        accepted = MiningSettings(7, 3, {"mining": {}}, dict(miner))

        async def load(actual_services, actual_connection):
            assert actual_services is services and actual_connection is connection
            return accepted

        monkeypatch.setattr(source_context.config, "load", load)
        return source_context.Context(services), request, accepted

    try:
        yield prepare, services, tenant
    finally:
        reset_current_tenant(token)


@pytest.mark.asyncio
async def test_explicit_miner_identity_does_not_require_global_connection_settings(monkeypatch, context_setup):
    prepare, _, _ = context_setup
    miner = {"account_id": "explicit-account", "user_id": "explicit-user"}
    context, request, accepted = prepare(miner)

    async def unavailable(*args):
        raise AssertionError("explicit Miner identity must not resolve transport settings")

    monkeypatch.setattr(source_context, "openviking_config", unavailable)
    result = await context.settings(request, account="explicit-account")

    assert result.miner_config == miner
    assert result.miner_revision == 7 and result.revision == 3
    assert accepted.miner_config == miner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("miner", "expected"),
    [
        ({"account_id": "", "user_id": ""}, {"account_id": "pg-account", "user_id": "pg-user"}),
        ({"account_id": "explicit-account", "user_id": ""},
         {"account_id": "explicit-account", "user_id": "pg-user"}),
        ({"account_id": "", "user_id": "explicit-user"},
         {"account_id": "pg-account", "user_id": "explicit-user"}),
    ],
)
async def test_missing_identity_uses_pg_settings_and_preserves_explicit_fields(
    monkeypatch, context_setup, miner, expected,
):
    prepare, services, tenant = context_setup
    context, request, accepted = prepare(miner)
    calls = []

    async def resolve(actual_services, actual_tenant):
        calls.append((actual_services, actual_tenant))
        return SimpleNamespace(sharing_viking_account="pg-account", sharing_viking_user="pg-user")

    monkeypatch.setattr(source_context, "openviking_config", resolve)
    result = await context.settings(request)

    assert calls == [(services, tenant)]
    assert result.miner_config == expected
    assert accepted.miner_config == miner
    assert result.config == accepted.config


@pytest.mark.asyncio
async def test_request_account_mismatch_is_rejected_after_resolving_default(monkeypatch, context_setup):
    prepare, _, _ = context_setup
    context, request, _ = prepare({"account_id": "", "user_id": ""})

    async def resolve(*args):
        return SimpleNamespace(sharing_viking_account="pg-account", sharing_viking_user="pg-user")

    monkeypatch.setattr(source_context, "openviking_config", resolve)
    with pytest.raises(HTTPException) as failure:
        await context.settings(request, account="other-account")

    assert failure.value.status_code == 403
    assert failure.value.detail == "TENANT_ACCOUNT_MISMATCH"


@pytest.mark.asyncio
async def test_global_settings_failure_does_not_reuse_bootstrap_identity(monkeypatch, context_setup):
    prepare, _, _ = context_setup
    context, request, _ = prepare({"account_id": "", "user_id": ""})

    async def unavailable(*args):
        raise RuntimeError("global settings unavailable")

    monkeypatch.setattr(source_context, "openviking_config", unavailable)
    with pytest.raises(RuntimeError, match="global settings unavailable"):
        await context.settings(request)
