import asyncio

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.discovery import discover_connection
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from team_skills.transfer.handler import transfer
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient
from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.managed_agents import skill_zip

pytestmark = pytest.mark.postgres


def test_discovery_imports_through_real_worker_and_records_shared_skill_once(pg_env):
    repo = MARepository(pg_env.tx)
    secret_repo = SecretRepository(pg_env.tx, SecretBox("test-key"))
    services = build_role_services(role="api", tx=pg_env.tx, config=TeamEvolverConfig())
    services.extras["secret_repo"] = secret_repo
    archive = skill_zip()

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        if path == "agents/root":
            return httpx.Response(200, json={"id": "root", "version": 1, "name": "editor",
                                            "multiagent": {"type": "coordinator", "agents": [
                                                {"type": "agent", "id": "writer", "version": 1}]},
                                            "skills": [{"type": "custom", "skill_id": "skill-note", "version": "1"}]})
        if path == "agents/writer":
            return httpx.Response(200, json={"id": "writer", "version": 1, "name": "writer",
                                            "skills": [{"type": "custom", "skill_id": "skill-note", "version": "1"}]})
        if path.startswith("environments/"):
            return httpx.Response(200, json={"id": "env", "config": {"type": "cloud"}})
        if path.endswith("/content"):
            return httpx.Response(302, headers={"Location": "https://files.example/skill"})
        if request.url.host == "files.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=archive)
        raise AssertionError(path)

    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            async with pg_env.tx.tenant_tx("alpha") as conn:
                ma = await secret_repo.store(conn, tenant_id="alpha", plaintext="ark")
                pull = await secret_repo.store(conn, tenant_id="alpha", plaintext="tevt_test")
                config = validate_config({"display_name": "Example", "entry_agent_id": "root", "environment_id": "env",
                                          "default_user_id": "operator", "ma_secret_ref": ma.ref,
                                          "tenant_token_ref": pull.ref})
                await repo.save(conn, tenant_id="alpha", connection_id="connection", config=config)
                await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.discover",
                              operation_id="discover")
                task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.discover"])
                connection = await repo.connection(conn, "connection")
                operation = await repo.operation(conn, "discover")
            async with MAClient("https://ma.example/api/v3", "ark", transport=httpx.MockTransport(respond)) as client:
                first = await discover_connection(task, services, client, connection, operation)
                assert first["waiting"]
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    transfer_task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300,
                                                               kinds=["skill.transfer"])
                assert (await transfer(transfer_task, services)).outcome == "succeed"
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    operation = await repo.operation(conn, "discover")
                second = await discover_connection(task, services, client, connection, operation)
                assert second["agent_count"] == 2
                assert second["managed_skill_count"] == 1
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    resource = (await repo.resources(conn, "connection", kind="skill"))[0]
                    assert set(resource["record"]["agents"]) == {"root", "writer"}
                    assert (await repo.applied(conn, "connection"))["status"] == "applied"
        finally:
            reset_current_tenant(token)
    asyncio.run(run())
