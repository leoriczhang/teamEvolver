"""Connection failures isolate collection from synchronization and survive retries."""

import asyncio

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.repository import MARepository
from team_ma.schedule import tick
from team_ma.schemas import validate_config
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient
from teamEvolver.runtime import process_once
from teamEvolver.security import SecretBox, SecretRepository

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("retryable", [False, True])
def test_operation_errors_recover_independently_and_control_dispatch(pg_env, retryable):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="api", tx=pg_env.tx, config=TeamEvolverConfig())

    async def run():
        async with pg_env.tx.tenant_tx("alpha") as conn:
            config = validate_config({
                "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
                "default_user_id": "operator", "ma_secret_ref": "ark", "tenant_token_ref": "pull",
            })
            await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            await repo.save_release(conn, tenant_id="alpha", connection_id="one", release_id="initial",
                                    manifest_hash="initial", record={}, status="applied")
            await repo.status(conn, "one", "ready", {"agent_count": 2})
            await repo.operation_status(conn, "one", "ma.reconcile", error="sync_failed",
                                        job_id="sync", retryable=retryable)
            await repo.operation_status(conn, "one", "ma.collect", error="ma_TransportFailed",
                                        job_id="collect", retryable=True)
            await repo.operation_status(conn, "one", "ma.collect")
            connection = await repo.connection(conn, "one")
            assert connection["status"] == "error"
            assert connection["detail"]["error"] == "sync_failed"
            assert set(connection["detail"]["operation_errors"]) == {"ma.reconcile"}
            assert connection["detail"]["agent_count"] == 2
            assert connection["last_collected_at"]
            assert not connection["last_synced_at"]
        await tick(services, tenant_id="alpha")
        async with pg_env.tx.tenant_tx("alpha") as conn:
            kinds = {r["kind"] for r in await conn.fetch(
                f"SELECT kind FROM {repo.schema}.ma_operations WHERE connection_id='one'")}
            assert kinds == ({"ma.collect", "ma.reconcile"} if retryable else {"ma.collect"})
            await repo.operation_status(conn, "one", "ma.reconcile")
            connection = await repo.connection(conn, "one")
            assert connection["status"] == "ready"
            assert connection["detail"] == {"agent_count": 2}
            assert connection["last_synced_at"]

    asyncio.run(run())


def test_real_worker_transport_exhaustion_resumes_without_erasing_sync_failure(pg_env):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
    secrets = SecretRepository(pg_env.tx, SecretBox("test-master"))
    services.extras["secret_repo"] = secrets
    broken = [True]

    def ma_response(request):
        if request.url.path.endswith("/agents/root"):
            return httpx.Response(200, json={"id": "wrong-root", "version": 1})
        if broken[0]:
            raise httpx.ReadTimeout("temporary read failure", request=request)
        assert request.url.path.endswith("/sessions")
        return httpx.Response(200, json={"data": []})

    services.extras["ma_client_factory"] = lambda url, key: MAClient(
        url, key, transport=httpx.MockTransport(ma_response))

    async def run():
        async with pg_env.tx.tenant_tx("alpha") as conn:
            ma = await secrets.store(conn, tenant_id="alpha", plaintext="ark")
            pull = await secrets.store(conn, tenant_id="alpha", plaintext="pull")
            config = validate_config({
                "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
                "default_user_id": "operator", "ma_secret_ref": ma.ref, "tenant_token_ref": pull.ref,
            })
            await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            await repo.save_release(conn, tenant_id="alpha", connection_id="one", release_id="initial",
                                    manifest_hash="initial", record={}, status="applied")
            await repo.status(conn, "one", "ready")
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.reconcile",
                          operation_id="sync")
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.collect",
                          operation_id="collect")
        for kind in ("ma.reconcile", "ma.collect"):
            await process_once(services, owner_id="worker", tenants=["alpha"], kinds=[kind], max_attempts=1)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            connection = await repo.connection(conn, "one")
            errors = connection["detail"]["operation_errors"]
            assert errors["ma.reconcile"]["retryable"] is False
            assert errors["ma.collect"]["retryable"] is True
            assert (await services.tasks.get(conn, task_id="collect"))["status"] == "failed"
            await conn.execute(f"UPDATE {repo.schema}.ma_operations SET created_at=now()-interval '2 days'")
        broken[0] = False
        await tick(services, tenant_id="alpha")
        task = await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.collect"])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await services.tasks.get(conn, task_id=task.task_id))["status"] == "succeeded"
            connection = await repo.connection(conn, "one")
            assert connection["last_collected_at"]
            assert connection["status"] == "error"
            assert set(connection["detail"]["operation_errors"]) == {"ma.reconcile"}
            assert (await services.tasks.get(conn, task_id="sync"))["error_code"] == "ma_validation_failed"

    asyncio.run(run())


@pytest.mark.parametrize("kind,code,expected", [
    ("ma.collect", "ma_TransportFailed", {"ma.collect", "ma.reconcile"}),
    ("ma.reconcile", "Published manifest omission is not a confirmed Skill deletion", {"ma.collect"}),
    ("ma.reconcile", "drift: managed Skill was changed externally: notes", {"ma.collect"}),
    ("ma.discover", "ma_TransportFailed", set()),
])
def test_legacy_error_state_dispatches_only_recoverable_operations(pg_env, kind, code, expected):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="api", tx=pg_env.tx, config=TeamEvolverConfig())

    async def run():
        async with pg_env.tx.tenant_tx("alpha") as conn:
            config = validate_config({
                "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
                "default_user_id": "operator", "ma_secret_ref": "ark", "tenant_token_ref": "pull",
            })
            await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            await repo.save_release(conn, tenant_id="alpha", connection_id="one", release_id="initial",
                                    manifest_hash="initial", record={}, status="applied")
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind=kind,
                          operation_id="failed")
            await conn.execute(f"UPDATE {repo.schema}.worker_tasks SET status='failed' WHERE task_id='failed'")
            await conn.execute(f"UPDATE {repo.schema}.ma_operations SET created_at=now()-interval '2 days'")
            await repo.status(conn, "one", "error", {"error": code, "job_id": "failed"})
        await tick(services, tenant_id="alpha")
        async with pg_env.tx.tenant_tx("alpha") as conn:
            kinds = {r["kind"] for r in await conn.fetch(
                f"SELECT kind FROM {repo.schema}.ma_operations WHERE operation_id<>'failed'")}
            assert kinds == expected
            if kind == "ma.reconcile":
                await repo.operation_status(conn, "one", "ma.collect")
                assert (await repo.connection(conn, "one"))["detail"]["error"] == code

    asyncio.run(run())


def test_connection_lock_contention_does_not_exhaust_execution_attempts(pg_env):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
    secrets = SecretRepository(pg_env.tx, SecretBox("test-key"))
    services.extras["secret_repo"] = secrets
    services.extras["ma_client_factory"] = lambda url, key: MAClient(
        url, key, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})))

    async def run():
        async with pg_env.tx.tenant_tx("alpha") as conn:
            secret = await secrets.store(conn, tenant_id="alpha", plaintext="test")
            config = validate_config({
                "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
                "default_user_id": "operator", "ma_secret_ref": secret.ref, "tenant_token_ref": secret.ref,
            })
            await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            await repo.status(conn, "one", "ready")
            await enqueue(conn, services, tenant_id="alpha", connection_id="one",
                          kind="ma.collect", operation_id="blocked")
            fence = await services.leases.acquire(
                conn, tenant_id="alpha", resource_key="ma-connection:one", owner_id="other", lease_seconds=60)
        for _ in range(5):
            await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.collect"])
            async with pg_env.tx.tenant_tx("alpha") as conn:
                row = await services.tasks.get(conn, task_id="blocked")
                assert row["status"] == "retry_wait" and row["attempt"] == 0
                await conn.execute(f"UPDATE {repo.schema}.worker_tasks SET available_at=now() WHERE task_id='blocked'")
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await services.leases.release(conn, tenant_id="alpha", resource_key="ma-connection:one",
                                          owner_id="other", fence=fence)
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.collect"])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            row = await services.tasks.get(conn, task_id="blocked")
            assert row["status"] == "succeeded" and row["attempt"] == 1
    asyncio.run(run())
