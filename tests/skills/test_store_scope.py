"""Cloud Skill snapshots must reach endpoints through nested FastAPI routers."""
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from team_skills.api.store_scope import install
from team_skills.library.store import snapshot
from teamEvolver.managed_agents import MAError


@pytest.mark.parametrize("nested", [False, True])
def test_workbench_uses_cloud_snapshot_instead_of_stale_team_version(nested):
    calls = []
    services = SimpleNamespace(tx=SimpleNamespace(), extras={})

    class Provider:
        async def load(self, tenant, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(provider=self, tenant=tenant, version=53)

    provider = Provider()
    provider.services = services
    services.extras["cloud_skill_provider"] = provider
    workbench = APIRouter()

    @workbench.get("/api/replay-lab/workspace")
    async def workspace():
        value = snapshot(services.tx)
        return {"version": value.version if value else 46}

    router = APIRouter()
    if nested:
        inner = APIRouter()
        inner.include_router(workbench)
        router.include_router(inner)
    else:
        router = workbench
    install(router, services)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/replay-lab/workspace?connection_id=codingvideo")
    assert response.status_code == 200
    assert response.json()["version"] == 53
    assert calls == [{"versions": None, "connection_id": "codingvideo"}]


@pytest.mark.parametrize("path,method", [
    ("/api/replay-lab/runs/historical", "GET"),
    ("/api/replay-lab/datasets", "GET"),
    ("/api/replay-lab/datasets", "POST"),
])
def test_historical_reads_and_dataset_edits_do_not_depend_on_cloud(path, method):
    services = SimpleNamespace(tx=SimpleNamespace(), extras={})

    class Provider:
        async def load(self, *args, **kwargs):
            raise AssertionError("Historical experiments must remain available offline")

    services.extras["cloud_skill_provider"] = Provider()
    child = APIRouter()

    @child.api_route(path, methods=[method])
    async def historical():
        assert snapshot(services.tx) is None
        return {"version": 51}

    router = APIRouter()
    router.include_router(child)
    install(router, services)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).request(method, path)
    assert response.status_code == 200
    assert response.json()["version"] == 51


@pytest.mark.parametrize("fail", [False, True])
def test_nested_cloud_write_is_accepted_or_reports_cloud_failure(fail):
    services = SimpleNamespace(tx=SimpleNamespace(), extras={})

    class Provider:
        async def load(self, tenant, **kwargs):
            if fail:
                raise MAError(503, "CloudUnavailable")
            return SimpleNamespace(provider=self, tenant=tenant)

    provider = Provider()
    provider.services = services
    services.extras["cloud_skill_provider"] = provider
    child = APIRouter()

    @child.put("/api/replay-lab/workspace/note/file")
    async def write():
        assert snapshot(services.tx) is not None
        return {"status": "accepted", "job_id": "ma-skill:write"}

    router = APIRouter()
    router.include_router(child)
    install(router, services)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).put("/api/replay-lab/workspace/note/file")
    assert response.status_code == (503 if fail else 202)
    assert response.json() == (
        {"detail": "MA HTTP 503: CloudUnavailable"} if fail else {"status": "accepted", "job_id": "ma-skill:write"}
    )
