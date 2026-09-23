from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from session_ingestion.pull import scheduler
from teamEvolver.tenants.registry import TenantContext, get_current_tenant


def _schedule(**overrides):
    return {
        **scheduler.DEFAULT_SCHEDULE,
        "enabled": True,
        **overrides,
    }


def test_previous_day_window_uses_local_calendar_day():
    window = scheduler.previous_day_window(
        _schedule(timezone="Asia/Shanghai"),
        now=datetime(2026, 9, 18, 1, 30, tzinfo=timezone.utc),
    )

    assert window == {
        "target_date": "2026-09-17",
        "from_timestamp": "2026-09-16T16:00:00Z",
        "to_timestamp": "2026-09-17T16:00:00Z",
        "scheduled_at": "2026-09-17T16:00:00Z",
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"time": "0:00"}, "HH:MM"),
        ({"timezone": "Mars/Olympus"}, "Unknown schedule timezone"),
        ({"window": "last_24_hours"}, "previous_day"),
        ({"max_sessions": 0}, "between 1 and 1000"),
        ({"unexpected": True}, "Unsupported schedule fields"),
    ],
)
def test_schedule_validation_rejects_invalid_values(value, message):
    with pytest.raises(ValueError, match=message):
        scheduler.normalize_schedule(value)


def test_non_default_tenant_does_not_inherit_default_schedule():
    config = SimpleNamespace(datasource_schedule=_schedule(time="03:15"))
    default = TenantContext()
    tenant = TenantContext("tenant-a")
    configured_tenant = TenantContext(
        "tenant-b",
        config_overrides={"datasource_schedule": _schedule(time="05:45")},
    )

    assert scheduler.schedule_for(config, default)["time"] == "03:15"
    assert scheduler.schedule_for(config, tenant) == scheduler.DEFAULT_SCHEDULE
    assert scheduler.schedule_for(config, configured_tenant)["time"] == "05:45"


@pytest.mark.asyncio
async def test_runtime_pull_binds_tenant_context(monkeypatch, tmp_path):
    owner = SimpleNamespace(config=SimpleNamespace())
    tenant = TenantContext("tenant-a")
    seen = {}

    async def fake_pull_sessions(config, current_tenant, ingest, body):
        seen["context"] = get_current_tenant()
        seen["tenant"] = current_tenant
        seen["body"] = body
        return {"total": 0, "counts": {}, "results": []}

    monkeypatch.setattr(scheduler, "pull_sessions", fake_pull_sessions)
    runtime = scheduler.DatasourcePullRuntime(
        owner,
        state_path=tmp_path / "state.json",
    )

    await runtime.pull(tenant, {"max_sessions": 10})

    assert seen == {
        "context": tenant,
        "tenant": tenant,
        "body": {"max_sessions": 10},
    }
    assert get_current_tenant() is None


@pytest.mark.asyncio
async def test_scheduled_pull_queues_window_and_persists_status(monkeypatch, tmp_path):
    config = SimpleNamespace(datasource_schedule=_schedule(max_sessions=37))
    owner = SimpleNamespace(config=config)
    tenant = TenantContext()
    bodies = []

    monkeypatch.setattr(
        scheduler.adapters,
        "describe",
        lambda config, tenant: {
            "file": "default.py",
            "configured": True,
            "enabled": True,
            "supported_filters": ["from_timestamp", "to_timestamp"],
        },
    )
    monkeypatch.setattr(
        scheduler.adapters,
        "binding",
        lambda config, tenant: "default.py",
    )

    async def fake_pull_sessions(config, tenant, ingest, body):
        bodies.append(body)
        return {
            "total": 2,
            "counts": {"queued": 2, "error": 0},
            "results": [],
        }

    monkeypatch.setattr(scheduler, "pull_sessions", fake_pull_sessions)
    runtime = scheduler.DatasourcePullRuntime(
        owner,
        state_path=tmp_path / "state.json",
    )

    accepted = await runtime.trigger(tenant, force=True)
    task = runtime._jobs[tenant.tenant_id]
    await task
    status = runtime.status(tenant)

    assert accepted["running"] is True
    assert bodies == [
        {
            "from_timestamp": bodies[0]["from_timestamp"],
            "to_timestamp": bodies[0]["to_timestamp"],
            "max_sessions": 37,
            "defer_evolution_trigger": True,
        }
    ]
    assert status["last_status"] == "succeeded"
    assert status["last_total"] == 2
    assert status["last_counts"] == {"queued": 2, "error": 0}
    assert status["last_target_date"]
    assert status["last_window_from"] == bodies[0]["from_timestamp"]
    assert status["last_window_to"] == bodies[0]["to_timestamp"]
