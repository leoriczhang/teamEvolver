"""Platform config contracts."""

from __future__ import annotations

import pytest

from teamEvolver.config_runtime import (
    TENANT_CONFIG_MODULES,
    ConfigRepository,
    ConfigRevisionConflict,
    TenantConfigMissing,
    freeze_effective_config,
)
from tests.support.config import FakeDB, FakeTxManager


@pytest.mark.asyncio
async def test_global_config_cas_update_and_conflict():
    db = FakeDB()
    tx = FakeTxManager(db)
    repo = ConfigRepository(tx)
    conn = tx.conn_for()

    current = await repo.read_global(conn)
    assert current.revision == 1
    new_rev = await repo.write_global(conn, expected_revision=1, config={"a": 1})
    assert new_rev == 2
    # A stale expected_revision loses the CAS race.
    with pytest.raises(ConfigRevisionConflict):
        await repo.write_global(conn, expected_revision=1, config={"a": 2})


@pytest.mark.asyncio
async def test_tenant_defaults_created_for_all_modules_and_missing_fails_closed():
    db = FakeDB()
    tx = FakeTxManager(db)
    repo = ConfigRepository(tx)
    conn = tx.conn_for("tenant-a")

    # Missing module row fails closed (no silent YAML fallback).
    with pytest.raises(TenantConfigMissing):
        await repo.read_tenant_module(conn, "skills")

    defaults = {m: {"enabled": True, "_module": m} for m in TENANT_CONFIG_MODULES}
    await repo.create_tenant_defaults(conn, tenant_id="tenant-a", defaults=defaults)
    for module in TENANT_CONFIG_MODULES:
        row = await repo.read_tenant_module(conn, module)
        assert row.config["_module"] == module
        assert row.revision == 1


@pytest.mark.asyncio
async def test_create_tenant_defaults_rejects_partial_module_set():
    db = FakeDB()
    tx = FakeTxManager(db)
    repo = ConfigRepository(tx)
    conn = tx.conn_for("tenant-a")
    with pytest.raises(ValueError, match="missing modules"):
        await repo.create_tenant_defaults(conn, tenant_id="tenant-a", defaults={"skills": {}})


@pytest.mark.asyncio
async def test_tenant_config_is_isolated_across_tenants():
    db = FakeDB()
    tx = FakeTxManager(db)
    repo = ConfigRepository(tx)

    conn_a = tx.conn_for("tenant-a")
    defaults = {m: {"_m": m, "owner": "a"} for m in TENANT_CONFIG_MODULES}
    await repo.create_tenant_defaults(conn_a, tenant_id="tenant-a", defaults=defaults)

    # Tenant B sees nothing of tenant A's rows (RLS fail-closed on empty match).
    conn_b = tx.conn_for("tenant-b")
    with pytest.raises(TenantConfigMissing):
        await repo.read_tenant_module(conn_b, "skills")


def test_freeze_effective_config_pins_all_modules():
    frozen = freeze_effective_config(
        global_config={"g": 1},
        module_configs={"skills": {"x": 1}},
    )
    assert frozen["global"] == {"g": 1}
    assert set(frozen["modules"]) == set(TENANT_CONFIG_MODULES)
    assert frozen["modules"]["skills"] == {"x": 1}
    assert frozen["modules"]["memory"] == {}
