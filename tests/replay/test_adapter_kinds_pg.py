"""Datasource, business Replay and evaluator files coexist without cross-kind writes."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from session_ingestion.adapters.repository import AdapterConflict, AdapterRepository
from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.datasets.repository import DatasetConflict
from teamEvolver.app import create_app
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.tenants.registry import TenantContext
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres

SOURCE = '''SOURCE = {"enabled": True, "name": "Real source"}
def build_adapter():
    return None
'''
REPLAY = '''REPLAY_ADAPTER = {"enabled": True, "label": "Business Agent"}
def build_replay_adapter(config):
    return None
'''
EVALUATOR = '''EVALUATOR_ADAPTER = {"enabled": True, "label": "Scoring Agent"}
def evaluate(request, config, *, secret=""):
    return {"baseline": {"score": 0, "reason": "Old"}, "candidate": {"score": 1, "reason": "New"}}
'''


def test_datasource_endpoints_only_access_datasource_files_and_preserve_other_kinds(pg_env):
    async def authorizer(request, services):
        tenant = request.headers.get("x-tenant-id", "default")
        request.state.tenant = TenantContext(tenant, tenant)
        request.state.console_user = {"service_root": True, "tenant_id": tenant}
        request.state.service_root_authenticated = True

    services = build_services(role="api", tx=pg_env.tx, extras={
        "config_repo": ConfigRepository(pg_env.tx), "session_payloads": object(),
    })
    with TestClient(create_app(services=services, authorizer=authorizer)) as client:
        assert client.put("/api/datasource/code", json={
            "file": "source.py", "code": SOURCE, "expected_revision": None,
        }).status_code == 200
        assert client.put("/api/replay-adapter/code", json={
            "file": "business.py", "code": REPLAY, "expected_revision": None,
        }).status_code == 200
        assert client.put("/api/replay-evaluator-adapter/code", json={
            "file": "grader.py", "code": EVALUATOR, "expected_revision": None,
        }).status_code == 200
        available = client.get("/api/datasource")
        assert available.status_code == 200, available.text
        assert [item["file"] for item in available.json()["available"]] == ["source.py"]
        assert client.put("/api/datasource", json={"file": "source.py", "revision": 1}).status_code == 200
        for foreign in ("business.py", "evaluator:grader.py"):
            assert client.put("/api/datasource", json={"file": foreign, "revision": 1}).status_code == 404
            assert client.get("/api/datasource/code", params={"file": foreign}).status_code == 404
            # Explicit null reaches repository ownership checks, not just a stale revision check.
            blocked = client.put("/api/datasource/code", json={
                "file": foreign, "code": SOURCE, "expected_revision": None,
            })
            assert blocked.status_code == 400, blocked.text
        assert client.get("/api/datasource").json()["file"] == "source.py"
        # The reverse direction must not append Replay source to an existing SOURCE name.
        blocked = client.put("/api/replay-adapter/code", json={
            "file": "source.py", "code": REPLAY, "expected_revision": 1,
        })
        assert blocked.status_code == 409, blocked.text
        # SOURCE filenames historically allow ':', so evaluator's internal namespace also needs a guard.
        assert client.put("/api/datasource/code", json={
            "file": "evaluator:blocked.py", "code": SOURCE, "expected_revision": None,
        }).status_code == 200
        blocked = client.put("/api/replay-evaluator-adapter/code", json={
            "file": "blocked.py", "code": EVALUATOR, "expected_revision": 1,
        })
        assert blocked.status_code == 409, blocked.text
        other = client.get("/api/datasource", headers={"x-tenant-id": "alpha"})
        assert other.status_code == 200 and other.json()["available"] == []
        assert client.get("/api/datasource/code", headers={"x-tenant-id": "alpha"},
                          params={"file": "source.py"}).status_code == 404
        assert client.get("/api/datasource").status_code == 200
        assert client.get("/api/datasource/schedule").status_code == 200
        assert client.get("/api/replay-adapter/code", params={"file": "business.py"}).json()["code"] == REPLAY
        current = client.get("/api/replay-evaluator-adapter/code", params={"file": "grader.py"}).json()
        assert current["code"] == EVALUATOR
    rows = _run(pg_env, "default", lambda conn: conn.fetch(
        f"SELECT a.adapter_id,a.kind,count(v.revision) AS versions FROM {pg_env.schema}.adapters a "
        f"JOIN {pg_env.schema}.adapter_versions v USING(tenant_id,adapter_id) GROUP BY a.adapter_id,a.kind"))
    assert len(rows) == 4 and all(row["versions"] == 1 for row in rows)


def test_simultaneous_different_kinds_cannot_claim_the_same_fresh_filename(pg_env):
    async def race():
        async def datasource():
            async with pg_env.tx.tenant_tx("alpha") as conn:
                return await AdapterRepository(pg_env.tx).save_source(
                    conn, tenant_id="alpha", adapter_id="simultaneous.py", source=SOURCE,
                )

        async def replay():
            async with pg_env.tx.tenant_tx("alpha") as conn:
                return await ReplayAdapterRepository(pg_env.tx).save(
                    conn, tenant_id="alpha", filename="simultaneous.py", source=REPLAY, expected_revision=None,
                )

        return await asyncio.gather(datasource(), replay(), return_exceptions=True)

    outcomes = pg_env.tx.pool.run(race())
    assert sum(isinstance(value, (AdapterConflict, DatasetConflict)) for value in outcomes) == 1, outcomes
    rows = _run(pg_env, "alpha", lambda conn: conn.fetch(
        f"SELECT a.kind,v.revision,v.source FROM {pg_env.schema}.adapters a "
        f"JOIN {pg_env.schema}.adapter_versions v USING(tenant_id,adapter_id) WHERE a.adapter_id='simultaneous.py'"))
    assert len(rows) == 1 and rows[0]["revision"] == 1
    assert rows[0]["source"] == {"python": SOURCE, "replay": REPLAY}[rows[0]["kind"]]
