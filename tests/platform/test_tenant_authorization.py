"""Tenant authorization lookups must observe mutations from peer replicas."""

import asyncio

from teamEvolver.tenants.registry import TenantRegistry, hash_agent_token


class MutableRegistry(TenantRegistry):
    def __init__(self):
        super().__init__(runtime=object(), ttl_seconds=3600)
        self.rows = {}
        self.token_rows = {}

    def _run(self, operation):
        return asyncio.run(operation)

    async def _fetch_tenant(self, tenant_id):
        row = self.rows.get(tenant_id)
        return dict(row) if row else None

    async def _fetch_by_token(self, token_hash):
        row = self.token_rows.get(token_hash)
        if not row or row["status"] != "active":
            return None
        return dict(row)


def tenant_row(tenant_id="tenant-a", *, status="active"):
    return {
        "tenant_id": tenant_id,
        "display_name": tenant_id,
        "status": status,
        "config": {},
    }


def test_peer_token_rotation_invalidates_old_token_immediately():
    registry = MutableRegistry()
    old_token = "tevt_old"
    new_token = "tevt_new"
    row = tenant_row()
    registry.token_rows[hash_agent_token(old_token)] = row

    assert registry.resolve_by_agent_token(old_token).tenant_id == "tenant-a"

    registry.token_rows.pop(hash_agent_token(old_token))
    registry.token_rows[hash_agent_token(new_token)] = row

    assert registry.resolve_by_agent_token(old_token) is None
    assert registry.resolve_by_agent_token(new_token).tenant_id == "tenant-a"


def test_peer_tenant_disable_is_visible_immediately():
    registry = MutableRegistry()
    registry.rows["tenant-a"] = tenant_row()

    assert registry.get("tenant-a").status == "active"

    registry.rows["tenant-a"] = tenant_row(status="disabled")

    assert registry.get("tenant-a").status == "disabled"
