"""The content endpoint resolves names by historical IDs, never library heads."""
import os

import pytest
from fastapi.testclient import TestClient

from session_ingestion.payloads import SessionPayloads
from session_ingestion.repository import SessionIngestionRepository
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from teamEvolver.app import build_role_services, create_app
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def test_content_displays_historical_versions_after_cloud_binding_replacement(pg_env, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-root")
    session = {
        "runtime": {"type": "managed-agents", "connection_id": "notes",
                    "agent_graph": {"entry_agent_id": "root", "agents": {
                        "root": {"name": "director", "version": 1,
                                 "skills": [{"skill_id": "historical-id", "version": "2"}]},
                    }}},
        "turns": [{"used_skills": ["original-notes"], "skill_refs": [{
            "name": "original-notes", "agent_name": "director", "source": "skill-tool", "used": True,
        }]}],
    }
    identity = set_current_tenant(TenantContext("alpha"))
    try:
        ref, _ = SessionPayloads(pg_env.tx).put_session("ingest-version", session, stage="session-analysis")
    finally:
        reset_current_tenant(identity)

    async def setup(conn):
        repo = SessionIngestionRepository(pg_env.tx)
        await repo.accept(conn, tenant_id="alpha", ingestion_id="ingest-version", source="push")
        await repo.write_session(conn, tenant_id="alpha", session_id="session-version",
                                 ingestion_id="ingest-version", decision="valuable", user_id="tester",
                                 content_ref=ref)
        ma = MARepository(pg_env.tx)
        config = validate_config({
            "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
            "ma_secret_ref": "ref", "tenant_token_ref": "ref",
        })
        await ma.save(conn, tenant_id="alpha", connection_id="notes", config=config)
        await ma.bind(conn, tenant_id="alpha", connection_id="notes", kind="skill", name="original-notes",
                      record={"id": "replacement-id", "version": "999", "agents": ["root"]})
        await ma.save_release(conn, tenant_id="alpha", connection_id="notes", release_id="historical",
                              manifest_hash="record", status="applied",
                              record={"skills": {"original-notes": {"id": "historical-id", "version": "1"}}})
    _run(pg_env, "alpha", setup)
    cfg = TeamEvolverConfig(storage_pg_enabled=True, storage_pg_dsn=os.environ["TEAMEVOLVER_TEST_PG_DSN"],
                           storage_pg_schema=pg_env.schema, storage_pg_ssl="disable")
    app = create_app(services=build_role_services(role="api", tx=pg_env.tx, config=cfg))
    headers = {"Authorization": "Bearer test-root", "X-Tenant-Id": "alpha"}
    with TestClient(app) as client:
        response = client.get("/api/sessions/session-version/content", headers=headers)
        assert response.status_code == 200, response.text
        version = response.json()["skill_versions"][0]
        assert (version["name"], version["skill_id"], version["version"], version["usage"]) == (
            "original-notes", "historical-id", "2", "used")
        assert client.get("/api/sessions/session-version/content",
                          headers={**headers, "X-Tenant-Id": "beta"}).status_code == 404

        payloads = app.state.services.extras["session_payloads"]
        original = payloads.get_session
        reads = []

        def tracked(ref):
            reads.append(ref)
            return original(ref)

        monkeypatch.setattr(payloads, "get_session", tracked)
        first = client.get("/api/sessions/page", headers=headers)
        assert first.status_code == 200, first.text
        item = first.json()["sessions"][0]
        assert item["used_skills"] == ["original-notes"]
        assert item["available_skills"] == ["original-notes"]
        assert item["used_skill_versions"][0]["version"] == "2"
        assert item["available_skill_versions"][0]["version"] == "2"
        assert "event_ids" not in item["used_skill_versions"][0]
        assert "content_ref" not in item and "content_hash" not in item
        assert reads == [ref]
        second = client.get("/api/sessions/page", headers=headers).json()["sessions"][0]
        assert second == item and reads == [ref]
        assert client.get("/api/sessions/page",
                          headers={**headers, "X-Tenant-Id": "beta"}).json()["sessions"] == []


def test_list_cache_cannot_overwrite_a_changed_source_or_metadata(pg_env):
    async def exercise(conn):
        repo = SessionIngestionRepository(pg_env.tx)
        await repo.write_session(
            conn, tenant_id="alpha", session_id="cache-race", content_ref="before", content_hash="old",
            meta={"title": "before"}, judge={"overall_score": 0.8},
        )
        row = dict(await conn.fetchrow(f"SELECT * FROM {pg_env.schema}.sessions WHERE session_id='cache-race'"))
        import json
        row["meta"] = json.loads(row["meta"])
        before = dict(row)
        assert await repo.cache_list_meta(conn, row=row, meta={**row["meta"], "skill_versions_revision": 1})
        assert not await repo.cache_list_meta(conn, row=row, meta={"title": "stale"})
        cached = dict(await conn.fetchrow(f"SELECT * FROM {pg_env.schema}.sessions WHERE session_id='cache-race'"))
        for key in ("judge", "business_time", "ingested_at", "content_ref", "content_hash"):
            assert cached[key] == before[key]
        cached["meta"] = json.loads(cached["meta"])
        await conn.execute(f"UPDATE {pg_env.schema}.sessions SET content_hash='new' WHERE session_id='cache-race'")
        assert not await repo.cache_list_meta(conn, row=cached, meta={"title": "wrong-source"})
    _run(pg_env, "alpha", exercise)
