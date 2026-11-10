"""The original task flow uses the saved editable compiler without bundled assets."""

import pytest

from team_miner.skill_miner.dialogue_dimensions import PROTOCOL
from team_miner.skill_miner.dialogue_dimensions.compiler import SKILL_BODY
from team_miner.skill_miner.handlers import register_handlers
from team_miner.skill_miner.repository import make_repository
from teamEvolver.openviking import OpenVikingResult
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod


@pytest.mark.postgres
def test_saved_compiler_selects_dialogue_execution_without_new_request_fields(pg_env, tmp_path, monkeypatch):
    services = build_services(role="api", tx=pg_env.tx)
    root = "viking://resources/dialogue-dataset/model_cases"
    source = make_repository(services, "default").save_source("acct-default", root, name="优秀对话", tags=["skill"])
    with _pod(pg_env, tmp_path, monkeypatch, pod="api") as client:
        _login(client, bootstrap=True)
        current = client.get("/api/mining/settings", headers=H).json()
        custom = SKILL_BODY + "\n用户指定：保留开场行为的适用条件。\n"
        saved = client.post("/api/mining/settings", headers=H, json={
            "expected_revision": current["revision"],
            "prompts": [{"id": "ov_compile", "prompt": custom}],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["prompts"][0]["effective_prompt"] == custom
        response = client.post("/api/mining/jobs", headers=H, json={
            "name": "优秀对话", "input_dir": root,
            "source_id": source["source_id"],
            "max_rounds": 3, "mining_mode": "auto"})
        assert response.status_code == 202, response.text
        receipt = response.json()

    class Source:
        async def request(self, method, path, **kwargs):
            assert method == "GET"  # Acceptance never calls Compile.
            if path == "/api/v1/content/read":
                assert kwargs["params"]["uri"] == "viking://resources/dialogue-dataset/dataset_manifest.json"
                return OpenVikingResult(True, 200, {"content": "{}"})
            assert path == "/api/v1/fs/ls"
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
    assert "compiler_body_snapshot" not in frozen
    assert not (make_repository(worker, "default").jobs_root / job["job_id"] / "compiler-assets").exists()
    assert frozen["config_snapshot"]["settings"]["mining"]["prompts"]["ov_compile"] == custom
    assert frozen["dialogue_options"]["quote_mode"] == "none"
