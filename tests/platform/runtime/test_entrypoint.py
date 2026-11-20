from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import teamEvolver.app as application
import teamEvolver.runtime.entrypoint as entrypoint
from teamEvolver.config import TeamEvolverConfig


@pytest.mark.parametrize("role", ["worker", "api", "all"])
def test_runtime_honors_configured_database_pool_limits(monkeypatch, role):
    pool = SimpleNamespace(schema="test_schema")
    factory = Mock(return_value=pool)
    monkeypatch.setattr(entrypoint, "get_database_pool", factory)
    monkeypatch.setattr(application, "build_role_services", Mock(return_value=object()))
    monkeypatch.setattr(application, "create_app", Mock(return_value=object()))
    monkeypatch.setattr(entrypoint, "build_lifecycle", Mock(return_value=object()))
    monkeypatch.setattr(entrypoint, "build_health_app", Mock(return_value=object()))
    monkeypatch.setattr(entrypoint, "_serve_uvicorn", AsyncMock())
    config = TeamEvolverConfig(
        storage_pg_dsn="postgresql://localhost/example", storage_pg_schema="test_schema",
        storage_pg_pool_min=1, storage_pg_pool_max=3, storage_pg_command_timeout_seconds=12,
    )
    entrypoint.run_role(role=role, config=config)
    factory.assert_called_once_with(
        dsn=config.storage_pg_dsn, schema="test_schema", ssl=config.storage_pg_ssl,
        pool_min=1, pool_max=3, command_timeout=12,
    )


@pytest.mark.asyncio
async def test_ma_lane_remains_available_while_analysis_is_blocked(monkeypatch):
    import asyncio

    blocked, connected, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    health, lanes = [], []

    async def worker(services, *, kinds, on_health, **kwargs):
        lanes.append(set(kinds))
        if "session.analyze" in kinds:
            blocked.set()
            on_health(False)
            await stop.wait()
        else:
            await blocked.wait()
            on_health(True)
            connected.set()
            await stop.wait()

    monkeypatch.setattr(entrypoint, "run_worker", worker)
    services = SimpleNamespace(registry=SimpleNamespace(kinds=lambda: {
        "session.analyze", "ma.collect", "ma.discover", "ma.reconcile",
    }))
    task = asyncio.create_task(entrypoint._run_worker_lanes(
        services, owner_id="one", stop_event=stop, tenant_source=AsyncMock(), on_health=health.append,
    ))
    try:
        await asyncio.wait_for(connected.wait(), 1)
        assert len(lanes) == 2 and not lanes[0] & lanes[1]
        assert set.union(*lanes) == services.registry.kinds()
        assert health[-1] is False  # a healthy MA lane cannot mask the other lane's failure
    finally:
        stop.set()
        await task
