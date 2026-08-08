from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILLMINER_ROOT = Path(__file__).resolve().parents[1] / "skillgene" / "skillminer"
if str(SKILLMINER_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLMINER_ROOT))

import trajectory_benchmark as tb  # noqa: E402
from web_console import server  # noqa: E402


def test_normalize_trajectories_accepts_skillgene_and_message_shapes_and_redacts() -> None:
    evidence, source = tb.normalize_trajectories([
        {
            "session_id": "person@example.com",
            "turns": [{
                "turn_num": 2,
                "prompt_text": "联系 13812345678 并处理 /Users/alice/private.txt",
                "response_text": "已完成，api_key=super-secret-value",
                "tool_calls": [{
                    "function": {"name": "read", "arguments": '{"path":"/Users/alice/private.txt"}'},
                }],
                "tool_results": [{"tool_name": "read", "content": "ok"}],
            }],
        },
        {
            "session_id": "session-2",
            "status": "completed",
            "success": True,
            "value_judge": {
                "decision": "valuable",
                "confidence": 0.91,
                "reason": "contains a reusable workflow",
            },
            "messages": [
                {"role": "user", "content": "整理这份数据"},
                {"role": "assistant", "content": "已完成", "tool_calls": []},
            ],
        },
    ])

    serialized = json.dumps(evidence, ensure_ascii=False)
    assert len(evidence) == 2
    assert source["trajectory_count"] == 2
    assert source["evidence_turn_count"] == 2
    assert "person@example.com" not in serialized
    assert "13812345678" not in serialized
    assert "/Users/alice" not in serialized
    assert "super-secret-value" not in serialized
    assert "[REDACTED_EMAIL]" in evidence[0]["source"]
    assert evidence[1]["outcome"]["success"] is True
    assert evidence[1]["outcome"]["status"] == "completed"
    assert evidence[1]["outcome"]["session_value_judge"]["decision"] == "valuable"
    assert tb.redact_sensitive_text(r"C:\Users\alice\private.txt") == (
        r"C:\Users\[REDACTED_USER]\private.txt"
    )


def test_normalize_trajectories_accepts_root_task_with_action_steps() -> None:
    evidence, _source = tb.normalize_trajectories([{
        "trajectory_id": "run-1",
        "task": "生成季度分析报告",
        "final_answer": "报告已生成",
        "success": True,
        "steps": [
            {"action": {"name": "read", "arguments": {"path": "input.csv"}}, "observation": "3 rows"},
            {"action": "write", "arguments": {"path": "report.md"}, "observation": "saved"},
        ],
    }])

    assert len(evidence) == 1
    assert evidence[0]["instruction"] == "生成季度分析报告"
    assert evidence[0]["reference_response"] == "报告已生成"
    assert [item["name"] for item in evidence[0]["tool_trace"] if item["kind"] == "call"] == ["read", "write"]


def test_normalize_request_validates_limits_and_difficulty_distribution() -> None:
    request = tb.normalize_request({
        "dataset_name": "skillgen-evolution",
        "target_total": 10,
        "difficulty_dist": "easy:2,medium:5,hard:3",
        "trajectories": [{"input": "处理任务", "output": "完成"}],
    })

    assert request["dataset_name"] == "skillgen-evolution"
    assert request["target_total"] == 10
    assert sum(request["difficulty_counts"].values()) == 10
    assert request["run_id"].endswith("skillgen-evolution-" + request["run_id"].rsplit("-", 1)[-1])

    with pytest.raises(tb.TrajectoryBenchmarkError):
        tb.normalize_request({
            "dataset_name": "../escape",
            "trajectories": [{"input": "处理任务", "output": "完成"}],
        })
    with pytest.raises(tb.TrajectoryBenchmarkError):
        tb.normalize_request({"dataset_name": "empty", "trajectories": []})


def test_generated_questions_are_strictly_normalized_and_persisted(tmp_path: Path) -> None:
    questions = tb.normalize_generated_questions([
        {
            "id": "anything",
            "target_dimensions": ["工具调用正确性"],
            "difficulty": "unknown",
            "input": "为 user@example.com 生成报告",
            "gold": {"must_hit": ["先校验数据"], "must_avoid": ["编造结果"]},
            "source": "trajectory:run-1:turn:1",
        },
        {"input": "没有评分锚点", "gold": {}},
    ], target_total=5)

    assert len(questions) == 1
    assert questions[0]["id"] == "TB-001"
    assert questions[0]["difficulty"] == "medium"
    assert questions[0]["dataset_format"] == tb.DATASET_FORMAT
    assert "user@example.com" not in questions[0]["input"]

    source_checked = tb.normalize_generated_questions([
        {
            "input": "有效来源",
            "gold": {"must_hit": ["完成任务"]},
            "source": "trajectory:run-1:turn:1,trajectory:invented:turn:9",
        },
        {
            "input": "伪造来源",
            "gold": {"must_hit": ["完成任务"]},
            "source": "trajectory:invented:turn:9",
        },
    ], target_total=5, allowed_sources={"trajectory:run-1:turn:1"})
    assert len(source_checked) == 1
    assert source_checked[0]["source"] == "trajectory:run-1:turn:1"

    request = {
        "run_id": "20260806T000000000000Z-demo-12345678",
        "dataset_name": "demo",
        "target_total": 5,
        "source": {
            "trajectory_count": 1,
            "evidence_turn_count": 1,
            "source_ids": ["run-1"],
            "source_sha256": "abc",
        },
    }
    run_dir = tmp_path / "trajectory_benchmarks" / request["run_id"]
    run_dir.mkdir(parents=True)
    manifest = tb._write_artifacts(run_dir, request, questions)
    loaded = tb.get_run(request["run_id"], project_root=tmp_path)

    assert manifest["question_count"] == 1
    assert loaded is not None
    assert loaded["questions"] == questions
    assert (run_dir / "BENCHMARK.md").is_file()


def test_run_manager_trajectory_job_does_not_enter_skill_pipeline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    called: dict[str, object] = {}

    def fake_mine(request, **kwargs):  # noqa: ANN001, ANN003
        called["request"] = request
        called["kwargs"] = kwargs
        return {
            "run_id": request["run_id"],
            "dataset_name": request["dataset_name"],
            "dataset_format": tb.DATASET_FORMAT,
            "origin": tb.ORIGIN,
            "state": "done",
            "question_count": 3,
            "target_total": request["target_total"],
            "difficulty_counts": {"easy": 1, "medium": 1, "hard": 1},
            "dimensions": ["任务完成性"],
            **request["source"],
            "artifact_dir": str(tmp_path / "trajectory_benchmarks" / request["run_id"]),
        }

    monkeypatch.setattr(server.tb, "mine_trajectory_benchmark", fake_mine)
    manager = server.RunManager()
    ok, message, run = manager.start_trajectory_benchmark({
        "dataset_name": "evolution-only",
        "target_total": 3,
        "trajectories": [{"input": "完成任务", "output": "任务完成"}],
    })
    assert ok is True
    assert message == "started"
    assert run is not None
    manager.thread.join(timeout=5)

    assert manager.state == "done"
    assert manager.task_kind == "trajectory_benchmark"
    assert manager.last_result["question_count"] == 3
    assert called["request"]["dataset_name"] == "evolution-only"
    assert not (tmp_path / "sample_packages").exists()
    assert not (tmp_path / "semantic_reports").exists()
    assert not (tmp_path / "compiled_skill").exists()
