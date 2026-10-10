from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from team_miner.bridge import SkillMinerBridgeMixin
from team_miner.ov_compile_mining import OvSkillMiningRepository
from teamEvolver.config_store import ConfigStore


@pytest.fixture
def native_api(tmp_path):
    class Owner(SkillMinerBridgeMixin):
        def _start_ov_skill_monitor(self):
            pass

    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file=config_file)
    data = store.load()
    data["mining"] = {"pipeline": {"max_rounds": 2, "runtime_timeout_seconds": 600}}
    store.save(data)
    owner = Owner()
    owner.config = SimpleNamespace(_config_file=str(config_file))
    owner._ov_skill_mining_repo = OvSkillMiningRepository(tmp_path / "state", tmp_path / "project")
    app = FastAPI()

    @app.middleware("http")
    async def admin(request: Request, call_next):
        request.state.console_user = {"role": request.headers.get("x-test-role", "admin")}
        return await call_next(request)

    owner._register_skillminer_routes(app)
    return TestClient(app), owner


def test_config_uses_native_saved_defaults_and_no_execution_subprocess(native_api):
    client, owner = native_api
    result = client.get("/api/mining/config").json()
    assert result["engine"] == "ov_compile"
    assert result["max_rounds_default"] == 2
    assert result["max_rounds_range"] == [1, 5]
    assert result["runtime_timeout_seconds_default"] == 600
    assert result["compile_model"]["configuration_source"] == "openviking_server"
    assert not hasattr(owner, "_skillminer_proc")
    for path in ("/api/mining/lift/run", "/api/mining/trajectory-benchmarks", "/api/mining/benchmark"):
        assert client.post(path, json={}).status_code == 404


def test_current_job_artifact_preview_and_edit_are_native(native_api):
    client, owner = native_api
    repository = owner._ov_skill_mining_repository()
    job = repository.create_job(
        name="Native artifact", account="default", source_uri="viking://resources/source",
        source_snapshot_uri="viking://resources/frozen", output_uri="viking://resources/output",
        document_count=1, max_rounds=1,
    )
    repository.write_output(job["job_id"], "compiled_skill/demo/SKILL.md", "# Original\n")
    repository.update_job(job["job_id"], status="waiting_human")
    relative = f"mining_jobs/{job['job_id']}/workspace/compiled_skill/demo/SKILL.md"
    response = client.get("/api/mining/artifacts/content", params={"path": relative})
    assert response.status_code == 200
    assert response.json()["content"] == "# Original\n"
    saved = client.post("/api/mining/artifacts/content", json={"path": relative, "content": "# Revised\n"})
    assert saved.status_code == 200
    assert saved.json()["edited"] is True
    assert repository.workspace(job).joinpath("compiled_skill/demo/SKILL.md").read_text() == "# Revised\n"
    denied = client.post("/api/mining/artifacts/content", headers={"x-test-role": "member"},
                         json={"path": relative, "content": "forbidden"})
    assert denied.status_code == 403
    repository.update_job(job["job_id"], status="running")
    blocked = client.post("/api/mining/artifacts/content", json={"path": relative, "content": "blocked"})
    assert blocked.status_code == 400


def test_compiler_settings_persist_for_new_rounds(native_api):
    client, owner = native_api
    prompt = owner._ov_skill_compiler_body() + "\nExplain conflicting Evidence.\n"
    response = client.post("/api/mining/settings", json={
        "pipeline": {"max_rounds": 4, "runtime_timeout_seconds": 900},
        "prompts": [{"id": "ov_compile", "prompt": prompt}],
    })
    assert response.status_code == 200, response.json()
    assert owner._ov_skill_compiler_body() == prompt
    assert client.get("/api/mining/config").json()["max_rounds_default"] == 4
    persisted = ConfigStore(config_file=Path(owner.config._config_file)).load()["mining"]
    assert persisted == {"pipeline": {"max_rounds": 4, "runtime_timeout_seconds": 900},
                         "prompts": {"ov_compile": prompt}}
    assert "api_key" not in json.dumps(response.json())
