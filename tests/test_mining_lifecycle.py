from __future__ import annotations

import json
from pathlib import Path

import pytest

from teamEvolver.mining_lifecycle import (
    INTERNAL_BENCHMARK_FORMAT,
    MiningLifecycleError,
    list_mined_skill_statuses,
    resolve_mined_job_skill_root,
    resolve_mined_job_workspace,
    resolve_mined_skill_dir,
    submit_mined_skill,
)
from teamEvolver.skillminer import benchmark_format as progressive_benchmark
from teamEvolver.storage import InMemoryObjectStore
from teamEvolver.validation.store import ValidationStore


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
    (skill_dir / "benchmark.jsonl").write_text(
        json.dumps(question, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
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
    assert job["replay_cases"][0]["gold"]["must_hit"] == ["cite the verified rule"]
    assert "benchmark.jsonl" in job["candidate_skill"]["bundle_files"]
    assert "EVALUATION.md" in job["candidate_skill"]["bundle_files"]


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
    (skill_dir / "benchmark.jsonl").unlink()

    with pytest.raises(MiningLifecycleError, match="benchmark.json"):
        submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)
    with pytest.raises(MiningLifecycleError):
        resolve_mined_skill_dir("../demo-skill", skillminer_root=miner_root)


def test_submit_mined_skill_accepts_progressive_benchmark_json(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    skill_dir = _write_mined_skill(miner_root)
    (skill_dir / "benchmark.jsonl").unlink()
    benchmark = progressive_benchmark.build_document(
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
    assert submitted["job"]["replay_cases"][0]["gold"]["must_hit"] == [
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
    (skill_dir / "benchmark.jsonl").unlink()
    progressive_benchmark.write_document(
        skill_dir / "benchmark.json",
        progressive_benchmark.build_document(
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


def test_legacy_job_skill_root_is_resolved_from_its_archived_artifact(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    older = "20260805_150618_777997"
    newer = "20260809_223901_916214"
    (miner_root / "reflection_rounds" / older).mkdir(parents=True)
    (miner_root / "reflection_rounds" / newer).mkdir(parents=True)
    snapshot = miner_root / "run_history" / newer / "preexisting"
    skill_dir = _write_mined_skill(snapshot)
    artifact_path = skill_dir.joinpath("SKILL.md").relative_to(miner_root).as_posix()

    resolved = resolve_mined_job_skill_root(
        f"legacy:{older}",
        "demo-skill",
        artifact_path=artifact_path,
        skillminer_root=miner_root,
    )

    assert resolved == snapshot
    with pytest.raises(MiningLifecycleError, match="不匹配"):
        resolve_mined_job_skill_root(
            f"legacy:{newer}",
            "demo-skill",
            artifact_path=artifact_path,
            skillminer_root=miner_root,
        )
