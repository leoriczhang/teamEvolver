"""Tenant keys survive reloads and imports never change the active credential."""

import pytest

from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.tenants.registry import TenantRegistry, hash_agent_token


@pytest.fixture
def registry(pg_env, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", "tenant-key-test-master")
    return TenantRegistry(runtime=pg_env.tx.pool)


def test_created_key_survives_registry_reload_and_rotation(pg_env, registry):
    tenant, token = registry.create_tenant("Key retention", "key-retention")
    reloaded = TenantRegistry(runtime=pg_env.tx.pool)
    assert reloaded.get_token(tenant.tenant_id) == token
    assert reloaded.get_token(tenant.tenant_id) == token
    assert reloaded.resolve_by_agent_token(token).tenant_id == tenant.tenant_id
    rotated = registry.rotate_token(tenant.tenant_id)
    assert rotated != token
    assert reloaded.get_token(tenant.tenant_id) == rotated
    assert reloaded.resolve_by_agent_token(token) is None


def test_import_retains_original_hash_and_rejects_other_tenant_key(pg_env, registry):
    token = "tevt_original-alpha-key"

    async def legacy(conn):
        await conn.execute(
            f"UPDATE {pg_env.schema}.tenants SET agent_token_hash=$1 WHERE tenant_id='alpha'",
            hash_agent_token(token),
        )

    pg_env.tx.run_tenant_tx("alpha", legacy)
    assert registry.get_token("alpha") is None
    with pytest.raises(ValueError, match="does not match"):
        registry.import_token("alpha", "tevt_other-tenant-key")
    assert registry.get_token("alpha") is None
    assert registry.import_token("alpha", token) == token
    assert TenantRegistry(runtime=pg_env.tx.pool).get_token("alpha") == token
    assert registry.resolve_by_agent_token(token).tenant_id == "alpha"


def test_existing_encrypted_key_can_be_recovered_within_its_tenant(pg_env, registry):
    token = "tevt_legacy-alpha-key"

    async def legacy(conn):
        await conn.execute(
            f"UPDATE {pg_env.schema}.tenants SET agent_token_hash=$1 WHERE tenant_id='alpha'",
            hash_agent_token(token),
        )
        repo = SecretRepository(pg_env.tx, SecretBox("tenant-key-test-master"))
        await repo.store(conn, tenant_id="alpha", plaintext=token, purpose="managed-agents")

    pg_env.tx.run_tenant_tx("alpha", legacy)
    assert registry.get_token("alpha") == token
    assert registry.get_token("beta") is None


def test_missing_tenant_and_default_are_not_issued_keys(registry):
    assert registry.get_token("missing") is None
    with pytest.raises(ValueError, match="unknown tenant"):
        registry.import_token("missing", "tevt_original")
    with pytest.raises(ValueError, match="default is reserved"):
        registry.import_token("default", "tevt_original")
