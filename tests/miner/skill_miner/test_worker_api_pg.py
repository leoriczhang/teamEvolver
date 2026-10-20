"""Real API -> PG -> Worker -> new API replica contracts for Skill job creation."""

from __future__ import annotations

import hashlib

import pytest

from team_miner.skill_miner.handlers import register_handlers
from team_miner.skill_miner.repository import make_repository
from teamEvolver.openviking import OpenVikingResult
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

ROOT = "viking://resources/course-source"


class SourceOV:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path == "/api/v1/fs/ls":
            assert kwargs["params"]["uri"] == ROOT
            return OpenVikingResult(True, 200, [{"uri": ROOT + "/lesson.md", "name": "lesson.md",
                                                "isDir": False, "size": 100}])
        raise AssertionError("Job acceptance must not submit remote Compile: " + path)

    async def aclose(self):
        pass


def worker(env, ov):
    return build_services(role="worker", tx=env.tx, handlers=register_handlers(),
                          extras={"skill_mining_client_factory": lambda *_: ov})


def register_source(env):
    services = build_services(role="api", tx=env.tx)
    repository = make_repository(services, "default")
    return repository.save_source("acct-default", ROOT, name="课程", tags=["skill"])


@pytest.mark.postgres
def test_create_job_receipt_frozen_assets_and_restart_then_stop_before_compile(pg_env, tmp_path, monkeypatch):
    source = register_source(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        body = {"source_id": source["source_id"], "input_dir": ROOT, "name": "课程练习", "max_rounds": 2}
        first = client.post("/api/mining/jobs", headers={**H, "Idempotency-Key": "create-once"}, json=body)
        assert first.status_code == 202, first.text
        receipt = first.json()
        again = client.post("/api/mining/jobs", headers={**H, "Idempotency-Key": "create-once"}, json=body)
        assert again.json()["job_id"] == receipt["job_id"]
        conflict = client.post("/api/mining/jobs", headers={**H, "Idempotency-Key": "create-once"},
                               json={**body, "name": "different"})
        assert conflict.status_code == 409
        assert client.get(receipt["status_url"], headers=H).json()["status"] == "queued"
    ov = SourceOV()
    services = worker(pg_env, ov)
    pg_env.tx.pool.run(process_once(services, owner_id="worker-a", kinds=["miner.skill_operation"]))
    with _pod(pg_env, tmp_path, monkeypatch, pod="b") as client:
        _login(client, bootstrap=False)
        response = client.get(receipt["status_url"], headers=H)
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["status"] == "succeeded", status
        job = status["result"]["jobs"][0]
        assert job["status"] == "preparing"
        identifier = job["job_id"]
        detail = client.get("/api/mining/jobs/" + identifier, headers=H)
        assert detail.status_code == 200, detail.text
        assert detail.json()["job"]["job_id"] == identifier
        stopped = client.post("/api/mining/jobs/" + identifier + "/stop", headers=H, json={})
        assert stopped.status_code == 202, stopped.text
    compile_rows = _run(pg_env, "default", lambda conn: conn.fetch(
        f"SELECT task_id FROM {pg_env.schema}.worker_tasks WHERE kind='miner.skill_compile'"))
    assert len(compile_rows) == 1
    files = _run(pg_env, "default", lambda conn: conn.fetch(
        f"SELECT path,content FROM {pg_env.schema}.skill_mining_files WHERE job_id=$1", identifier))
    assert any(row["path"] == "compiler-skill.md" for row in files)
    assert any("compiler-assets" in row["path"] for row in files)
    # Draining both accepted tasks never creates remote work after the stop.
    pg_env.tx.pool.run(process_once(services, owner_id="worker-b", kinds=["miner.skill_compile"]))
    pg_env.tx.pool.run(process_once(services, owner_id="worker-b", kinds=["miner.skill_operation"]))
    assert all(path != "/api/v1/compile" for _, path, _ in ov.calls)
    assert make_repository(services, "default").get_job(identifier)["status"] == "stopped"
    assert make_repository(services, "alpha").backend.load_job(identifier) is None


@pytest.mark.postgres
def test_skill_settings_are_cas_guarded_and_do_not_change_knowledge_settings(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        before = client.get("/api/knowledge-mining/config", headers=H).json()
        current = client.get("/api/mining/settings", headers=H).json()
        assert current["revision"] == 0
        response = client.post("/api/mining/settings", headers=H,
                               json={"expected_revision": 0, "pipeline": {"max_rounds": 4}})
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        stale = client.post("/api/mining/settings", headers=H,
                            json={"expected_revision": 0, "pipeline": {"max_rounds": 1}})
        assert stale.status_code == 409
        reset = client.post("/api/mining/prompts/ov_compile/reset", headers=H, json={"expected_revision": 1})
        assert reset.status_code == 200, reset.text
        assert reset.json()["pipeline"]["max_rounds"] == 4
        assert reset.json()["revision"] == 2
        assert client.get("/api/knowledge-mining/config", headers=H).json() == before


@pytest.mark.postgres
def test_operation_payload_tampering_is_rejected_before_external_io(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        result = client.get("/api/mining/ov-sources", headers=H)
        assert result.status_code == 202, result.text
        identifier = result.json()["job_id"]
    raw = b'{"operation":"source.list","tampered":true}'
    _run(pg_env, "default", lambda conn: conn.execute(
        f"UPDATE {pg_env.schema}.storage_objects SET content=$1,content_hash=$2 WHERE key=$3",
        raw, hashlib.sha256(raw).hexdigest(), "skill_miner/payload/" + identifier))
    ov = SourceOV()
    services = worker(pg_env, ov)
    pg_env.tx.pool.run(process_once(services, owner_id="worker-a", kinds=["miner.skill_operation"]))
    assert ov.calls == []
    row = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id=identifier))
    assert row["status"] == "retry_wait"
    assert "ValueError" in row["error_code"]
