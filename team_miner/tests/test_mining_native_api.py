from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from team_miner.bridge import SkillMinerBridgeMixin
from team_miner.native_compiler import PROTOCOL as NATIVE_PROTOCOL
from team_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY, OvSkillMiningRepository
from team_miner.settings import LEGACY_COMPILER_WARNING
from teamEvolver.config_store import ConfigStore


@pytest.fixture
def native_api(tmp_path):
    class Owner(SkillMinerBridgeMixin):
        def _start_ov_skill_monitor(self):
            pass

        def _schedule_ov_skill_round_submission(self, job):
            frozen = self._ov_skill_mining_repository().jobs_root / job["job_id"] / "compiler-skill.md"
            self.submissions.append({**job, "frozen_compiler": frozen.read_text()})

        async def _ov_source_detail(self, record):
            self.probed_sources.append(record["source_id"])
            return {**record, "ready": True, "status": "ready", "document_count": 1}

    config_file = tmp_path / "config.yaml"
    store = ConfigStore(config_file=config_file)
    data = store.load()
    data["mining"] = {"pipeline": {"max_rounds": 2, "runtime_timeout_seconds": 600}}
    store.save(data)
    owner = Owner()
    owner.submissions = []
    owner.probed_sources = []
    owner.config = SimpleNamespace(_config_file=str(config_file))
    owner._ov_skill_mining_repo = OvSkillMiningRepository(tmp_path / "state", tmp_path / "project")
    app = FastAPI()

    @app.middleware("http")
    async def admin(request: Request, call_next):
        request.state.console_user = {"role": request.headers.get("x-test-role", "admin")}
        return await call_next(request)

    owner._register_skillminer_routes(app)
    return TestClient(app), owner


def test_input_source_inventory_does_not_scan_every_remote_tree(native_api):
    client, owner = native_api
    repo = owner._ov_skill_mining_repository()
    first = repo.save_source("default", "viking://resources/source-a", name="source-a")
    repo.save_source("default", "viking://resources/source-b", name="source-b")

    response = client.get("/api/mining/input-sources")

    assert response.status_code == 200
    assert {row["source_id"] for row in response.json()["sources"]} == {
        row["source_id"] for row in repo.list_source_records()
    }
    assert all(row["status"] == "unverified" and not row["ready"] for row in response.json()["sources"])
    assert owner.probed_sources == []

    detail = client.get(f"/api/mining/input-sources/{first['source_id']}")
    assert detail.status_code == 200
    assert detail.json()["source"]["ready"] is True
    assert owner.probed_sources == [first["source_id"]]


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


@pytest.mark.parametrize(
    ("compiler_body", "expected_version"),
    [
        (SKILL_BODY, 2),
        (SKILL_BODY + "\nPreserve regional boundaries.\n", 2),
        (LEGACY_SKILL_BODY, 1),
        (LEGACY_SKILL_BODY + "\nPreserve historical task rules.\n", 1),
    ],
    ids=["default", "custom-collection", "legacy", "custom-single"],
)
def test_new_job_freezes_selected_compiler_contract_before_scheduling(native_api, compiler_body, expected_version):
    client, owner = native_api
    response = client.post("/api/mining/settings", json={"prompts": [{"id": "ov_compile", "prompt": compiler_body}]})
    assert response.status_code == 200, response.text
    repository = owner._ov_skill_mining_repository()
    source = repository.save_source(
        account="default", uri="viking://resources/policy", name="Policy", tags=["skill"]
    )
    response = client.post("/api/mining/jobs", json={"input_dir": source["uri"], "source_id": source["source_id"]})
    assert response.status_code == 201, response.text
    public = response.json()["jobs"][0]
    job = repository.get_job(public["job_id"])
    assert job["candidate_contract_version"] == public["candidate_contract_version"] == expected_version
    warnings = [LEGACY_COMPILER_WARNING] if expected_version == 1 else []
    assert job["options_warnings"] == public["options_warnings"] == warnings
    assert (repository.jobs_root / job["job_id"] / "compiler-skill.md").read_text() == compiler_body
    assert owner.submissions[0]["frozen_compiler"] == compiler_body
    assert owner._ov_skill_compiler_body() == compiler_body

    reset = client.post("/api/mining/prompts/ov_compile/reset")
    assert reset.status_code == 200, reset.text
    from team_miner.knowledge_compiler import SKILL_BODY as POINT_SKILL_BODY
    assert owner._ov_skill_compiler_body() == POINT_SKILL_BODY
    assert repository.get_job(job["job_id"])["candidate_contract_version"] == expected_version
    assert (repository.jobs_root / job["job_id"] / "compiler-skill.md").read_text() == compiler_body


def test_invalid_saved_compiler_does_not_create_or_schedule_job(native_api):
    client, owner = native_api
    data = owner._mining_store().load()
    data["mining"]["prompts"] = {"ov_compile": "Invalid stored compiler"}
    owner._mining_store().save(data)
    repository = owner._ov_skill_mining_repository()
    source = repository.save_source(
        account="default", uri="viking://resources/policy", name="Policy", tags=["skill"]
    )
    response = client.post("/api/mining/jobs", json={"input_dir": source["uri"], "source_id": source["source_id"]})
    assert response.status_code == 400 and "frontmatter" in response.json()["detail"]
    assert not owner.submissions and repository.list_jobs() == []


@pytest.mark.parametrize("mode,direction,count", [("auto", "客户沟通", None),
                                                  ("single", "完整客户沟通方法", 1), ("target", "", 4)])
def test_job_creation_freezes_user_scope_and_count(native_api, mode, direction, count):
    client, owner = native_api
    repo = owner._ov_skill_mining_repository()
    source = repo.save_source("default", "viking://resources/intent", tags=["skill"])
    response = client.post("/api/mining/jobs", json={"input_dir": source["uri"], "mining_mode": mode,
                                                  "mining_direction": direction, "target_skill_count": count})
    assert response.status_code == 201, response.text
    job = response.json()["jobs"][0]
    assert job["mining_intent"]["mode"] == mode
    assert job["mining_intent"]["direction"] == direction
    assert job["mining_intent"]["target_count"] == count
    assert job["empty_review_policy"] == "te-empty-review-skip.v1"
    assert job["execution_protocol"] == NATIVE_PROTOCOL
    assert job["output_uri"] == job["stable_output_uri"]
    assert "staged_identity_partition_protocol" not in job
    assert repo.get_job(job["job_id"])["mining_intent"] == owner.submissions[0]["mining_intent"]


def test_invalid_single_goal_creates_no_job_or_remote_work(native_api):
    client, owner = native_api
    repo = owner._ov_skill_mining_repository()
    source = repo.save_source("default", "viking://resources/intent", tags=["skill"])
    response = client.post("/api/mining/jobs", json={"input_dir": source["uri"], "mining_mode": "single"})
    assert response.status_code == 400 and "完整方法" in response.json()["detail"]
    assert not repo.list_jobs() and not owner.submissions


def test_completion_only_continue_api_preserves_success_until_explicit_feedback(native_api):
    from team_miner.knowledge_compiler import SKILL_BODY as POINT_COMPILER

    client, owner = native_api
    repo = owner._ov_skill_mining_repository()
    job = repo.create_job(name="完成后自愿修订", account="default", source_uri="viking://resources/source",
                          source_snapshot_uri="viking://resources/snapshot", output_uri="viking://resources/output",
                          document_count=1, max_rounds=2, compiler_body=POINT_COMPILER)
    repo.write_output(job["job_id"], "compiled_skill/point/SKILL.md", "未验收的原始返回正文")
    repo.update_job(job["job_id"], status="running", ov_status="completed")
    repo.finalize_materialized(job["job_id"])
    repo.finish_materialized_round(job["job_id"])
    current = client.get(f"/api/mining/jobs/{job['job_id']}").json()["job"]
    assert current["status"] == "succeeded" and current["pending_checkpoint"] is None
    checkpoint = current["optional_revision"]
    url = f"/api/mining/jobs/{job['job_id']}/continue"
    payload = {"checkpoint_id": checkpoint["id"], "feedback": "完善现有场景选择步骤", "request_id": "manual-2"}
    assert client.post(url, json=payload, headers={"x-test-role": "member"}).status_code == 403
    assert client.post(url, json={**payload, "feedback": ""}).status_code == 409
    assert repo.get_job(job["job_id"])["status"] == "succeeded"
    response = client.post(url, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["job"]["current_round"] == 2
    assert response.json()["job"]["completion_policy"] == "te-compile-finished.v1"
    assert not response.json()["job"]["content_validated"]
    duplicate = client.post(url, json=payload)
    assert duplicate.status_code == 200 and duplicate.json()["job"]["current_round"] == 2
    assert client.post(url, json={**payload, "feedback": "已改变的重复请求"}).status_code == 409


def test_resume_preserves_frozen_request_and_requires_the_identified_host_failure(native_api):
    from team_miner.ov_compile_mining import RECOVERABLE_IDENTITY_ERROR
    client, owner = native_api
    repo = owner._ov_skill_mining_repository()
    job = repo.create_job(name="第二轮身份复核", account="default", source_uri="viking://resources/source",
                          source_snapshot_uri="viking://resources/snapshot", output_uri="viking://resources/output",
                          document_count=1, max_rounds=2, compiler_body=SKILL_BODY)
    job = repo.update_job(job["job_id"], current_round=2, status="failed", error=RECOVERABLE_IDENTITY_ERROR,
                          finished_at="2026-10-14T10:24:40Z")
    root = repo.jobs_root / job["job_id"] / "round_inputs/round-2/staged"
    root.mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({"round": 2, "phase": "identity_group", "current": None,
                                               "complete": False}))
    frozen = root / "request.json"
    frozen.write_text('{"original":"immutable request"}')
    before = frozen.read_bytes()
    denied = client.post(f"/api/mining/jobs/{job['job_id']}/resume", headers={"x-test-role": "member"})
    assert denied.status_code == 403
    response = client.post(f"/api/mining/jobs/{job['job_id']}/resume")
    assert response.status_code == 200, response.text
    resumed = response.json()["job"]
    assert resumed["status"] == "preparing" and resumed["current_round"] == 2
    assert resumed["recovery_history"][0]["error"] == RECOVERABLE_IDENTITY_ERROR
    assert frozen.read_bytes() == before
    repo.update_job(job["job_id"], status="failed", error="Other unverified failure")
    assert client.post(f"/api/mining/jobs/{job['job_id']}/resume").status_code == 409
