"""Server-attested tenant projection with real PG token lookup and same-name profiles."""
from __future__ import annotations

import json

import pytest

from team_skills.execution.repository import ExecutionRepository
from teamEvolver.tenants.registry import TenantRegistry, hash_agent_token
from tests.support.app import _login, _pod
from tests.support.execution import profile_raw
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres

PATH = ("/sync/skills?user_id=same-user&execution_profile_id=same-profile"
        "&execution_capability=teamevolver.execution-plan.v1")
TOKENS = {tenant: f"tevt_isolated_test_{tenant}" for tenant in ("alpha", "beta")}


def seed(env, *, same_content):
    releases = {}
    for tenant in TOKENS:
        async def create(conn):
            await conn.execute(f"UPDATE {env.schema}.tenants SET agent_token_hash=$2,config=$3::jsonb "
                               "WHERE tenant_id=$1", tenant, hash_agent_token(TOKENS[tenant]),
                               json.dumps({"sharing_viking_account": f"acct-{tenant}"}))
            raw = profile_raw("same-profile", user_id="same-user")
            raw["agent_id"] = "same-agent"
            raw["affected_skill_ids"] = []
            if not same_content:
                raw["components"][0]["content"] = f"Business instructions belonging to {tenant}"
            return await ExecutionRepository(env.tx).register(
                conn, tenant_id=tenant, raw=raw, skill_dependencies=[], actor="fixture",
            )
        releases[tenant] = _run(env, tenant, create)[1]
    return releases


def pod(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from teamEvolver.app import create_app

    fixture = _pod(env, tmp_path, monkeypatch, pod="tenant-projection")
    services = fixture.app.state.services
    # Use the production hashed-token resolver over the isolated PG tenants,
    # not a test resolver that merely trusts a tenant header or token suffix.
    services.extras["tenant_registry"] = TenantRegistry(runtime=env.tx.pool, config=services.extras["config"])
    return TestClient(create_app(services=services), base_url="http://192.0.2.10:1080")


@pytest.mark.parametrize("same_content", [True, False])
def test_same_named_profiles_and_agents_return_actual_token_tenant_and_isolated_etags(
    pg_env, tmp_path, monkeypatch, same_content,
):
    releases = seed(pg_env, same_content=same_content)
    with pod(pg_env, tmp_path, monkeypatch) as client:
        packets = {}
        for tenant, token in TOKENS.items():
            headers = {"Authorization": "Bearer " + token}
            response = client.get(PATH, headers=headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["tenant_id"] == tenant
            assert payload["execution"]["profile_id"] == "same-profile"
            assert payload["execution"]["release_id"] == releases[tenant]["release_id"]
            assert payload["execution"]["document"] == releases[tenant]["document"]
            assert payload["skills"] == [] and payload["total"] == 0
            assert token not in response.text
            assert client.get(PATH, headers={**headers, "If-None-Match": response.headers["ETag"]}).status_code == 304
            legacy = client.get("/sync/skills?user_id=same-user", headers=headers)
            assert legacy.json()["tenant_id"] == tenant and legacy.json()["status"] == "ok"
            assert "execution" not in legacy.json()
            packets[tenant] = response
        # Identical business names/content must still occupy different caches.
        assert packets["alpha"].headers["ETag"] != packets["beta"].headers["ETag"]
        beta = client.get(PATH, headers={"Authorization": "Bearer " + TOKENS["beta"],
                                        "If-None-Match": packets["alpha"].headers["ETag"]})
        assert beta.status_code == 200 and beta.json()["tenant_id"] == "beta"
        mismatch = client.get(PATH, headers={"Authorization": "Bearer " + TOKENS["alpha"], "X-Tenant-ID": "beta"})
        assert mismatch.status_code == 403
        query = client.get(PATH + "&tenant_id=beta", headers={"Authorization": "Bearer " + TOKENS["alpha"]})
        assert query.json()["tenant_id"] == "alpha"
        assert client.get(PATH, headers={"Authorization": "Bearer tevt_unknown"}).status_code == 401


def test_integration_uses_authorized_server_tenant_and_keeps_console_machine_boundary(pg_env, tmp_path, monkeypatch):
    releases = seed(pg_env, same_content=False)
    path = "/api/skill-evolution/execution-profiles/same-profile/integration"
    with pod(pg_env, tmp_path, monkeypatch) as client:
        _login(client, bootstrap=True)
        for tenant in TOKENS:
            response = client.get(path, headers={"X-Tenant-ID": tenant})
            assert response.status_code == 200, response.text
            document = response.json()
            assert document["tenant_id"] == tenant
            assert document["current_release_id"] == releases[tenant]["release_id"]
            observation = document["metadata"]["teamevolver_execution"]
            assert observation["agent_id"] == "same-agent" and observation["execution_profile_id"] == "same-profile"
            assert observation["composition_hash"] == releases[tenant]["composition_hash"]
        rejected = client.get(path, headers={"Authorization": "Bearer " + TOKENS["alpha"]})
        assert rejected.status_code == 403  # Adding a projection does not grant console access to a machine token.
    assert _run(pg_env, "alpha", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.worker_tasks")) == 0
