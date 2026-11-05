import asyncio

import httpx
import pytest
from fastapi import FastAPI

from team_ma.api import create_router
from team_ma.repository import MARepository
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant

pytestmark = pytest.mark.postgres


def test_console_rbac_secret_redaction_cas_and_data_plane_etag(pg_env):
    services = build_role_services(role="api", tx=pg_env.tx, config=TeamEvolverConfig(sharing_viking_account="alpha"))
    services.extras["secret_repo"] = SecretRepository(pg_env.tx, SecretBox("test-key"))
    app = FastAPI()
    app.include_router(create_router(services))

    @app.middleware("http")
    async def identity(request, call_next):
        tenant = request.headers.get("x-test-tenant", "alpha")
        token = set_current_tenant(TenantContext(tenant))
        request.state.console_user = {"id": "operator", "tenant_id": tenant, "capabilities":
                                     ["tenant.manage"] if request.headers.get("x-test-role") == "admin" else []}
        request.state.tenant_source = ("token" if request.headers.get("authorization") == "Bearer tevt_test"
                                       else "console")
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = {"connection_id": "generic", "display_name": "Editor", "entry_agent_id": "root",
                    "environment_id": "env", "default_user_id": "employee", "ma_api_key": "private-ark-key",
                    "tenant_token": "tevt_test"}
            assert (await client.post("/api/ma/connections", json=body)).status_code == 403
            headers = {"x-test-role": "admin"}
            creation_headers = {**headers, "Idempotency-Key": "creation"}
            created = await client.post("/api/ma/connections", json=body, headers=creation_headers)
            assert created.status_code == 202
            assert "private-ark-key" not in created.text and "tevt_test" not in created.text
            assert created.json()["connection"]["config"]["ma_credential_configured"]
            repeated = await client.post("/api/ma/connections", json=body, headers=creation_headers)
            assert repeated.status_code == 202 and repeated.json()["job_id"] == created.json()["job_id"]
            changed = await client.post("/api/ma/connections", json={**body, "display_name": "Changed"},
                                        headers=creation_headers)
            assert changed.status_code == 409
            updated = await client.put("/api/ma/connections/generic", headers=headers,
                                       json={"expected_revision": 1, "enabled": False})
            assert updated.status_code == 200 and updated.json()["revision"] == 2
            assert (await client.put("/api/ma/connections/generic", headers=headers,
                                     json={"expected_revision": 1, "enabled": True})).status_code == 409
            cross = await client.get("/api/ma/connections/generic", headers={"x-test-tenant": "beta"})
            assert cross.status_code == 404
            assert (await client.post("/api/ma/connections/generic/collect", headers=headers,
                                      json={"session_ids": ["one", {}]})).status_code == 400
            assert (await client.post("/api/ma/connections/generic/collect", headers=headers,
                                      json={"from_timestamp": "yesterday"})).status_code == 400
            first = await client.post("/api/ma/connections/generic/discover",
                                      headers={**headers, "Idempotency-Key": "same"}, json={})
            again = await client.post("/api/ma/connections/generic/discover",
                                      headers={**headers, "Idempotency-Key": "same"}, json={})
            assert first.json()["job_id"] == again.json()["job_id"] and again.json()["dedupe"]
            assert (await client.get("/sync/ma/generic?user_id=employee")).status_code == 401
            assert (await client.get("/sync/ma/generic?user_id=employee",
                                     headers={"Authorization": "Bearer tevt_test"})).status_code == 503
            async with pg_env.tx.tenant_tx("alpha") as conn:
                repo = MARepository(pg_env.tx)
                await repo.save_release(conn, tenant_id="alpha", connection_id="generic", release_id="release",
                                        manifest_hash="tree", status="applied",
                                        record={"release_id": "release", "entry_agent_version": "2",
                                                "environment": {"config": {"setup_script": "private-script"}}})
                await repo.bind(conn, tenant_id="alpha", connection_id="generic", kind="agent", name="root",
                                record={"id": "root", "snapshot": {"system": "private-script"}})
            public = await client.get("/api/ma/connections/generic/resources")
            assert "private-script" not in public.text
            published = await client.get("/sync/ma/generic?user_id=employee",
                                         headers={"Authorization": "Bearer tevt_test"})
            assert published.status_code == 200 and "private-script" not in published.text
            cached = await client.get("/sync/ma/generic?user_id=employee", headers={
                "Authorization": "Bearer tevt_test", "If-None-Match": published.headers["etag"],
            })
            assert cached.status_code == 304
    asyncio.run(run())
