"""Original File references are read only inside tenant and connection scope."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from session_ingestion.repository import SessionIngestionRepository
from team_ma.replay_support import capabilities
from teamEvolver.storage.postgres import PostgresObjectStore

pytestmark = pytest.mark.postgres


def test_original_material_lookup_is_tenant_connection_and_hash_scoped(pg_env):
    async def run():
        for tenant in ("alpha", "beta"):
            source = {"runtime": {"connection_id": "one"},
                      "source_materials": [{"sha256": "same", "original_file_id": tenant + "-file"}]}
            async with pg_env.tx.tenant_tx(tenant) as conn:
                await PostgresObjectStore(pg_env.tx, tenant).put(
                    conn, "source/content", json.dumps({"session": source}).encode())
                await SessionIngestionRepository(pg_env.tx).write_session(
                    conn, tenant_id=tenant, session_id="source", content_ref="source/content")
        services = SimpleNamespace(tx=pg_env.tx)
        for tenant in ("alpha", "beta"):
            lookup = capabilities(services, tenant)["original_file_id"]
            material = {"source_session_id": "source", "sha256": "same"}
            assert await lookup({"connection_id": "one"}, material) == tenant + "-file"
            assert await lookup({"connection_id": "other"}, material) == ""
            assert await lookup({"connection_id": "one"}, {**material, "sha256": "different"}) == ""
            assert await lookup({"connection_id": "one"}, {**material, "source_session_id": "missing"}) == ""
            assert await lookup({"connection_id": "one"}, {"sha256": "same"}) == ""
        assert await capabilities(services, "gamma")["original_file_id"](
            {"connection_id": "one"}, material) == ""

    asyncio.run(run())
