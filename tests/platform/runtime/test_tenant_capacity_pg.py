"""Actual PostgreSQL runtime config CAS/RLS and exclusive worker ownership."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.capacity import TenantCapacity
from teamEvolver.runtime.leases import LeaseGuard
from teamEvolver.runtime.repository import ResourceLeaseRepository
from teamEvolver.tenants.api import create_router

pytestmark = pytest.mark.postgres


def test_runtime_api_cas_validation_and_tenant_isolation(pg_env):
    registry = SimpleNamespace(get=lambda tid: SimpleNamespace(tenant_id=tid) if tid in {"alpha", "beta"} else None)
    services = SimpleNamespace(tx=pg_env.tx, extras={
        "config": TeamEvolverConfig(), "config_repo": ConfigRepository(pg_env.tx), "tenant_registry": registry,
    })
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.console_user = {"tenant_id": "alpha", "service_root": True}
        return await call_next(request)

    app.include_router(create_router(services))
    with TestClient(app) as http:
        url = "/api/tenants/alpha/runtime-config"
        initial = http.get(url)
        assert initial.status_code == 200
        assert initial.json()["config"] == asdict(TenantCapacity())
        changed = http.put(url, json={"expected_revision": 1, "config": {"analysis_concurrency": 4}})
        assert changed.status_code == 200 and changed.json()["revision"] == 2
        assert changed.json()["config"]["analysis_concurrency"] == 4
        assert http.put(url, json={"expected_revision": 1, "config": {"analysis_concurrency": 2}}).status_code == 409
        for invalid in ({"analysis_concurrency": 0}, {"llm_max_concurrency": True},
                        {"llm_max_concurrency": 8, "llm_queue_capacity": 2}, {"unknown": 1}):
            assert http.put(url, json={"expected_revision": 2, "config": invalid}).status_code == 400
        assert http.get("/api/tenants/beta/runtime-config").status_code == 403
        assert http.get(url).json()["config"]["analysis_concurrency"] == 4

    async def beta(conn):
        return (await services.extras["config_repo"].read_tenant_module(conn, "runtime")).config

    assert pg_env.tx.run_tenant_tx("beta", beta, timeout=30) == {}


@pytest.mark.asyncio
async def test_owner_is_exclusive_across_replicas_and_expired_owner_is_fenced(pg_env):
    import asyncio

    guard = LeaseGuard(ResourceLeaseRepository(pg_env.tx))

    async def acquire(tenant, owner):
        async with pg_env.tx.tenant_tx(tenant) as conn:
            return await guard.acquire(conn, tenant_id=tenant, resource_key="runtime:tenant-worker",
                                       owner_id=owner, lease_seconds=60)

    results = await asyncio.gather(acquire("alpha", "a"), acquire("alpha", "b"))
    winners = [handle for handle in results if handle is not None]
    assert len(winners) == 1
    original = winners[0]
    assert await acquire("beta", "b") is not None
    async with pg_env.tx.tenant_tx("alpha") as conn:
        await conn.execute(f"UPDATE {pg_env.schema}.resource_leases SET lease_until=now()-interval '1 second'")
    replacement = await acquire("alpha", "replacement")
    assert replacement and replacement.fence > original.fence
    async with pg_env.tx.tenant_tx("alpha") as conn:
        assert not await guard.heartbeat(conn, original, lease_seconds=60)
        assert not await guard.release(conn, original)
        assert await guard.heartbeat(conn, replacement, lease_seconds=60)
