"""Run the visible Skill job lifecycle through API receipts, PG and real Worker handlers."""

from __future__ import annotations

import pytest

from team_miner.skill_miner.handlers import register_handlers
from team_miner.skill_miner.repository import make_repository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.miner.skill_miner.support.platform import SOURCE, PlatformRemote
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def test_job_create_compile_preview_edit_candidate_gate_and_delete_survive_api_restart(pg_env, tmp_path, monkeypatch):
    remote = PlatformRemote()
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
        "skill_mining_cache_root": tmp_path / "worker-cache",
        "skill_mining_client_factory": lambda *_: remote,
    })
    repository = make_repository(services, "default")
    source = repository.save_source("acct-default", SOURCE, name="演练素材", tags=["skill"])

    def tick(kind):
        # Advance the deterministic scheduler instead of sleeping for monitor delays.
        _run(pg_env, "default", lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE kind=$1", kind))
        return pg_env.tx.pool.run(process_once(services, owner_id="lifecycle-worker", kinds=[kind]))

    def operation(client, method, path, body=None):
        response = client.request(method, path, headers=H, json=body or {})
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert client.get(receipt["status_url"], headers=H).json()["status"] == "queued"
        tick("miner.skill_operation")
        completed = client.get(receipt["status_url"], headers=H)
        assert completed.status_code == 200, completed.text
        return completed.json()

    with _pod(pg_env, tmp_path, monkeypatch, pod="lifecycle-a") as client:
        _login(client, bootstrap=True)
        created = operation(client, "POST", "/api/mining/jobs", {
            "source_id": source["source_id"], "input_dir": SOURCE, "name": "全链路演练",
            "max_rounds": 1, "human_checkpoints": False, "mining_mode": "auto",
        })
        assert created["status"] == "succeeded", created
        identifier = created["result"]["jobs"][0]["job_id"]
        assert not remote.remote.compile_calls, "Acceptance must not compile on the API process"
        tick("miner.skill_compile")
        queued = repository.get_job(identifier)
        assert queued["status"] == "queued"
        assert len(remote.remote.compile_calls) == 1
        remote.complete(queued, "# 演练 Skill\n\n原始正文。")
        tick("miner.skill_compile")
        completed = client.get("/api/mining/jobs/" + identifier, headers=H)
        assert completed.status_code == 200, completed.text
        job = completed.json()["job"]
        assert job["status"] == "succeeded"
        artifact = next(item for item in job["artifacts"] if item["kind"] == "skill")
        path = artifact["path"]
        preview = client.get("/api/mining/artifacts/content", headers=H, params={"path": path})
        assert preview.status_code == 200, preview.text
        assert preview.json()["content"] == "# 演练 Skill\n\n原始正文。"
        edited = operation(client, "POST", "/api/mining/artifacts/content", {
            "path": path, "content": "# 演练 Skill\n\n人工保存的正文。",
        })
        assert edited["status"] == "succeeded", edited
        assert edited["result"]["content"] == "# 演练 Skill\n\n人工保存的正文。"

    # A new API replica reads persisted business state and edited bytes without
    # depending on a browser cache or on the previous API process.
    with _pod(pg_env, tmp_path, monkeypatch, pod="lifecycle-b") as client:
        _login(client, bootstrap=False)
        listed = client.get("/api/mining/jobs", headers=H)
        assert listed.status_code == 200, listed.text
        assert any(row["job_id"] == identifier and row["status"] == "succeeded" for row in listed.json()["jobs"])
        preview = client.get("/api/mining/artifacts/content", headers=H, params={"path": path})
        assert preview.json()["content"] == "# 演练 Skill\n\n人工保存的正文。"
        # Compile completion is sufficient for browsing/editing. The independent
        # Candidate gate must reject this deliberately unvalidated fake package.
        rejected = operation(client, "POST", f"/api/mined-jobs/{identifier}/skills/lesson/submit", {
            "artifact_path": path,
        })
        assert rejected["status"] == "failed", rejected
        assert "未执行内容验收" in rejected["error_code"]
        deleted = operation(client, "DELETE", "/api/mining/jobs/" + identifier)
        assert deleted["status"] == "succeeded", deleted
        assert deleted["result"]["deleted_files"] > 0
        assert client.get("/api/mining/jobs/" + identifier, headers=H).status_code == 404
        assert client.get("/api/mining/artifacts/content", headers=H, params={"path": path}).status_code == 404
        assert all(row["job_id"] != identifier for row in client.get("/api/mining/jobs", headers=H).json()["jobs"])
        assert len(remote.remote.compile_calls) == 1
