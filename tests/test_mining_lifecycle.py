from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillgene.mining_lifecycle import (
    INTERNAL_BENCHMARK_FORMAT,
    MiningLifecycleError,
    list_mined_skill_statuses,
    resolve_mined_skill_dir,
    submit_mined_skill,
)
from skillgene.validation.store import ValidationStore


def _store(root: Path) -> ValidationStore:
    return ValidationStore(backend="local", endpoint="", local_root=str(root / "objects"))


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

    with pytest.raises(MiningLifecycleError, match="benchmark.jsonl"):
        submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)
    with pytest.raises(MiningLifecycleError):
        resolve_mined_skill_dir("../demo-skill", skillminer_root=miner_root)
