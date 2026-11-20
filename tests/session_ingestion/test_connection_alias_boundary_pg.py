"""The legacy pull route cannot bypass named-connection controls via a binding alias."""
from __future__ import annotations

import pytest

from session_ingestion.adapters.repository import AdapterRepository
from session_ingestion.api import create_router
from session_ingestion.connections import ConnectionRepository
from session_ingestion.payloads import SessionPayloads
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres

SOURCE = '''SOURCE = {"enabled":True,"supported_filters":["agent_id"],"required_filters":[]}
def build_adapter(config=None):
    return object()
'''


def setup(env):
    async def seed(conn):
        adapters = AdapterRepository(env.tx)
        revision = await adapters.save_source(conn, tenant_id="default", adapter_id="shared.py", source=SOURCE)
        for identity, enabled in (("disabled", False), ("fixed", True)):
            await ConnectionRepository(env.tx).save(
                conn, tenant_id="default", identity=identity, expected_revision=0,
                document={"adapter_id": "shared.py", "adapter_revision": revision, "enabled": enabled,
                          "settings": {"profile": "default", "options": {}},
                          "default_filters": {"agent_id": "agent-a"}},
            )
        for binding in ("ordinary-binding", "datasource"):
            await adapters.bind(conn, tenant_id="default", binding_key=binding,
                                adapter_id="shared.py", revision=revision)
    _run(env, "default", seed)


def counts(env):
    async def read(conn):
        return {table: await conn.fetchval(f"SELECT count(*) FROM {env.schema}.{table}") for table in (
            "ingestions", "worker_tasks", "session_pull_runs", "session_pull_members", "storage_objects",
            "datasource_connection_versions",
        )}
    return _run(env, "default", read)


def test_reserved_aliases_cannot_bypass_disabled_or_fixed_agent_and_legacy_bindings_still_work(
    pg_env, tmp_path, monkeypatch,
):
    setup(pg_env)
    client = _pod(pg_env, tmp_path, monkeypatch, pod="connection-alias")
    services = client.app.state.services
    services.extras["session_payloads"] = SessionPayloads(pg_env.tx)
    client.app.include_router(create_router(services))
    with client:
        _login(client, bootstrap=True)
        before = counts(pg_env)
        for identity in ("disabled", "fixed"):
            # Even an authorized tenant admin must use the full connection
            # pipeline; resolving only its adapter revision loses its controls.
            response = client.post("/api/datasource/pull", headers=H, json={
                "adapter_id": "datasource:" + identity, "filters": {"agent_id": "other-agent"},
            })
            assert response.status_code == 400, response.text
            assert "/api/datasource/connections/" in response.json()["detail"]
            assert counts(pg_env) == before
        disabled = client.post("/api/datasource/connections/disabled/pull", headers=H, json={})
        assert disabled.status_code == 409
        fixed = client.post("/api/datasource/connections/fixed/pull", headers=H,
                            json={"filters": {"agent_id": "other-agent"}})
        assert fixed.status_code == 400
        assert counts(pg_env) == before
        for body, expected in (({"adapter_id": "ordinary-binding", "filters": []}, 400),
                               ({"adapter_id": "not-registered", "filters": {}}, 409),
                               ({"adapter_id": "ordinary-binding", "unexpected": "value"}, 400)):
            response = client.post("/api/datasource/pull", headers=H, json=body)
            assert response.status_code == expected, response.text
            assert counts(pg_env) == before
        accepted = client.post("/api/datasource/pull", headers=H,
                               json={"adapter_id": "ordinary-binding", "filters": {"agent_id": "agent-a"}})
        assert accepted.status_code == 202, accepted.text
        after = counts(pg_env)
        assert after["worker_tasks"] == before["worker_tasks"] + 1
        assert after["session_pull_runs"] == before["session_pull_runs"] + 1


def test_old_and_named_schedule_apis_reject_invalid_inputs_without_partial_writes(pg_env, tmp_path, monkeypatch):
    setup(pg_env)
    client = _pod(pg_env, tmp_path, monkeypatch, pod="schedule-validation")
    services = client.app.state.services
    services.extras["session_payloads"] = SessionPayloads(pg_env.tx)
    client.app.include_router(create_router(services))
    valid = {"enabled": True, "time": "02:00", "timezone": "Asia/Shanghai", "window": "previous_day"}
    with client:
        _login(client, bootstrap=True)
        before = counts(pg_env)
        for schedule in (None, [], "daily", {**valid, "timezone": "Definitely/NotAZone"},
                         {**valid, "timezone": 3}, {**valid, "time": None}, {**valid, "time": []}):
            legacy = client.put("/api/datasource/schedule", headers=H, json=schedule)
            named = client.put("/api/datasource/connections/fixed", headers=H, json={
                "expected_revision": 1, "adapter_id": "shared.py", "adapter_revision": 1, "schedule": schedule,
            })
            assert legacy.status_code == named.status_code == 400, (legacy.text, named.text)
            assert counts(pg_env) == before
        accepted = client.put("/api/datasource/schedule", headers=H, json=valid)
        assert accepted.status_code == 200, accepted.text
