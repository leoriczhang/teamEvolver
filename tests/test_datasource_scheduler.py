import asyncio
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
        ({"unexpected": True}, "Unsupported schedule fields"),
    ],
)
def test_schedule_validation_rejects_invalid_values(value, message):
    with pytest.raises(ValueError, match=message):
        scheduler.normalize_schedule(value)


def test_schedule_discards_legacy_session_limit():
    assert "max_sessions" not in scheduler.normalize_schedule(
        {"max_sessions": 1000}
    )


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

    async def fake_pull_sessions(
        config,
        current_tenant,
        ingest,
        body,
        *,
        progress=None,
    ):
        seen["context"] = get_current_tenant()
        seen["tenant"] = current_tenant
        seen["body"] = body
        return {"total": 0, "counts": {}, "results": []}

    monkeypatch.setattr(scheduler, "pull_sessions", fake_pull_sessions)
    runtime = scheduler.DatasourcePullRuntime(
        owner,
        state_path=tmp_path / "state.json",
    )

    await runtime.pull(tenant, {"from_timestamp": "2026-09-01T00:00:00Z"})

    assert seen == {
        "context": tenant,
        "tenant": tenant,
        "body": {"from_timestamp": "2026-09-01T00:00:00Z"},
    }
    assert get_current_tenant() is None


@pytest.mark.asyncio
async def test_scheduled_pull_queues_window_and_persists_status(monkeypatch, tmp_path):
    config = SimpleNamespace(datasource_schedule=_schedule())
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

    async def fake_pull_sessions(
        config,
        tenant,
        ingest,
        body,
        *,
        progress=None,
    ):
        bodies.append(body)
        if progress is not None:
            await progress({"total": 2, "counts": {"queued": 2}})
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
            "defer_evolution_trigger": True,
        }
    ]
    assert status["last_status"] == "succeeded"
    assert status["last_total"] == 2
    assert status["last_counts"] == {"queued": 2, "error": 0}
    assert status["last_target_date"]
    assert status["last_window_from"] == bodies[0]["from_timestamp"]
    assert status["last_window_to"] == bodies[0]["to_timestamp"]


@pytest.mark.asyncio
async def test_manual_pull_runs_in_background_and_reports_progress(
    monkeypatch,
    tmp_path,
):
    owner = SimpleNamespace(config=SimpleNamespace())
    tenant = TenantContext("tenant-a")
    release = asyncio.Event()

    monkeypatch.setattr(
        scheduler.adapters,
        "describe",
        lambda *_args: {
            "file": "tenant-a.py",
            "configured": True,
            "enabled": True,
            "supported_filters": ["from_timestamp", "to_timestamp"],
            "required_filters": ["from_timestamp", "to_timestamp"],
            "max_sessions": 100,
        },
    )

    async def fake_pull_sessions(
        config,
        current_tenant,
        ingest,
        body,
        *,
        progress=None,
    ):
        await progress({"total": 100, "counts": {"queued": 100}})
        await release.wait()
        return {
            "total": 125,
            "counts": {"queued": 125, "error": 0},
            "results": [{"session_id": "s-1", "status": "queued"}],
            "results_truncated": True,
            "result_limit": 1,
        }

    monkeypatch.setattr(scheduler, "pull_sessions", fake_pull_sessions)
    runtime = scheduler.DatasourcePullRuntime(
        owner,
        state_path=tmp_path / "state.json",
    )
    body = {
        "from_timestamp": "2026-09-01T00:00:00Z",
        "to_timestamp": "2026-10-01T00:00:00Z",
    }

    accepted = await runtime.trigger_manual(tenant, body)
    await asyncio.sleep(0)
    running = runtime.status(tenant)
    task = runtime._jobs[tenant.tenant_id]

    assert accepted["running"] is True
    assert running["job_type"] == "manual"
    assert running["current_total"] == 100

    release.set()
    await task
    completed = runtime.status(tenant)

    assert completed["running"] is False
    assert completed["last_status"] == "succeeded"
    assert completed["last_total"] == 125
    assert completed["last_results_truncated"] is True
