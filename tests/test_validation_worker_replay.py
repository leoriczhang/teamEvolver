from __future__ import annotations

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.validation.worker import ValidationWorker


class _FakeClient:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return "已完成重放任务。"


@pytest.mark.anyio
async def test_replay_branch_uses_replay_result_fields(tmp_path) -> None:
    worker = ValidationWorker(
        TeamEvolverConfig(
            sharing_enabled=True,
            sharing_backend="local",
            sharing_session_backend="local",
            sharing_local_root=str(tmp_path),
            llm_api_key="",
            validation_enabled=True,
        ),
        llm_client=_FakeClient(),
    )

    result = await worker._run_replay_branch(
        {"instruction": "整理一个可复用流程"},
        None,
        label="baseline",
    )

    assert result["label"] == "baseline"
    assert result["replay_score"] == 0.75
    assert result["normalized_score"] == 0.75
    assert not any(key.startswith("pr" + "m_") for key in result)


@pytest.mark.anyio
async def test_replay_validation_rejects_tied_replay_scores(tmp_path) -> None:
    worker = ValidationWorker(
        TeamEvolverConfig(
            sharing_enabled=True,
            sharing_backend="local",
            sharing_session_backend="local",
            sharing_local_root=str(tmp_path),
            llm_api_key="",
            validation_enabled=True,
        ),
        llm_client=_FakeClient(),
    )

    result = await worker._replay_validate_job(
        {
            "candidate_skill": {
                "name": "candidate",
                "description": "candidate skill",
                "content": "Use this procedure.",
            },
            "replay_cases": [{"instruction": "整理一个可复用流程"}],
            "min_score": 0.75,
        }
    )

    assert result["score"] == 0.75
    assert result["accepted"] is False
    assert result["decision"] == "reject"
