"""Worker observation, revisions and discovery persist atomically in PostgreSQL."""

import asyncio

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.environment_observation import refresh_environment
from team_ma.repository import MARepository
from team_ma.schedule import tick
from team_ma.schemas import digest, validate_config
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient
from teamEvolver.runtime import process_once
from teamEvolver.security import SecretBox, SecretRepository

pytestmark = pytest.mark.postgres


async def setup(env, respond, *, environment_id="old", replay_enabled=False):
    services = build_role_services(role="worker", tx=env.tx, config=TeamEvolverConfig())
    secrets = SecretRepository(env.tx, SecretBox("test-key"))
    services.extras["secret_repo"] = secrets
    services.extras["ma_client_factory"] = lambda url, key: MAClient(
        url, key, transport=httpx.MockTransport(respond),
    )
    repo = MARepository(env.tx)
    async with env.tx.tenant_tx("alpha") as conn:
        secret = await secrets.store(conn, tenant_id="alpha", plaintext="fixture")
        config = validate_config({"display_name": "Automatic", "entry_agent_id": "root",
                                  "environment_id": environment_id, "ma_secret_ref": secret.ref,
                                  "tenant_token_ref": secret.ref, "replay_enabled": replay_enabled})
        await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
        await repo.save_release(
            conn, tenant_id="alpha", connection_id="one", release_id="old-release", manifest_hash="old",
            record={"environment_id": environment_id, "connection_revision": 1,
                    "environment_hash": digest({"type": "cloud"})}, status="applied",
        )
        await repo.status(conn, "one", "ready")
    return services, repo


def test_worker_updates_environment_then_discovers_without_rewriting_old_inputs(pg_env):
    source = {"id": "source", "created_at": "2026-11-23T00:00:00Z", "status": "running",
              "agent": {"id": "root", "version": 1},
              "environment": {"id": "new", "config": {"type": "cloud"}}}
    requests = []

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        requests.append((request.method, path))
        if path == "sessions":
            return httpx.Response(200, json={"data": [source]})
        if path == "sessions/source":
            return httpx.Response(200, json=source)
        if path.endswith(("/threads", "/events")):
            return httpx.Response(200, json={"data": []})
        if path == "agents/root":
            return httpx.Response(200, json={"id": "root", "version": 1, "skills": []})
        if path == "environments/new":
            return httpx.Response(200, json=source["environment"])
        raise AssertionError((request.method, path))

    async def run():
        services, repo = await setup(pg_env, respond)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.collect",
                          operation_id="collect")
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.collect"])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            live = await repo.connection(conn, "one")
            assert live["revision"] == 2 and live["config"]["environment_id"] == "new"
            assert (await repo.connection(conn, "one", 1))["config"]["environment_id"] == "old"
            operation = await repo.operation(conn, "collect")
            assert operation["input"]["revision"] == 1
            assert operation["result"]["counts"] == {"active": 1}
            assert live["detail"]["environment_observation"]["session_id"] == "source"
            assert (await repo.applied(conn, "one"))["record"]["environment_id"] == "old"
        # The scheduler must not dispatch competing sync/collection while discovery is pending.
        await tick(services, tenant_id="alpha")
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert await conn.fetchval(f"SELECT count(*) FROM {repo.schema}.ma_operations") == 2
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.discover"])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            live = await repo.connection(conn, "one")
            assert live["status"] == "ready" and live["revision"] == 2
            release = (await repo.applied(conn, "one"))["record"]
            assert release["environment_id"] == "new" and release["connection_revision"] == 2
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.collect",
                          operation_id="repeat")
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.collect"])
        # Even if the newer Session disappears, an older active one cannot roll back the environment.
        source.update(id="older", created_at="2026-11-20T00:00:00Z", environment={"id": "old"})
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.discover",
                          operation_id="old-session")
        # Inspect just the observation; discovery still reads the fixed current environment.
        async with pg_env.tx.tenant_tx("alpha") as conn:
            task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.discover"])
            connection = await repo.connection(conn, "one")
            operation = await repo.operation(conn, "old-session")
        def older_response(request):
            return httpx.Response(200, json={"data": [source]} if request.url.path.endswith("/sessions") else source)
        async with MAClient("https://ma.example/api/v3", "fixture",
                            transport=httpx.MockTransport(older_response)) as client:
            assert await refresh_environment(task, services, client, connection, operation) is None
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await repo.connection(conn, "one"))["revision"] == 2
        assert not any(method != "GET" or path == "environments/old" for method, path in requests)
    asyncio.run(run())


@pytest.mark.parametrize("environment_id", ["old", ""])
def test_discovery_requeues_at_observed_revision_and_invalidates_replay(pg_env, environment_id):
    def respond(request):
        source = {"id": "source", "created_at": "2026-11-23T00:00:00Z",
                  "agent": {"id": "root"}, "environment": {"id": "new", "config": {"type": "cloud"}}}
        return httpx.Response(200, json={"data": [source]} if request.url.path.endswith("/sessions") else source)

    async def run():
        services, repo = await setup(pg_env, respond, environment_id=environment_id, replay_enabled=True)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.discover",
                          operation_id="discover")
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.discover"])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            row = await repo.connection(conn, "one")
            assert row["revision"] == 2 and row["status"] == "discovering"
            assert row["detail"]["replay_resources"]["status"] == "preparing"
            assert row["detail"]["replay_capability"]["supported"] is False
            result = (await repo.operation(conn, "discover"))["result"]
            assert result["status"] == "environment_changed"
            follow = await repo.operation(conn, result["discovery_job_id"])
            assert follow["input"]["revision"] == 2
            assert (await services.tasks.get(conn, task_id="discover"))["status"] == "succeeded"
    asyncio.run(run())


def test_same_environment_config_change_triggers_one_revision(pg_env):
    environment = {"id": "old", "config": {"type": "cloud", "setup_script": "new runtime"}}
    def respond(request):
        source = {"id": "source", "created_at": "2026-11-23T00:00:00Z",
                  "agent": {"id": "root"}, "environment": environment}
        return httpx.Response(200, json={"data": [source]} if request.url.path.endswith("/sessions") else source)

    async def run():
        services, repo = await setup(pg_env, respond)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.discover",
                          operation_id="discover")
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.discover"])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.discover"])
            live = await repo.connection(conn, "one")
            operation = await repo.operation(conn, task.task_id)
        async with services.extras["ma_client_factory"]("https://ma.example/api/v3", "fixture") as client:
            assert await refresh_environment(task, services, client, live, operation) is None
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await repo.connection(conn, "one"))["revision"] == 2
    asyncio.run(run())


def test_waiting_for_first_session_is_retried_by_scheduler(pg_env):
    async def run():
        services, repo = await setup(pg_env, lambda _: httpx.Response(200, json={"data": []}), environment_id="")
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.discover",
                          operation_id="discover")
        await process_once(services, owner_id="worker", tenants=["alpha"], kinds=["ma.discover"], max_attempts=1)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await repo.connection(conn, "one"))["detail"]["error"] == "ma_EnvironmentNotObserved"
            await conn.execute(f"UPDATE {repo.schema}.ma_operations SET created_at=now()-interval '2 days'")
        await tick(services, tenant_id="alpha")
        await tick(services, tenant_id="alpha")
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert await conn.fetchval(f"SELECT count(*) FROM {repo.schema}.ma_operations") == 2
    asyncio.run(run())


def test_queued_old_discovery_and_sync_cannot_replace_observed_environment(pg_env):
    source = {"id": "source", "created_at": "2026-11-23T00:00:00Z", "status": "running",
              "agent": {"id": "root"}, "environment": {"id": "new"}}
    requests = []
    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        requests.append(path)
        if path == "sessions":
            return httpx.Response(200, json={"data": [source]})
        if path == "sessions/source":
            return httpx.Response(200, json=source)
        if path.endswith(("/threads", "/events")):
            return httpx.Response(200, json={"data": []})
        raise AssertionError(path)

    async def run():
        services, repo = await setup(pg_env, respond)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            for kind in ["ma.discover", "ma.reconcile", "ma.collect"]:
                await enqueue(conn, services, tenant_id="alpha", connection_id="one",
                              kind=kind, operation_id=kind)
        for kind in ["ma.collect", "ma.discover", "ma.reconcile"]:
            await process_once(services, owner_id="worker", tenants=["alpha"], kinds=[kind])
        async with pg_env.tx.tenant_tx("alpha") as conn:
            for ident in ["ma.discover", "ma.reconcile"]:
                operation = await repo.operation(conn, ident)
                assert operation["input"]["revision"] == 1
                assert operation["result"] == {"status": "environment_superseded", "connection_revision": 2}
            assert (await repo.connection(conn, "one"))["config"]["environment_id"] == "new"
        assert not any(path.startswith("environments/") for path in requests)
    asyncio.run(run())


def test_concurrent_config_edit_is_not_overwritten_by_observation(pg_env, monkeypatch):
    async def run():
        services, repo = await setup(pg_env, lambda _: httpx.Response(200, json={}))
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="one", kind="ma.collect",
                          operation_id="collect")
            task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.collect"])
            connection = await repo.connection(conn, "one")
            operation = await repo.operation(conn, "collect")
        async def changed_during_read(*args):
            async with pg_env.tx.tenant_tx("alpha") as conn:
                await repo.save(conn, tenant_id="alpha", connection_id="one", expected_revision=1,
                                config={**connection["config"], "display_name": "User edit"})
            return {"session_id": "source", "created_at": "2026-11-23T00:00:00Z",
                    "environment_id": "observed", "environment_hash": ""}
        monkeypatch.setattr("team_ma.environment_observation.latest_environment", changed_during_read)
        assert await refresh_environment(task, services, None, connection, operation) is None
        async with pg_env.tx.tenant_tx("alpha") as conn:
            live = await repo.connection(conn, "one")
            assert live["revision"] == 2 and live["config"]["display_name"] == "User edit"
            assert live["config"]["environment_id"] == "old"
            assert await conn.fetchval(f"SELECT count(*) FROM {repo.schema}.ma_operations") == 1
    asyncio.run(run())
