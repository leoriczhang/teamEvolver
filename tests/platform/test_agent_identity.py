"""One backend OpenViking Root Key and tenant/user Agent identity."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.openviking.client import OpenVikingClient, OpenVikingError


def test_config_exposes_exactly_one_openviking_root_key():
    key_fields = {f.name for f in fields(TeamEvolverConfig) if "api_key" in f.name or f.name.endswith("_key")}
    # The only OpenViking credential is the single trusted Root Key. No
    # per-agent / per-user / per-tenant key fields are allowed back in.
    assert "sharing_viking_api_key" in key_fields
    forbidden = {name for name in key_fields if name != "sharing_viking_api_key" and "viking" in name}
    assert not forbidden, f"unexpected extra OpenViking key fields: {forbidden}"


def test_miner_uses_the_same_root_key_for_every_account():
    first = OpenVikingClient(endpoint="https://ov.test", api_key="root-key", account_id="account-a").headers()
    second = OpenVikingClient(endpoint="https://ov.test", api_key="root-key", account_id="account-b").headers()
    assert first["Authorization"] == second["Authorization"] == "Bearer root-key"
    assert first["X-API-Key"] == second["X-API-Key"] == "root-key"
    assert first["X-OpenViking-Account"] == "account-a"
    assert second["X-OpenViking-Account"] == "account-b"


def test_miner_cloud_requires_the_root_key():
    with pytest.raises(OpenVikingError) as excinfo:
        OpenVikingClient(endpoint="https://ov.test", api_key="", account_id="default").headers()
    assert excinfo.value.status_code == 503


def test_agent_principal_takes_account_from_config_without_registry():
    from teamEvolver.security.agent_principal import resolve_agent_principal
    from teamEvolver.tenants import registry

    tenant = registry.TenantContext("tenant-x", status="active")
    token = registry.set_current_tenant(tenant)
    try:
        request = SimpleNamespace(
            headers={"authorization": "Bearer tevt_demo"},
            state=SimpleNamespace(tenant_source="token"),
        )
        config = TeamEvolverConfig(sharing_viking_account="account-x")
        principal = resolve_agent_principal(request, config, "  Alice  ")
        assert principal.tenant_id == "tenant-x"
        assert principal.account_id == "account-x"
        assert principal.user_id == "Alice"
    finally:
        registry.reset_current_tenant(token)
