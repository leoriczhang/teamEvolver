from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SKILLMINER_ROOT = Path(__file__).resolve().parents[1] / "teamEvolver" / "skillminer"
if str(SKILLMINER_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLMINER_ROOT))

import benchmark_format as bf  # noqa: E402
from mining_jobs import MiningJobManager  # noqa: E402


def _progressive_benchmark_json() -> str:
    payload = bf.build_document(
        "demo",
        [{
            "id": "BM-01",
            "name": "测试场景",
            "input": "test",
            "gold": {"must_hit": [f"requirement {index}" for index in range(1, 13)]},
            "trajectory_requirements": ["confirm facts", "verify result"],
        }],
        created_at="2026-08-13T00:00:00+00:00",
    )
    return json.dumps(payload, ensure_ascii=False)


def _prepare_project(root: Path) -> None:
    for source_name in ("alpha", "beta"):
        source = root / "data" / source_name
        source.mkdir(parents=True)
        (source / f"{source_name}.md").write_text(f"knowledge for {source_name}", encoding="utf-8")
    for name in (
        "sample-package-constructor-agent-skill",
        "semantic-discovery-agent-skill",
        "evaluation-compiler-agent-skill",
    ):
        (root / name).mkdir()
    for name in (
        "sample_package_constructor_agent_prompt.py",
        "semantic_discovery_agent_prompt.py",
        "evaluation_compiler_agent_prompt.py",
    ):
        (root / name).write_text("PROMPT = ''\n", encoding="utf-8")
    benchmark_json = _progressive_benchmark_json()
    (root / "run_pipeline.py").write_text(
        f"""from pathlib import Path
print('[第 1 轮][Step 1/3] sample')
print('[第 1 轮][Step 2/3] semantic')
print('[第 1 轮][Step 3/3] compile')
target = Path('compiled_skill') / 'demo'
target.mkdir(parents=True, exist_ok=True)
(target / 'SKILL.md').write_text('# Demo skill', encoding='utf-8')
(target / 'EVALUATION.md').write_text('# Demo evaluation', encoding='utf-8')
(target / 'benchmark.json').write_text({benchmark_json!r}, encoding='utf-8')
(target / 'BENCHMARK.md').write_text('# Demo benchmark', encoding='utf-8')
print('编译产物契约校验通过')
""",
        encoding="utf-8",
    )


def _wait_finished(manager: MiningJobManager, job_ids: list[str]) -> list[dict]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        jobs = [manager.get_job(job_id) for job_id in job_ids]
        if all(job["status"] not in {"preparing", "queued", "running", "stopping"} for job in jobs):
            return jobs
        time.sleep(0.05)
    raise AssertionError("parallel mining jobs did not finish in time")


def test_parallel_jobs_are_isolated_persisted_and_expose_artifacts(tmp_path):
    _prepare_project(tmp_path)
    manager = MiningJobManager(tmp_path, max_parallel=2)

    jobs = manager.create_jobs({
        "jobs": [
            {"name": "alpha mining", "input_dir": "data/alpha", "max_rounds": 2},
            {"name": "beta mining", "input_dir": "data/beta", "max_rounds": 3},
        ]
    })
    job_ids = [job["job_id"] for job in jobs]
    finished = _wait_finished(manager, job_ids)

    assert [job["status"] for job in finished] == ["succeeded", "succeeded"]
    assert all(job["phase"] == {"step1": "done", "step2": "done", "step3": "done"} for job in finished)
    assert all(any(item["name"] == "SKILL.md" for item in job["artifacts"]) for job in finished)
    assert all(any(item["name"] == "benchmark.json" for item in job["artifacts"]) for job in finished)

    alpha_workspace = tmp_path / "mining_jobs" / job_ids[0] / "workspace"
    beta_workspace = tmp_path / "mining_jobs" / job_ids[1] / "workspace"
    assert (alpha_workspace / "data" / "input" / "alpha.md").is_file()
    assert not (alpha_workspace / "data" / "input" / "beta.md").exists()
    assert (beta_workspace / "data" / "input" / "beta.md").is_file()
    assert not (beta_workspace / "data" / "input" / "alpha.md").exists()

    restored = MiningJobManager(tmp_path, max_parallel=2)
    assert {job["job_id"] for job in restored.list_jobs()} == set(job_ids)
    assert all(restored.get_job(job_id)["status"] == "succeeded" for job_id in job_ids)


def test_delete_completed_job_removes_workspace_and_keeps_knowledge_source(tmp_path):
    _prepare_project(tmp_path)
    manager = MiningJobManager(tmp_path, max_parallel=1)
    job = manager.create_jobs({
        "name": "delete after completion",
        "input_dir": "data/alpha",
        "max_rounds": 1,
    })[0]
    _wait_finished(manager, [job["job_id"]])

    job_dir = tmp_path / "mining_jobs" / job["job_id"]
    assert (job_dir / "workspace" / "compiled_skill" / "demo" / "SKILL.md").is_file()

    result = manager.delete_job(job["job_id"])

    assert result["ok"] is True
    assert result["deleted_files"] > 0
    assert not job_dir.exists()
    assert job["job_id"] not in {item["job_id"] for item in manager.list_jobs()}
    assert (tmp_path / "data" / "alpha" / "alpha.md").is_file()
    try:
        manager.get_job(job["job_id"])
    except KeyError:
        pass
    else:
        raise AssertionError("deleted mining job remained addressable")


def test_loading_manager_without_start_is_read_only(tmp_path):
    _prepare_project(tmp_path)
    job_id = "mine-existing-waiting"
    job_dir = tmp_path / "mining_jobs" / job_id
    checkpoint_dir = job_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    job = {
        "job_id": job_id,
        "name": "existing waiting job",
        "status": "waiting",
        "created_at": "2026-08-13T00:00:00+00:00",
        "updated_at": "2026-08-13T00:00:00+00:00",
        "workspace": f"mining_jobs/{job_id}/workspace",
    }
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    pending = {"id": "checkpoint-existing", "questions": []}
    pending_path = checkpoint_dir / "pending.json"
    pending_path.write_text(json.dumps(pending), encoding="utf-8")

    manager = MiningJobManager(tmp_path, start_immediately=False)

    assert manager.get_job(job_id)["status"] == "waiting"
    assert json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"] == "waiting"
    assert pending_path.is_file()


def test_zero_exit_with_missing_benchmark_completes_with_incomplete_quality_marker(tmp_path):
    _prepare_project(tmp_path)
    (tmp_path / "run_pipeline.py").write_text(
        """from pathlib import Path
target = Path('compiled_skill') / 'demo'
target.mkdir(parents=True, exist_ok=True)
(target / 'SKILL.md').write_text('# Demo skill', encoding='utf-8')
(target / 'EVALUATION.md').write_text('# Demo evaluation', encoding='utf-8')
print('编译产物契约校验通过')
""",
        encoding="utf-8",
    )
    manager = MiningJobManager(tmp_path, max_parallel=1)
    job = manager.create_jobs({
        "name": "incomplete mining",
        "input_dir": "data/alpha",
        "max_rounds": 1,
        "human_checkpoints": False,
    })[0]

    finished = _wait_finished(manager, [job["job_id"]])[0]

    assert finished["status"] == "succeeded"
    assert finished["error"] == ""
    assert finished["artifact_quality"]["level"] == "incomplete"
    assert finished["artifact_quality"]["can_submit"] is True
    assert any("Benchmark" in warning for warning in finished["artifact_quality"]["warnings"])


def test_nonzero_exit_keeps_partial_skill_as_completed_with_review_warning(tmp_path):
    _prepare_project(tmp_path)
    (tmp_path / "run_pipeline.py").write_text(
        """from pathlib import Path
target = Path('compiled_skill') / 'demo'
target.mkdir(parents=True, exist_ok=True)
(target / 'SKILL.md').write_text('# Demo skill', encoding='utf-8')
(target / 'EVALUATION.md').write_text('# Demo evaluation', encoding='utf-8')
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    manager = MiningJobManager(tmp_path, max_parallel=1)
    job = manager.create_jobs({
        "name": "failed benchmark build",
        "input_dir": "data/alpha",
        "max_rounds": 1,
        "human_checkpoints": False,
    })[0]

    finished = _wait_finished(manager, [job["job_id"]])[0]

    assert finished["status"] == "succeeded"
    assert finished["artifact_quality"]["level"] == "incomplete"
    assert any("退出码" in warning for warning in finished["artifact_quality"]["warnings"])


def test_waiting_job_exposes_form_checkpoint_and_accepts_answers(tmp_path):
    _prepare_project(tmp_path)
    manager = MiningJobManager(tmp_path, max_parallel=1)
    job = manager.create_jobs({
        "name": "checkpoint mining",
        "input_dir": "data/alpha",
        "max_rounds": 2,
        "human_checkpoints": False,
    })[0]
    finished = _wait_finished(manager, [job["job_id"]])[0]
    internal = manager._jobs[job["job_id"]]
    internal["status"] = "waiting"
    manager._persist(internal)
    checkpoint_dir = manager._job_dir(job["job_id"]) / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    pending = {
        "id": "gap-r1-abc",
        "checkpoint": "on_gap_low_confidence",
        "round": 1,
        "title": "请补充指标",
        "intro": "逐项填写",
        "questions": [{
            "qid": "g1",
            "question": "请问，升级时效指标是多少？",
            "field_label": "指标值（请包含单位与适用条件）",
            "answer_type": "short_text",
        }],
    }
    (checkpoint_dir / "pending.json").write_text(
        json.dumps(pending, ensure_ascii=False),
        encoding="utf-8",
    )

    detail = manager.get_job(job["job_id"])
    assert detail["status"] == "waiting"
    assert detail["pending_checkpoint"]["questions"][0]["question"].startswith("请问")

    resumed = manager.submit_checkpoint_answer(job["job_id"], {
        "question_id": pending["id"],
        "answers": {"g1": "48 小时；普通订单"},
    })
    answer = json.loads((checkpoint_dir / "answer.json").read_text(encoding="utf-8"))
    assert resumed["status"] == "running"
    assert resumed["pending_checkpoint"] is None
    assert answer["answers"] == {"g1": "48 小时；普通订单"}
    assert finished["status"] == "succeeded"


def test_job_detail_persists_semantic_knowledge_gaps_without_pending_checkpoint(tmp_path):
    _prepare_project(tmp_path)
    manager = MiningJobManager(tmp_path, max_parallel=1)
    job = manager.create_jobs({
        "name": "gap history",
        "input_dir": "data/alpha",
        "max_rounds": 1,
        "human_checkpoints": False,
    })[0]
    _wait_finished(manager, [job["job_id"]])

    workspace = manager.project_root / manager._jobs[job["job_id"]]["workspace"]
    reports = workspace / "semantic_reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "report.md").write_text(
        """## [结构化缺口清单]
| 缺口ID | 缺口描述 | 影响结构 | 严重度 | 跨批次 | 来源 |
|---|---|---|---|---|---|
| GAP-01 | 库存差异率计算公式缺失：缺少分子、分母和阈值 | U-08 | 高 | 否 | 库存合同 |
""",
        encoding="utf-8",
    )

    detail = manager.get_job(job["job_id"])
    assert detail["pending_checkpoint"] is None
    assert detail["knowledge_gaps"]["total"] == 1
    assert detail["knowledge_gaps"]["questions"][0]["qid"] == "gap-01"
    assert "分子、分母" in detail["knowledge_gaps"]["questions"][0]["question"]


def test_job_detail_preserves_every_round_of_knowledge_supplementation(tmp_path):
    _prepare_project(tmp_path)
    manager = MiningJobManager(tmp_path, max_parallel=1)
    job = manager.create_jobs({
        "name": "multiple supplement rounds",
        "input_dir": "data/alpha",
        "max_rounds": 3,
        "human_checkpoints": False,
    })[0]
    _wait_finished(manager, [job["job_id"]])
    workspace = manager.project_root / manager._jobs[job["job_id"]]["workspace"]
    reports = """## [结构化缺口清单]
| 缺口ID | 缺口描述 | 影响结构 | 严重度 | 跨批次 | 来源 |
|---|---|---|---|---|---|
| GAP-01 | 第 {round_idx} 轮的指标阈值缺失 | U-{round_idx} | 高 | 否 | 轮次 {round_idx} 资料 |
"""
    for round_idx in (1, 2):
        target = workspace / "reflection_rounds" / "session-a" / f"round_{round_idx}" / "semantic_reports"
        target.mkdir(parents=True, exist_ok=True)
        (target / "report.md").write_text(reports.format(round_idx=round_idx), encoding="utf-8")

    history = manager._job_dir(job["job_id"]) / "checkpoints" / "history"
    history.mkdir(parents=True)
    (history / "after_semantic-r3.json").write_text(json.dumps({
        "checkpoint": "after_semantic",
        "round": 3,
        "title": "第 3 轮关键知识补充",
        "questions": [{"qid": "gap-03", "question": "请填写第 3 轮阈值？"}],
        "answers": {"gap-03": "72 小时"},
        "submitted_at": "2026-08-18T00:00:00Z",
    }, ensure_ascii=False), encoding="utf-8")

    detail = manager.get_job(job["job_id"])

    assert [item["round"] for item in detail["knowledge_supplements"]] == [1, 2, 3]
    assert detail["knowledge_supplements"][2]["answers"] == {"gap-03": "72 小时"}
    assert detail["knowledge_supplements"][0]["source"] == "semantic_report"
