"""Validation acceptance must work while its caller owns the only connection."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from team_ma import queries


@pytest.mark.asyncio
async def test_validation_reads_reuse_the_callers_only_connection(monkeypatch):
    slot = asyncio.Semaphore(1)
    parent = object()
    seen = []

    @asynccontextmanager
    async def tenant_tx(tenant):
        async with slot:
            yield parent

    class Repo:
        async def connection(self, conn, ident, revision=None):
            seen.append(conn)
            return {"enabled": True, "revision": 1, "config": {}, "config_hash": "hash"}

        async def all_resources(self, conn, ident, kind):
            seen.append(conn)
            return []

        async def connections(self, conn):
            seen.append(conn)
            return []

    monkeypatch.setattr(queries, "MARepository", lambda tx: Repo())
    services = SimpleNamespace(tx=SimpleNamespace(tenant_tx=tenant_tx))
    cases = [{"case_id": "c1", "provenance": {"source_runtime": {
        "type": "managed-agents", "connection_id": "ma1", "connection_revision": 1,
        "materials_missing": [{"reason": "video bytes unavailable"}],
    }}}]
    async with tenant_tx("alpha") as conn:
        with pytest.raises(ValueError, match="incomplete"):
            await asyncio.wait_for(queries.freeze(services, "alpha", cases, "demo", conn=conn), 0.2)
        assert await asyncio.wait_for(queries.active_connections(services, "alpha", conn=conn), 0.2) == set()
        assert await asyncio.wait_for(queries.automatic_fingerprints(services, "alpha", conn=conn), 0.2) == {}
        assert await asyncio.wait_for(queries.required_targets(services, "alpha", "demo", conn=conn), 0.2) == []
    assert seen and all(connection is parent for connection in seen)
