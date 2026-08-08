from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


SKILLMINER_DIR = Path(__file__).resolve().parents[1] / "skillgene" / "skillminer"
sys.path.insert(0, str(SKILLMINER_DIR))

import lift_integration as li  # noqa: E402


def _question(qid: str, difficulty: str, dimension: str) -> dict:
    return {
        "id": qid,
        "difficulty": difficulty,
        "target_dimensions": [dimension],
        "input": f"请处理业务情境 {qid}",
        "gold": {
            "expected_label": {"风险": "高" if difficulty == "hard" else "低"},
            "must_hit": [f"命中要点 {qid}"],
            "must_avoid": ["越权承诺"],
        },
        "customer_sim": {
            "hidden_facts": ["关键事实尚未披露"],
            "reveal_rules": "主动询问时才说明",
        },
    }


def _configure_paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    skillminer_root = tmp_path / "skillminer"
    datasets_root = skillminer_root / "lift_datasets"
    lift_root = tmp_path / "LIFT"
    monkeypatch.setattr(li, "PROJECT_ROOT", skillminer_root)
    monkeypatch.setattr(li, "LIFT_DATASETS_ROOT", datasets_root)
    monkeypatch.setattr(li, "DRAFTS_DIR", datasets_root / "drafts")
    monkeypatch.setattr(li, "PUBLISHED_DIR", datasets_root / "published")
    monkeypatch.setattr(li, "RUNS_DIR", datasets_root / "runs")
    monkeypatch.setenv("SKILLGENE_LIFT_ROOT", str(lift_root))
    monkeypatch.setenv("SKILLGENE_LIFT_PYTHON", sys.executable)
    return skillminer_root, lift_root


def _write_source_skill(skillminer_root: Path) -> Path:
    skill_dir = skillminer_root / "compiled_skill" / "demo-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\n# Demo\n", encoding="utf-8")
    (skill_dir / "EVALUATION.md").write_text("# Evaluation\n", encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text("guide\n", encoding="utf-8")
    questions = [
        _question("BM-01", "easy", "维度一"),
        _question("BM-02", "medium", "维度二"),
        _question("BM-03", "hard", "维度一"),
    ]
    (skill_dir / "benchmark.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in questions),
        encoding="utf-8",
    )
    return skill_dir


def _write_lift_markers(lift_root: Path) -> None:
    (lift_root / "src" / "cli").mkdir(parents=True)
    (lift_root / "src" / "models.py").write_text("# marker\n", encoding="utf-8")
    (lift_root / "src" / "cli" / "lift_main.py").write_text("# marker\n", encoding="utf-8")


def test_build_suite_matches_lift_suite_v1_contract():
    questions = [
        _question("BM-01", "easy", "维度一"),
        _question("BM-02", "medium", "维度二"),
        _question("BM-03", "hard", "维度一"),
    ]

    suite, metrics = li.build_suite_from_questions(
        "demo-skill", questions, suite_name="Demo Suite", category="Demo", warmup_ratio=2 / 3
    )

    assert set(suite) == {"name", "category", "warmup_tasks", "holdout_tasks"}
    assert len(suite["warmup_tasks"]) == 2
    assert len(suite["holdout_tasks"]) == 1
    for task in suite["warmup_tasks"] + suite["holdout_tasks"]:
        assert set(task) == {"name", "query", "requirements", "expected_result"}
        assert set(task["requirements"]) == {"default_skills", "extra_skills_dir", "material_dir"}
        assert set(task["expected_result"]) == {"content_reqs", "trajectory_reqs"}
        assert task["query"]
        assert task["expected_result"]["content_reqs"].startswith("1. ")
        assert task["requirements"]["extra_skills_dir"] == "assets/benchmark_mds/skillgene/demo-suite/skills"
    assert metrics["dimension_overlap_pct"] == 100.0
    assert li.validate_suite(suite)["valid"] is True


def test_validate_suite_rejects_missing_holdout_and_empty_query():
    suite = {
        "name": "broken",
        "category": "broken",
        "warmup_tasks": [{
            "name": "W1",
            "query": "",
            "requirements": {},
            "expected_result": {"content_reqs": "", "trajectory_reqs": ""},
        }],
        "holdout_tasks": [],
    }

    result = li.validate_suite(suite)

    assert result["valid"] is False
    assert any("holdout" in error for error in result["errors"])
    assert any("query" in error for error in result["errors"])


def test_draft_review_publish_lifecycle_writes_lift_source_and_json(tmp_path, monkeypatch):
    skillminer_root, lift_root = _configure_paths(tmp_path, monkeypatch)
    _write_source_skill(skillminer_root)
    _write_lift_markers(lift_root)

    created = li.create_draft("demo-skill", suite_name="demo-suite", category="demo")
    draft_id = created["manifest"]["id"]
    assert created["manifest"]["state"] == "draft"
    draft_dir = li.DRAFTS_DIR / draft_id
    task_md = next((draft_dir / "source" / "demo-suite" / "train").glob("q*/*.md"))
    assert "### query" in task_md.read_text(encoding="utf-8")
    assert (draft_dir / "source" / "demo-suite" / "skills" / "demo-skill" / "SKILL.md").is_file()

    edited = created["suite"]
    edited["warmup_tasks"][0]["query"] = "人工审核后的 query"
    saved = li.save_draft(draft_id, edited)
    assert saved["manifest"]["state"] == "draft"
    approved = li.approve_draft(draft_id, "reviewer-a", "checked")
    assert approved["manifest"]["state"] == "approved"

    published = li.publish_draft(draft_id)
    assert published["manifest"]["state"] == "published"
    suite_json = lift_root / "assets" / "benchmarks" / "skillgene" / "demo-suite.json"
    source_scene = lift_root / "assets" / "benchmark_mds" / "skillgene" / "demo-suite"
    assert json.loads(suite_json.read_text(encoding="utf-8"))["warmup_tasks"][0]["query"] == "人工审核后的 query"
    assert (source_scene / "skills" / "demo-skill" / "references" / "guide.md").is_file()

    # 再次编辑、审核、发布时，旧版本会先进入可恢复历史目录。
    edited_again = published["suite"]
    edited_again["holdout_tasks"][0]["query"] = "第二版 holdout"
    li.save_draft(draft_id, edited_again)
    li.approve_draft(draft_id, "reviewer-b")
    republished = li.publish_draft(draft_id)
    backup = Path(republished["manifest"]["published_paths"]["backup"])
    assert backup.is_dir()
    assert (backup / "benchmarks" / "demo-suite.json").is_file()


def test_build_lift_command_uses_published_suite_and_safe_hermes_policy(tmp_path, monkeypatch):
    _skillminer_root, lift_root = _configure_paths(tmp_path, monkeypatch)
    _write_lift_markers(lift_root)
    suite_path = lift_root / "assets" / "benchmarks" / "skillgene" / "demo-suite.json"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text("{}\n", encoding="utf-8")
    ready_status = li.lift_status()
    ready_status["python_ready"] = True
    monkeypatch.setattr(li, "lift_status", lambda: ready_status)

    command, metadata = li.build_lift_command({
        "suite": "demo-suite.json",
        "runtime": "hermes",
        "repeat": 2,
        "max_parallel_suites": 1,
        "run_id": "reviewed-run",
    })

    assert command[:3] == [sys.executable, "-m", "src.cli.lift_main"]
    assert "--warmup-container-policy" in command
    assert command[command.index("--warmup-container-policy") + 1] == "serial_single"
    assert metadata["suite"] == "demo-suite.json"
    assert metadata["result_dir"].endswith("results/lift-runid-reviewed-run")


def test_publish_failure_restores_previous_lift_dataset(tmp_path, monkeypatch):
    skillminer_root, lift_root = _configure_paths(tmp_path, monkeypatch)
    _write_source_skill(skillminer_root)
    _write_lift_markers(lift_root)
    created = li.create_draft("demo-skill", suite_name="demo-suite", category="demo")
    draft_id = created["manifest"]["id"]
    li.approve_draft(draft_id, "reviewer-a")
    first = li.publish_draft(draft_id)
    suite_path = Path(first["manifest"]["published_paths"]["suite_json"])
    original_suite = suite_path.read_text(encoding="utf-8")

    edited = first["suite"]
    edited["warmup_tasks"][0]["query"] = "不应留下的新版内容"
    li.save_draft(draft_id, edited)
    li.approve_draft(draft_id, "reviewer-b")
    original_copytree = shutil.copytree

    def fail_external_scene_copy(src, dst, *args, **kwargs):
        if Path(dst) == lift_root / "assets" / "benchmark_mds" / "skillgene" / "demo-suite":
            raise OSError("simulated publish failure")
        return original_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(li.shutil, "copytree", fail_external_scene_copy)

    try:
        li.publish_draft(draft_id)
    except OSError as exc:
        assert "simulated publish failure" in str(exc)
    else:
        raise AssertionError("publish should have failed")

    assert suite_path.read_text(encoding="utf-8") == original_suite
    assert li.get_draft(draft_id)["manifest"]["state"] == "approved"


def test_draft_identifier_does_not_accept_path_or_sanitized_alias(tmp_path, monkeypatch):
    _configure_paths(tmp_path, monkeypatch)

    for unsafe in ("../draft", "draft/child", "draft with spaces", ""):
        try:
            li.get_draft(unsafe)
        except ValueError as exc:
            assert "非法数据集标识" in str(exc)
        else:
            raise AssertionError(f"unsafe draft id was accepted: {unsafe!r}")
