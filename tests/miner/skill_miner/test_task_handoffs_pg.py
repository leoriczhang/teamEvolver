"""Crash windows, fair monitor ticks and API/Worker transaction boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from team_miner.skill_miner import job_operations
from team_miner.skill_miner.cache import persist_cache
from team_miner.skill_miner.config import MiningSettings, freeze, save
from team_miner.skill_miner.handlers import register_handlers
from team_miner.skill_miner.repository import MiningRevisionConflict, make_repository
from team_miner.skill_miner.settings import compiler_prompt
from team_miner.skill_miner.storage import PgMiningFileStore
from team_miner.skill_miner.task_io import State, accept, advance_compile, digest, enqueue_compile
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.openviking import OpenVikingResult
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres
ROOT = "viking://resources/course-source"


class Remote:
    def __init__(self):
        self.calls = []

    async def aclose(self):
        pass

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/api/v1/fs/ls":
            return OpenVikingResult(True, 200, [{"uri": kwargs["params"]["uri"] + "/lesson.md",
                                                "name": "lesson.md", "size": 100, "isDir": False}])
        if "/compile/submissions/" in path:
            return OpenVikingResult(True, 200, {"task_id": "upstream-" + path.rsplit("/", 1)[-1]})
        if path.startswith("/api/v1/tasks/"):
            return OpenVikingResult(True, 200, {"status": "running", "stage": "running"})
        raise AssertionError((method, path))


def worker(env, remote=None):
    return build_services(role="worker", tx=env.tx, handlers=register_handlers(), extras={
        "config_repo": ConfigRepository(env.tx), "skill_mining_client_factory": lambda *_: remote or Remote(),
    })


def create_job(services, name="job-1", tenant="default", **changes):
    repo = make_repository(services, tenant)
    value = repo.create_job(job_id=name, name="课程练习", account="acct-default", source_uri=ROOT,
                            source_snapshot_uri="viking://resources/frozen", output_uri="viking://resources/output",
                            document_count=1, max_rounds=3, compiler_body=compiler_prompt({}))
    persist_cache(repo, name)
    if changes:
        value = repo.update_job(name, **changes)
    return repo, value


def tick(env, services, *, kinds=None):
    return env.tx.pool.run(process_once(services, owner_id="handoff-worker", kinds=kinds), timeout=10)


def test_create_recovers_first_insert_before_artifact_checkpoint_with_original_settings(pg_env, tmp_path, monkeypatch):
    services = worker(pg_env)
    source = make_repository(services, "default").save_source("acct-default", ROOT, tags=["skill"])
    with _pod(pg_env, tmp_path, monkeypatch, pod="accept") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/mining/jobs", headers={**H, "Idempotency-Key": "crash-create"},
                               json={"source_id": source["source_id"], "input_dir": ROOT})
        assert response.status_code == 202
        identifier = response.json()["job_id"]
    original = job_operations.persist_cache

    def crash(*_args):
        raise RuntimeError("crash after first metadata insert")

    monkeypatch.setattr(job_operations, "persist_cache", crash)
    tick(pg_env, services, kinds=["miner.skill_operation"])
    repository = make_repository(services, "default")
    jobs = repository.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["config_snapshot"]["settings_revision"] == 0
    assert jobs[0]["max_rounds"] == 3
    settings_services = SimpleNamespace(tx=pg_env.tx, extras={"config_repo": ConfigRepository(pg_env.tx)})
    _run(pg_env, "default", lambda conn: save(settings_services, conn,
         {"mining": {"pipeline": {"max_rounds": 5}}}, expected_revision=0))
    repository.remove_source(source["source_id"])
    monkeypatch.setattr(job_operations, "persist_cache", original)
    _run(pg_env, "default", lambda conn: conn.execute(
        f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE task_id=$1", identifier))
    tick(pg_env, services, kinds=["miner.skill_operation"])
    row = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id=identifier))
    assert row["status"] == "succeeded", row["error_code"]
    assert len(repository.list_jobs()) == 1
    value = repository.get_job(jobs[0]["job_id"])
    assert value["config_snapshot"]["settings_revision"] == 0 and value["max_rounds"] == 3
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks WHERE kind='miner.skill_compile'")) == 1


def test_stop_intent_and_receipt_roll_back_together_and_fence_stale_writer(pg_env, tmp_path, monkeypatch):
    services = worker(pg_env)
    old, value = create_job(services)
    old.get_job(value["job_id"])
    with _pod(pg_env, tmp_path, monkeypatch, pod="stop") as client:
        _login(client, bootstrap=True)
        tasks = client.app.state.services.tasks
        enqueue = tasks.enqueue

        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(tasks, "enqueue", unavailable)
        failed = client.post("/api/mining/jobs/job-1/stop", headers=H, json={})
        assert failed.status_code == 500
        assert make_repository(services, "default").get_job("job-1")["status"] == "preparing"
        monkeypatch.setattr(tasks, "enqueue", enqueue)
        first = client.post("/api/mining/jobs/job-1/stop", headers=H, json={})
        assert first.status_code == 202, first.text
        repeat = client.post("/api/mining/jobs/job-1/stop", headers=H, json={})
        assert repeat.json()["job_id"] == first.json()["job_id"]
    with pytest.raises(MiningRevisionConflict):
        old.backend.save_job({**value, "status": "running"})
    assert make_repository(services, "default").get_job("job-1")["status"] == "stopped"


def test_two_compile_jobs_rotate_and_do_not_block_source_work(pg_env):
    services = worker(pg_env)
    settings = freeze(MiningSettings(1, 0, {}, {"account_id": "acct-default", "user_id": "team"}))
    for name in ("job-a", "job-b"):
        _, value = create_job(services, name, compile_request={"from": [], "to": "output", "skill": "immutable"},
                               config_snapshot=settings)
        pg_env.tx.pool.run(enqueue_compile(services, "default", value, settings))
    payload = {"operation": "source.create", "body": {"uri": ROOT, "tags": ["skill"]}, "settings": settings,
               "job_id": "", "submitted_by": "admin"}

    async def source(conn):
        await State(services, "default").put(conn, "payload", "source-operation", payload)
        await services.tasks.enqueue(conn, tenant_id="default", task_id="source-operation",
            kind="miner.skill_operation", payload_ref="source-operation", payload_hash=digest(payload))

    _run(pg_env, "default", source)
    for _ in range(3):
        tick(pg_env, services)
    states = _run(pg_env, "default", lambda conn: conn.fetch(
        f"SELECT kind,status FROM {pg_env.schema}.worker_tasks ORDER BY created_at"))
    assert sum(row["kind"] == "miner.skill_compile" and row["status"] == "succeeded" for row in states) == 2
    assert sum(row["kind"] == "miner.skill_compile" and row["status"] == "queued" for row in states) == 2
    source_task = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id="source-operation"))
    assert source_task["status"] == "succeeded", source_task["error_code"]


def test_failed_tick_handoff_keeps_current_monitor_recoverable(pg_env, monkeypatch):
    services = worker(pg_env)
    settings = freeze(MiningSettings(1, 0, {}, {"account_id": "acct-default", "user_id": "team"}))
    _, value = create_job(services, compile_request={"from": [], "to": "output", "skill": "immutable"},
                           config_snapshot=settings)
    current = pg_env.tx.pool.run(enqueue_compile(services, "default", value, settings))

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("next tick unavailable")

    monkeypatch.setattr(services.tasks, "enqueue", unavailable)
    tick(pg_env, services, kinds=["miner.skill_compile"])
    row = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id=current))
    assert row["status"] == "reconciling"
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks WHERE kind='miner.skill_compile'")) == 1


def test_continuation_during_finishing_tick_cannot_lose_the_next_monitor(pg_env):
    services = worker(pg_env)
    settings = freeze(MiningSettings(1, 0, {}, {"account_id": "acct-default", "user_id": "team"}))
    repo, value = create_job(services, status="waiting_human", config_snapshot=settings)
    original = pg_env.tx.pool.run(enqueue_compile(services, "default", value, settings))
    task = _run(pg_env, "default", lambda conn: services.tasks.claim(
        conn, owner_id="finishing-tick", lease_seconds=60, kinds=["miner.skill_compile"]))
    newer = repo.update_job("job-1", status="preparing", current_round=2, submission_idempotency_key="round-2")
    # The continuation races before the older handler has committed completion.
    assert pg_env.tx.pool.run(enqueue_compile(services, "default", newer, settings)) == original
    pg_env.tx.pool.run(advance_compile(services, task, settings))
    rows = _run(pg_env, "default", lambda conn: conn.fetch(
        f"SELECT task_id,status FROM {pg_env.schema}.worker_tasks WHERE kind='miner.skill_compile'"))
    assert len(rows) == 2
    assert sum(row["status"] == "queued" for row in rows) == 1
    assert sum(row["task_id"] == original and row["status"] == "succeeded" for row in rows) == 1


def test_delete_tombstone_replays_original_receipt_after_lost_result_commit(pg_env, tmp_path, monkeypatch):
    services = worker(pg_env)
    create_job(services, status="succeeded")
    with _pod(pg_env, tmp_path, monkeypatch, pod="delete") as client:
        _login(client, bootstrap=True)
        response = client.delete("/api/mining/jobs/job-1", headers=H)
        assert response.status_code == 202
        identifier = response.json()["job_id"]
    put = State.put

    async def lost_result(self, conn, kind, name, value):
        if kind == "result":
            raise RuntimeError("result write connection lost")
        return await put(self, conn, kind, name, value)

    monkeypatch.setattr(State, "put", lost_result)
    tick(pg_env, services, kinds=["miner.skill_operation"])
    row = make_repository(services, "default").backend.load_job("job-1")
    assert row["_deleted"] and row["delete_receipt"]["operation_id"] == identifier
    monkeypatch.setattr(State, "put", put)
    _run(pg_env, "default", lambda conn: conn.execute(
        f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE task_id=$1", identifier))
    tick(pg_env, services, kinds=["miner.skill_operation"])
    task = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id=identifier))
    assert task["status"] == "succeeded", task["error_code"]
    result = _run(pg_env, "default", lambda conn: State(services, "default").get(conn, "result", identifier))
    assert result == row["delete_receipt"]["result"]


def test_concurrent_acceptance_is_bounded_and_stop_keeps_one_reserved_slot(pg_env):
    services = worker(pg_env)
    settings = MiningSettings(1, 0, {}, {"account_id": "acct-default", "user_id": "team"})

    async def seed(conn):
        for number in range(99):
            await services.tasks.enqueue(conn, tenant_id="default", task_id=f"queued-{number}",
                                         kind="miner.skill_operation")
    _run(pg_env, "default", seed)

    def request(key):
        return SimpleNamespace(headers={"Idempotency-Key": key}, state=SimpleNamespace(console_user={"id": "admin"}))

    async def race():
        return await asyncio.gather(
            accept(request("one"), services, "source.list", {}, settings),
            accept(request("two"), services, "source.list", {}, settings), return_exceptions=True,
        )
    results = pg_env.tx.pool.run(race())
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(getattr(result, "status_code", 0) == 429 for result in results) == 1
    create_job(services)
    first = pg_env.tx.pool.run(accept(request("stop-one"), services, "job.stop", {}, settings, job_id="job-1"))
    second = pg_env.tx.pool.run(accept(request("stop-two"), services, "job.stop", {}, settings, job_id="job-1"))
    assert first["job_id"] == second["job_id"]
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks WHERE kind='miner.skill_operation'")) == 101


def test_candidate_acceptance_freezes_revisions_and_stale_attempt_fails_without_retry(pg_env, tmp_path, monkeypatch):
    remote = Remote()
    services = worker(pg_env, remote)
    repo, _ = create_job(services)
    with _pod(pg_env, tmp_path, monkeypatch, pod="candidate") as client:
        _login(client, bootstrap=True)
        forged = client.post("/api/mined-jobs/job-1/skills/lesson/submit", headers=H,
                             json={"_accepted_job_revision": 999})
        assert forged.status_code == 400
        response = client.post("/api/mined-jobs/job-1/skills/lesson/submit", headers=H, json={})
        assert response.status_code == 202, response.text
        identifier = response.json()["job_id"]
    payload = _run(pg_env, "default", lambda conn: State(services, "default").get(conn, "payload", identifier))
    assert isinstance(payload["accepted_job_revision"], int)
    assert isinstance(payload["accepted_files_revision"], int)
    repo.update_job("job-1", name="edited after acceptance")
    tick(pg_env, services, kinds=["miner.skill_operation"])
    task = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id=identifier))
    assert task["status"] == "failed" and task["error_code"].startswith("409:"), task
    assert remote.calls == []


def test_detail_projection_and_artifact_get_do_not_restore_a_workspace_on_api(pg_env, tmp_path, monkeypatch):
    from team_miner.skill_miner import cache
    services = worker(pg_env)
    repo, _ = create_job(services)
    PgMiningFileStore(repo.backend).save("job-1", {
        "workspace/compiled_skill/lesson/SKILL.md": b"---\nname: lesson\ndescription: lesson\n---\n# Lesson\n",
    })
    restore = cache.restore_cache

    def forbidden(*_args):
        raise AssertionError("API may not rebuild an entire workspace")

    with _pod(pg_env, tmp_path, monkeypatch, pod="detail") as client:
        _login(client, bootstrap=True)
        monkeypatch.setattr(cache, "restore_cache", forbidden)
        response = client.get("/api/mining/artifacts/content", headers=H,
                              params={"path": "mining_jobs/job-1/workspace/compiled_skill/lesson/SKILL.md"})
        assert response.status_code == 200, response.text
        assert "# Lesson" in response.json()["content"]
        detail = client.get("/api/mining/jobs/job-1", headers=H)
        assert detail.status_code == 202, detail.text
        monkeypatch.setattr(cache, "restore_cache", restore)
        tick(pg_env, services, kinds=["miner.skill_operation"])
        detail = client.get("/api/mining/jobs/job-1", headers=H)
        assert detail.status_code == 200, detail.text
        assert "config_snapshot" not in detail.json()["job"]
        assert detail.json()["job"]["artifacts"][0]["name"] == "SKILL.md"


@pytest.mark.parametrize("invalid", [{"api_key": "must-not-freeze"}, {"source_uri": "viking://resources/../escape"},
                                     {"skill_uri": "viking://resources/not-a-skill"}])
def test_invalid_miner_config_is_503_before_creating_any_task(pg_env, tmp_path, monkeypatch, invalid):
    config = ConfigRepository(pg_env.tx)
    _run(pg_env, "default", lambda conn: config.write_tenant_module(
        conn, module="miner", expected_revision=1, config=invalid))
    with _pod(pg_env, tmp_path, monkeypatch, pod="invalid-config") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/mining/jobs", headers=H, json={})
        assert response.status_code == 503, response.text
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks WHERE kind LIKE 'miner.skill_%'")) == 0
