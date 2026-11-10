"""New mode acceptance freezes its compiler and preserves the existing 202 Worker boundary."""

import pytest

from team_miner.skill_miner.dialogue_dimensions import PROTOCOL
from team_miner.skill_miner.handlers import register_handlers
from team_miner.skill_miner.repository import make_repository
from teamEvolver.openviking import OpenVikingResult
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod


@pytest.mark.postgres
def test_new_mode_is_accepted_without_compile_and_frozen_separately(pg_env, tmp_path, monkeypatch):
    services = build_services(role="api", tx=pg_env.tx)
    root = "viking://resources/dialogue-dataset/model_cases"
    source = make_repository(services, "default").save_source("acct-default", root, name="优秀对话", tags=["skill"])
    with _pod(pg_env, tmp_path, monkeypatch, pod="api") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/mining/jobs", headers=H, json={
            "name": "优秀对话", "task_mode": "dialogue_dimensions", "input_dir": root,
            "source_id": source["source_id"],
            "dataset_manifest_ref": "viking://resources/dialogue-dataset/dataset_manifest.json",
            "dataset_manifest_sha256": "a" * 64})
        assert response.status_code == 202, response.text
        receipt = response.json()
        invalid = client.post("/api/mining/jobs", headers=H, json={
            "task_mode": "dialogue_dimensions", "dialogue_options": {"max_inflight_compiles": 3}})
        assert invalid.status_code == 400

    class Source:
        async def request(self, method, path, **kwargs):
            assert method == "GET" and path == "/api/v1/fs/ls"  # Acceptance never calls Compile.
            return OpenVikingResult(True, 200, [{"uri": root + "/case.md", "name": "case.md", "isDir": False}])

        async def aclose(self):
            pass
    worker = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                            extras={"skill_mining_client_factory": lambda *_: Source()})
    pg_env.tx.pool.run(process_once(worker, owner_id="worker", kinds=["miner.skill_operation"]))
    with _pod(pg_env, tmp_path, monkeypatch, pod="replica") as client:
        _login(client, bootstrap=False)
        result = client.get(receipt["status_url"], headers=H).json()
        assert result["status"] == "succeeded", result
        job = result["result"]["jobs"][0]
    frozen = make_repository(worker, "default").get_job(job["job_id"])
    assert frozen["execution_protocol"] == PROTOCOL
    assert frozen["task_mode"] == "dialogue_dimensions"
    assert frozen["max_rounds"] == 1
    assert not any(key.startswith("staged_") for key in frozen)
    assert "taikang-dialogue-dimensions-v1" in frozen["compiler_body_snapshot"]
    assert frozen["dialogue_options"]["quote_mode"] == "none"
