"""Durable Memory jobs, recovery, cancellation and Replay."""

import asyncio
import os

import pytest

from team_memory.handlers import register_handlers
from team_memory.repository import MemoryRepository
from team_memory.schedule import memory_tick
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.openviking import OV
from tests.support.postgres import _run

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="requires PostgreSQL")


def worker(env, ov):
    return build_services(
        role="worker", tx=env.tx, handlers=register_handlers(), extras={"memory_client_factory": lambda *_: ov}
    )


def start(client, *, target="viking://resources/shared-knowledge", key=""):
    return client.post(
        "/api/aggregation/run",
        headers={**H, **({"Idempotency-Key": key} if key else {})},
        json={"user_ids": ["alice"], "pipeline": "aggregation", "target_uri": target},
    )


@pytest.mark.postgres
def test_memory_checkpoint_resumes_existing_upstream_after_crash(pg_env, tmp_path, monkeypatch):
    ov = OV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = start(client, key="once")
        assert response.status_code == 202, response.text
        rid = response.json()["job_id"]
        assert start(client, key="once").json()["job_id"] == rid
    original = MemoryRepository.finish

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after checkpoint")

    monkeypatch.setattr(MemoryRepository, "finish", crash)
    services = worker(pg_env, ov)
    pg_env.tx.pool.run(process_once(services, owner_id="a"))
    assert len(ov.submits) == 1
    monkeypatch.setattr(MemoryRepository, "finish", original)
    _run(
        pg_env,
        "default",
        lambda c: c.execute(f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE task_id=$1", rid),
    )
    pg_env.tx.pool.run(process_once(services, owner_id="b"))
    assert len(ov.submits) == 1
    with _pod(pg_env, tmp_path, monkeypatch, pod="b") as client:
        _login(client, bootstrap=False)
        row = client.get("/api/aggregation/status/" + rid, headers=H).json()
        assert row["status"] == "succeeded"
    assert _run(pg_env, "default", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.storage_objects")) == 1
    assert _run(pg_env, "alpha", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.memory_runs")) == 0


@pytest.mark.postgres
def test_overlapping_memory_targets_cannot_execute_across_workers(pg_env, tmp_path, monkeypatch):
    ov = OV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        assert start(client).status_code == 202
        assert start(client, target="viking://resources/shared-knowledge/profile").status_code == 202
    services = worker(pg_env, ov)

    async def compete():
        ov.block = asyncio.Event()
        first = asyncio.create_task(process_once(services, owner_id="a"))
        for _ in range(200):
            if ov.submits:
                break
            await asyncio.sleep(0.01)
        assert len(ov.submits) == 1
        second = await process_once(services, owner_id="b")
        assert second is not None
        ov.block.set()
        await first

    pg_env.tx.pool.run(compete())
    assert len(ov.submits) == 1
    statuses = _run(
        pg_env,
        "default",
        lambda c: c.fetch(f"SELECT status,error_code FROM {pg_env.schema}.worker_tasks ORDER BY created_at"),
    )
    assert statuses[0]["status"] == "succeeded"
    assert statuses[1]["error_code"] == "memory_target_busy"


@pytest.mark.postgres
def test_memory_configuration_missing_and_secret_input_fail_closed(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        assert client.post("/api/aggregation/run", headers=H, json={"root_key": "secret"}).status_code == 400
        _run(
            pg_env, "default", lambda c: c.execute(f"DELETE FROM {pg_env.schema}.tenant_configs WHERE module='memory'")
        )
        assert start(client).status_code == 503


@pytest.mark.postgres
def test_memory_automatic_policy_and_canonical_pipeline(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        settings = client.get("/api/aggregation/settings", headers=H).json()
        assert settings["target_root"] == "viking://resources/shared-knowledge"
        saved = client.post(
            "/api/aggregation/settings",
            headers=H,
            json={
                "expected_revision": settings["revision"],
                "okf_skill_uri": settings["okf_skill_uri"],
                "maintenance_skill_uri": settings["maintenance_skill_uri"],
                "maintenance_trigger_mode": "automatic",
                "maintenance_interval_seconds": 300,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["maintenance_trigger_mode"] == "automatic"

    services = worker(pg_env, OV())
    services.extras.update(config=TeamEvolverConfig(), config_repo=ConfigRepository(pg_env.tx))
    assert pg_env.tx.pool.run(memory_tick(services, tenant_id="default"))["state"] == "enqueued"
    assert pg_env.tx.pool.run(memory_tick(services, tenant_id="default"))["state"] == "active"
    with _pod(pg_env, tmp_path, monkeypatch, pod="b") as client:
        _login(client, bootstrap=False)
        response = client.post(
            "/api/aggregation/run",
            headers=H,
            json={"pipeline": "maintenance", "target_uri": settings["target_root"]},
        )
        assert response.status_code == 202, response.text


@pytest.mark.postgres
def test_memory_cancel_waits_for_upstream_and_releases_only_after_terminal(pg_env, tmp_path, monkeypatch):
    ov = OV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        rid = start(client).json()["job_id"]
    services = worker(pg_env, ov)

    async def exercise():
        ov.block = asyncio.Event()
        work = asyncio.create_task(process_once(services, owner_id="a"))
        for _ in range(300):
            async with pg_env.tx.tenant_tx("default") as conn:
                task = await services.tasks.get(conn, task_id=rid)
            if task["upstream_task_id"]:
                break
            await asyncio.sleep(0.01)
        async with pg_env.tx.tenant_tx("default") as conn:
            await services.tasks.request_cancel(conn, task_id=rid)
            lease = await conn.fetchrow(f"SELECT * FROM {pg_env.schema}.resource_leases")
            assert lease["owner_id"] == rid
        ov.block.set()
        await work
        async with pg_env.tx.tenant_tx("default") as conn:
            task = await services.tasks.get(conn, task_id=rid)
            assert task["status"] == "cancelled"
            assert await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.memory_changes") == 0
            assert await conn.fetchval(f"SELECT lease_until FROM {pg_env.schema}.resource_leases") is None

    pg_env.tx.pool.run(exercise())


@pytest.mark.postgres
def test_memory_reconciles_submission_if_worker_dies_before_saving_task_id(pg_env, tmp_path, monkeypatch):
    ov = OV()
    repo = MemoryRepository(pg_env.tx)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        rid = start(client).json()["job_id"]
    services = worker(pg_env, ov)
    original = MemoryRepository.save_stage

    async def crash(self, conn, task, key, payload, **kwargs):
        if kwargs.get("upstream"):
            raise RuntimeError("crash after remote acceptance")
        return await original(self, conn, task, key, payload, **kwargs)

    monkeypatch.setattr(MemoryRepository, "save_stage", crash)
    pg_env.tx.pool.run(process_once(services, owner_id="a"))
    assert len(ov.submits) == 1
    monkeypatch.setattr(MemoryRepository, "save_stage", original)
    _run(
        pg_env,
        "default",
        lambda c: c.execute(
            f"UPDATE {pg_env.schema}.worker_tasks SET lease_until=now()-interval '1 second' WHERE task_id=$1", rid
        ),
    )
    pg_env.tx.pool.run(process_once(services, owner_id="b"))
    assert len(ov.submits) == 1
    assert _run(pg_env, "default", lambda c: repo.get(c, rid))["result"] is not None
    assert any(call[1].startswith("/api/v1/compile/submissions/") for call in ov.calls)


@pytest.mark.postgres
def test_memory_replay_api_and_handler_use_fixed_pg_inputs(pg_env, tmp_path, monkeypatch):
    from teamEvolver.app import build_role_services
    from teamEvolver.config import TeamEvolverConfig
    from tests.support.replay import Host, judge

    host = Host()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post(
            "/api/openviking/memory/true-replay",
            headers=H,
            json={
                "memory_path": "viking://resources/facts",
                "before_content": "Old",
                "after_content": "New",
                "query": "Execute task",
                "checklist": ["Produce answer"],
                "runtime": {"type": "fake"},
            },
        )
        assert response.status_code == 202, response.text
        rid = response.json()["run_id"]
    services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
    services.extras.update(replay_host_factory=lambda *_: host, replay_judge=judge)
    pg_env.tx.pool.run(process_once(services, owner_id="worker"))
    row = _run(pg_env, "default", lambda c: services.tasks.get(c, task_id=rid))
    assert row["status"] == "succeeded" and row["kind"] == "memory.replay"
    assert [context.context_snapshot["items"][0]["expanded"]["full"] for context in host.contexts] == ["Old", "New"]


@pytest.mark.postgres
@pytest.mark.parametrize("strategy", ["incremental", "partition"])
def test_memory_large_account_publish_strategies_preserved(pg_env, tmp_path, monkeypatch, strategy):
    import json

    ov = OV()
    aggregation = {
        "incremental_publish": strategy == "incremental",
        "publish_batch_users": 2,
        "partition_threshold": 3 if strategy == "partition" else 512,
        "partition_count": 16,
    }
    _run(
        pg_env,
        "default",
        lambda c: c.execute(
            f"UPDATE {pg_env.schema}.tenant_configs SET config=$1::jsonb WHERE module='memory'",
            json.dumps({"aggregation": aggregation}),
        ),
    )
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post(
            "/api/aggregation/run", headers=H, json={"user_ids": ["alice", "bob", "charlie"], "pipeline": "aggregation"}
        )
        assert response.status_code == 202
        rid = response.json()["job_id"]
    services = worker(pg_env, ov)
    pg_env.tx.pool.run(process_once(services, owner_id="a"))
    assert _run(pg_env, "default", lambda c: services.tasks.get(c, task_id=rid))["status"] == "succeeded"
    bodies = [s["json_body"] for s in ov.submits]
    if strategy == "incremental":
        assert [len(body["from"]) for body in bodies] == [2, 1]
        assert all(body["to"] == "viking://resources/shared-knowledge" for body in bodies)
    else:
        assert all("/partition-" in body["to"] for body in bodies)
    change = _run(pg_env, "default", lambda c: MemoryRepository(pg_env.tx).changes(c))[0]
    assert change["before_ref"].endswith("/before") and change["after_ref"].endswith("/after")
