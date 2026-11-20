"""Connection schedules pin revisions and isolate tenants/windows in real PG."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from session_ingestion.adapters.repository import AdapterRepository
from session_ingestion.config import DEFAULT_SESSION_CONFIG
from session_ingestion.connections import ConnectionRepository
from session_ingestion.payloads import SessionPayloads
from session_ingestion.schedule import enqueue_due
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.repository import WorkerTaskRepository
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
SOURCE = '''SOURCE = {"provider":"langfuse","enabled":True,
"connection_profiles":{"default":{"options":{"host":"https://observed.example"},
"secret_refs":{"secret_key":"sref_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}},"connection_options":[],
"supported_filters":["agent_id","from_timestamp","to_timestamp"],"required_filters":[]}
def build_adapter():
    return object()
'''


def services(env):
    return SimpleNamespace(tx=env.tx, tasks=WorkerTaskRepository(env.tx), extras={
        "config_repo": ConfigRepository(env.tx), "session_payloads": SessionPayloads(env.tx),
    })


def connection(env, identity, *, tenant="default", revision=0, actor="A", enabled=True,
               time="00:00", zone="UTC", schedule_enabled=True, adapter_revision=None):
    async def seed(conn):
        adapters = AdapterRepository(env.tx)
        adapter = adapter_revision or await adapters.save_source(
            conn, tenant_id=tenant, adapter_id="shared.py", source=SOURCE)
        return await ConnectionRepository(env.tx).save(
            conn, tenant_id=tenant, identity=identity, expected_revision=revision,
            document={"adapter_id": "shared.py", "adapter_revision": adapter, "enabled": enabled,
                      "settings": {"profile": "default", "options": {}},
                      "default_filters": {"agent_id": actor},
                      "schedule": {"enabled": schedule_enabled, "time": time, "timezone": zone,
                                   "window": "previous_day"}},
        )
    return _run(env, tenant, seed)


def run(env, app, *, tenant="default", now=NOW):
    async def execute():
        token = set_current_tenant(TenantContext(tenant))
        try:
            await enqueue_due(app, tenant, now=now)
        finally:
            reset_current_tenant(token)
    env.tx.pool.run(execute())


def payloads(env, tenant="default"):
    async def read(conn):
        rows = await conn.fetch(
            f"SELECT i.ingestion_id,i.dedupe_key,t.config_revision,t.adapter_revision,o.content "
            f"FROM {env.schema}.ingestions i JOIN {env.schema}.worker_tasks t ON t.task_id=i.task_id "
            f"JOIN {env.schema}.storage_objects o ON o.key=i.payload_ref "
            "ORDER BY i.received_at,i.ingestion_id")
        return [{**dict(row), "control": json.loads(json.loads(bytes(row["content"]))["payload"])} for row in rows]
    return _run(env, tenant, read)


def test_two_connections_share_adapter_and_day_without_schedule_collision(pg_env):
    app = services(pg_env)
    connection(pg_env, "business-a", actor="A")
    connection(pg_env, "business-b", actor="B")
    run(pg_env, app)
    rows = payloads(pg_env)
    assert len(rows) == 2 and len({r["dedupe_key"] for r in rows}) == 2
    assert {r["control"]["pull"]["filters"]["agent_id"] for r in rows} == {"A", "B"}
    for row in rows:
        pull = row["control"]["pull"]
        assert pull["from_timestamp"] == "2026-11-19T00:00:00+00:00"
        assert pull["to_timestamp"] == "2026-11-20T00:00:00+00:00"
        assert pull["connection"]["revision"] == row["adapter_revision"] == 1
        assert pull["connection"]["settings"] == {"profile": "default", "options": {}}
        assert "sref_" not in bytes(row["content"]).decode()
        assert "https://observed.example" not in bytes(row["content"]).decode()
    assert _run(pg_env, "default", lambda c: c.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.session_pull_runs")) == 2
    # A revised connection is eligible tomorrow, never a second copy today.
    first_control = copy.deepcopy(rows[0]["control"])
    new_adapter = _run(pg_env, "default", lambda conn: AdapterRepository(pg_env.tx).save_source(
        conn, tenant_id="default", adapter_id="shared.py", source=SOURCE + "\n# revision two"))
    connection(pg_env, "business-a", revision=1, actor="new-A", adapter_revision=new_adapter)
    run(pg_env, app)
    assert len(payloads(pg_env)) == 2 and payloads(pg_env)[0]["control"] == first_control
    run(pg_env, app, now=NOW + timedelta(days=1))
    tomorrow = payloads(pg_env)
    assert len(tomorrow) == 4
    revised = [r["control"]["pull"] for r in tomorrow if
               r["control"]["pull"]["connection"]["connection_id"] == "business-a"]
    assert {p["connection"]["revision"] for p in revised} == {1, 2}
    assert {p["connection"]["adapter_revision"] for p in revised} == {1, 2}
    assert {p["filters"]["agent_id"] for p in revised} == {"A", "new-A"}


def test_concurrent_workers_accept_once_and_other_tenant_has_independent_window(pg_env):
    app = services(pg_env)
    for tenant in ("default", "alpha"):
        connection(pg_env, "business-a", tenant=tenant)

    async def schedule():
        token = set_current_tenant(TenantContext("default"))
        try:
            await asyncio.gather(enqueue_due(app, "default", now=NOW), enqueue_due(app, "default", now=NOW))
        finally:
            reset_current_tenant(token)
    pg_env.tx.pool.run(schedule())
    assert len(payloads(pg_env)) == 1 and len(payloads(pg_env, "alpha")) == 0
    run(pg_env, app, tenant="alpha")
    assert len(payloads(pg_env, "alpha")) == 1
    assert payloads(pg_env)[0]["control"]["tenant_id"] == "default"
    assert payloads(pg_env, "alpha")[0]["control"]["tenant_id"] == "alpha"


def test_disabled_not_due_and_timezone_windows_are_respected(pg_env):
    app = services(pg_env)
    connection(pg_env, "disabled", enabled=False)
    connection(pg_env, "schedule-off", schedule_enabled=False)
    connection(pg_env, "later", time="23:59")
    connection(pg_env, "shanghai", time="19:00", zone="Asia/Shanghai")
    run(pg_env, app)
    rows = payloads(pg_env)
    assert len(rows) == 1
    pull = rows[0]["control"]["pull"]
    assert pull["connection"]["connection_id"] == "shanghai"
    assert pull["from_timestamp"] == "2026-11-19T00:00:00+08:00"
    assert pull["to_timestamp"] == "2026-11-20T00:00:00+08:00"


def test_schedule_compares_parsed_times_instead_of_lexical_strings(pg_env):
    app = services(pg_env)
    # strptime accepts this existing input; "12:00" < "1:00" lexically.
    connection(pg_env, "morning", time="1:00")
    run(pg_env, app)
    assert len(payloads(pg_env)) == 1


def test_legacy_default_schedule_still_runs_alongside_independent_connections(pg_env):
    app = services(pg_env)
    connection(pg_env, "business-a")
    async def enable(conn):
        await AdapterRepository(pg_env.tx).bind(conn, tenant_id="default", binding_key="datasource",
                                               adapter_id="shared.py", revision=1)
        repo = ConfigRepository(pg_env.tx)
        row = await repo.read_tenant_module(conn, "session")
        config = copy.deepcopy(DEFAULT_SESSION_CONFIG)
        config["datasource_schedule"].update(enabled=True, time="00:00", timezone="UTC")
        await repo.write_tenant_module(conn, module="session", expected_revision=row.revision, config=config)
    _run(pg_env, "default", enable)
    run(pg_env, app)
    rows = payloads(pg_env)
    assert len(rows) == 2
    assert sum("connection" in row["control"]["pull"] for row in rows) == 1
    legacy = next(row for row in rows if "connection" not in row["control"]["pull"])
    assert legacy["dedupe_key"].startswith("scheduled:{")
    run(pg_env, app)
    assert len(payloads(pg_env)) == 2


def test_one_invalid_connection_cannot_rollback_another_and_does_not_log_credentials(pg_env, monkeypatch, caplog):
    app = services(pg_env)
    connection(pg_env, "invalid")
    connection(pg_env, "valid")
    freeze = ConnectionRepository.freeze

    async def reject(self, conn, identity, revision=None):
        if identity == "invalid":
            raise ValueError("sensitive provider URL or credential must not reach the log")
        return await freeze(self, conn, identity, revision=revision)
    monkeypatch.setattr(ConnectionRepository, "freeze", reject)
    run(pg_env, app)
    rows = payloads(pg_env)
    assert len(rows) == 1 and rows[0]["control"]["pull"]["connection"]["connection_id"] == "valid"
    assert "connection=invalid" in caplog.text and "sensitive provider" not in caplog.text
