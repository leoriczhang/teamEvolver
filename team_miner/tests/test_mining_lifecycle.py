from __future__ import annotations

import json
from pathlib import Path

import pytest

from team_miner import benchmark_format as progressive_benchmark
from team_miner.lifecycle import (
    INTERNAL_BENCHMARK_FORMAT,
    MiningLifecycleError,
    list_mined_skill_statuses,
    resolve_mined_job_skill_root,
    resolve_mined_job_workspace,
    resolve_mined_skill_dir,
    submit_mined_skill,
)
from team_miner.tests.dataset_fixtures import build_document
from team_skills.candidates.store import ValidationStore
from teamEvolver.storage import InMemoryObjectStore


def _store(root: Path) -> ValidationStore:
    return ValidationStore.from_bucket(bucket=InMemoryObjectStore(str(root / "objects")))


def _write_mined_skill(root: Path, name: str = "demo-skill") -> Path:
    skill_dir = root / "compiled_skill" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Demo mined skill\n"
        "category: support\n"
        "---\n\n"
        "# Procedure\n\nFollow the verified workflow.\n",
        encoding="utf-8",
    )
    (skill_dir / "EVALUATION.md").write_text("# Evaluation\n", encoding="utf-8")
    question = {
        "id": "BM-01",
        "target_dimensions": ["accuracy"],
        "difficulty": "medium",
        "input": "Handle this support case.",
        "gold": {
            "expected_label": {"result": "accepted"},
            "must_hit": ["cite the verified rule"],
            "must_avoid": ["invent evidence"],
        },
    }
    document = build_document(name, [question])
    progressive_benchmark.write_document(skill_dir / "benchmark.json", document)
    (skill_dir / "BENCHMARK.md").write_text("# Benchmark\n", encoding="utf-8")
    return skill_dir


def test_submit_mined_skill_creates_internal_candidate_and_is_idempotent(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    _write_mined_skill(miner_root)
    store = _store(tmp_path)

    first = submit_mined_skill(
        store,
        "demo-skill",
        submitted_by="admin",
        skillminer_root=miner_root,
    )
    second = submit_mined_skill(
        store,
        "demo-skill",
        submitted_by="admin",
        skillminer_root=miner_root,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["job"]["job_id"] == first["job"]["job_id"]
    job = store.load_job(first["job"]["job_id"])
    assert job is not None
    assert job["source"]["kind"] == "skillminer"
    assert job["source"]["dataset_format"] == INTERNAL_BENCHMARK_FORMAT
    assert job["source"]["question_count"] == 1
    document = json.loads((miner_root / "compiled_skill/demo-skill/benchmark.json").read_text())
    assert job["replay_cases"] == document["cases"]
    assert job["replay_cases"][0]["metadata"]["gold"]["must_hit"] == ["cite the verified rule"]
    assert "benchmark.json" in {item["path"] for item in job["candidate_skill"]["bundle"]["files"]}
    assert "EVALUATION.md" in {item["path"] for item in job["candidate_skill"]["bundle"]["files"]}


def test_mined_skill_status_tracks_candidate_and_publish_decision(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    _write_mined_skill(miner_root)
    store = _store(tmp_path)
    submitted = submit_mined_skill(store, "demo-skill", skillminer_root=miner_root)
    job_id = submitted["job"]["job_id"]

    pending = list_mined_skill_statuses(store, skillminer_root=miner_root)
    assert pending[0]["status"] == "candidate"
    assert pending[0]["job_id"] == job_id

    store.save_decision(job_id, {"status": "published", "accepted": True})
    published = list_mined_skill_statuses(
        store,
        registered_skill_names=["demo-skill"],
        skillminer_root=miner_root,
    )
    assert published[0]["status"] == "published"
    assert published[0]["registered"] is True


def test_submit_requires_complete_artifacts_and_rejects_path_escape(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    skill_dir = _write_mined_skill(miner_root)
    (skill_dir / "benchmark.json").unlink()

    with pytest.raises(MiningLifecycleError, match="benchmark.json"):
        submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)
    with pytest.raises(MiningLifecycleError):
        resolve_mined_skill_dir("../demo-skill", skillminer_root=miner_root)


def test_submit_mined_skill_accepts_progressive_benchmark_json(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    skill_dir = _write_mined_skill(miner_root)
    (skill_dir / "benchmark.json").unlink()
    benchmark = build_document(
        "demo-skill",
        [{
            "id": "BM-01",
            "input": "Handle this support case.",
            "gold": {"must_hit": [f"criterion {index}" for index in range(1, 13)]},
            "trajectory_requirements": ["confirm facts"],
        }],
    )
    progressive_benchmark.write_document(skill_dir / "benchmark.json", benchmark)

    submitted = submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)

    assert submitted["created"] is True
    assert submitted["job"]["source"]["question_count"] == 1
    assert submitted["job"]["replay_cases"][0]["metadata"]["gold"]["must_hit"] == [
        f"criterion {index}" for index in range(1, 13)
    ]


def test_completed_job_workspace_can_submit_edited_artifacts(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    job_id = "mine-001"
    job_dir = miner_root / "mining_jobs" / job_id
    workspace = job_dir / "workspace"
    _write_mined_skill(workspace)
    (workspace / "compiled_skill" / "demo-skill" / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Human refined skill\n"
        "category: support\n"
        "---\n\n"
        "# Refined procedure\n",
        encoding="utf-8",
    )
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": "succeeded"}),
        encoding="utf-8",
    )

    resolved = resolve_mined_job_workspace(job_id, skillminer_root=miner_root)
    submitted = submit_mined_skill(
        _store(tmp_path),
        "demo-skill",
        skillminer_root=resolved,
        mining_job_id=job_id,
    )

    assert resolved == workspace
    assert submitted["job"]["candidate_skill"]["description"] == "Human refined skill"
    assert submitted["job"]["source"]["mining_job_id"] == job_id


def test_completed_job_workspace_submits_current_benchmark_json(tmp_path: Path) -> None:
    """The task-detail hand-off must accept the current non-JSONL artifact."""
    miner_root = tmp_path / "skillminer"
    job_id = "mine-progressive-json"
    job_dir = miner_root / "mining_jobs" / job_id
    workspace = job_dir / "workspace"
    skill_dir = _write_mined_skill(workspace)
    (skill_dir / "benchmark.json").unlink()
    progressive_benchmark.write_document(
        skill_dir / "benchmark.json",
        build_document(
            "demo-skill",
            [{
                "id": "BM-01",
                "input": "Handle this support case.",
                "requirements": [f"criterion {index}" for index in range(1, 13)],
                "trajectory_requirements": ["confirm facts"],
            }],
        ),
    )
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": "succeeded"}),
        encoding="utf-8",
    )

    workspace_root = resolve_mined_job_skill_root(
        job_id,
        "demo-skill",
        skillminer_root=miner_root,
    )
    submitted = submit_mined_skill(
        _store(tmp_path),
        "demo-skill",
        skillminer_root=workspace_root,
        mining_job_id=job_id,
    )

    assert submitted["created"] is True
    assert submitted["job"]["source"]["mining_job_id"] == job_id
    assert submitted["job"]["source"]["question_count"] == 1


def test_non_completed_job_cannot_enter_evolution(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    job_id = "mine-running"
    job_dir = miner_root / "mining_jobs" / job_id
    _write_mined_skill(job_dir / "workspace")
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": "running"}),
        encoding="utf-8",
    )

    with pytest.raises(MiningLifecycleError, match="已完成"):
        resolve_mined_job_workspace(job_id, skillminer_root=miner_root)
    with pytest.raises(MiningLifecycleError):
        resolve_mined_job_workspace("../mine-running", skillminer_root=miner_root)




def test_console_mined_submission_uses_tenant_hub_and_complete_bundles(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from team_skills.library.bundle import candidate_skill_bundle
    from team_skills.library.hub import SkillHub
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.proxy import ProxyServer, routes

    miner_root = tmp_path / "miner"
    skill_dir = _write_mined_skill(miner_root)
    published_bundle = {"SKILL.md": (skill_dir / "SKILL.md").read_bytes(), "references/rule.md": b"published rule"}
    store = _store(tmp_path)
    captured = []

    class Hub:
        def list_skills(self):
            return [{"name": "demo-skill"}]

        def read_skill_bundle(self, name):
            assert name == "demo-skill"
            return published_bundle

    def hub_for_config(config, *, tenant_id):
        captured.append(tenant_id)
        return Hub()

    monkeypatch.setattr(SkillHub, "team_from_config", hub_for_config)
    monkeypatch.setattr(routes.ValidationStore, "from_config", lambda *args, **kwargs: store)
    original_submit = routes.submit_mined_skill
    monkeypatch.setattr(routes, "submit_mined_skill", lambda *args, **kwargs: original_submit(
        *args, **kwargs, skillminer_root=miner_root
    ))
    server = ProxyServer(TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "cache"),
        sharing_skill_mirror_enabled=False,
    ))
    client = TestClient(server.app)
    assert client.get("/api/mined-skills").status_code == 401
    response = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    from teamEvolver.admin.identity_store import IdentityStore

    IdentityStore(server.config).set_member("default", "admin", "tenant_admin", "active", "admin")
    response = client.post("/api/mined-skills/demo-skill/submit", headers={"X-Tenant-Id": "default"})
    assert response.status_code == 200, response.text
    job = store.load_job(response.json()["job_id"])
    assert candidate_skill_bundle(job["current_skill"]) == published_bundle
    assert (
        candidate_skill_bundle(job["candidate_skill"])["benchmark.json"]
        == (skill_dir / "benchmark.json").read_bytes()
    )
    assert job["replay_cases"][0]["checks"]
    assert captured and set(captured) == {"default"}


def test_jsonl_artifact_cannot_enter_candidate_review(tmp_path):
    from team_miner.lifecycle import load_internal_benchmark

    skill_dir = _write_mined_skill(tmp_path / "miner")
    (skill_dir / "benchmark.json").unlink()
    (skill_dir / "benchmark.jsonl").write_text('{"input": "Old task"}\n')
    with pytest.raises(MiningLifecycleError, match="缺少 benchmark.json"):
        load_internal_benchmark(skill_dir)
