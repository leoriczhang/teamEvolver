from __future__ import annotations

import asyncio
from pathlib import Path

import skillgene
import skillgene.trajectory_benchmark as api


def _payload() -> dict:
    return {
        "dataset_name": "skillgen-evolution",
        "target_total": 3,
        "trajectories": [{
            "session_id": "session-001",
            "turns": [{
                "turn_num": 1,
                "prompt_text": "完成任务",
                "response_text": "任务已完成",
                "success": True,
            }],
        }],
    }


def test_public_package_exposes_internal_trajectory_benchmark_api(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_mine(request, **kwargs):  # noqa: ANN001, ANN003
        captured["request"] = request
        captured["kwargs"] = kwargs
        return {"state": "done", "run_id": request["run_id"], "question_count": 3}

    monkeypatch.setattr(api._implementation, "mine_trajectory_benchmark", fake_mine)
    result = skillgene.mine_benchmark_from_trajectories(_payload(), project_root=tmp_path)

    assert result["state"] == "done"
    assert captured["request"]["dataset_name"] == "skillgen-evolution"
    assert captured["request"]["source"]["trajectory_count"] == 1
    assert captured["kwargs"]["project_root"] == tmp_path


def test_async_internal_api_runs_blocking_miner_off_event_loop(tmp_path: Path, monkeypatch) -> None:
    def fake_sync(payload, **kwargs):  # noqa: ANN001, ANN003
        return {"state": "done", "dataset_name": payload["dataset_name"], **kwargs}

    monkeypatch.setattr(api, "mine_benchmark_from_trajectories", fake_sync)
    result = asyncio.run(api.amine_benchmark_from_trajectories(_payload(), project_root=tmp_path))

    assert result["state"] == "done"
    assert result["dataset_name"] == "skillgen-evolution"
    assert result["project_root"] == tmp_path
