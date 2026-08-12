from __future__ import annotations

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.session_store import SessionStore
from teamEvolver.skills.hub import SkillHub
from teamEvolver.validation.worker import ValidationWorker


def test_true_replay_window_aggregation_uses_metrics_only() -> None:
    def result(
        baseline_turns,
        candidate_turns,
    ):
        return {
            "status": "evaluated",
            "accepted": True,
            "no_regression": True,
            "case_count": 1,
            "cases": [],
            "efficiency": {
                "baseline": {
                    "interaction_turns": baseline_turns,
                    "tool_call_count": 4,
                    "total_tokens": 400,
                },
                "candidate": {
                    "interaction_turns": candidate_turns,
                    "tool_call_count": 4,
                    "total_tokens": 400,
                },
            },
        }

    replay = ValidationWorker._aggregate_true_replay_windows(
        [
            (
                "recent",
                result(
                    4,
                    2,
                ),
            ),
            (
                "historical",
                result(
                    2,
                    2,
                ),
            ),
        ],
    )

    assert replay["accepted"] is True
    assert replay["verdict"] == "accept"
    assert replay["efficiency"]["dimensions"]["interaction_turns"] == {
        "baseline": 6,
        "candidate": 4,
        "delta": 2,
        "reduction_ratio": 0.3333,
        "winner": "candidate",
    }


@pytest.mark.anyio
async def test_validation_worker_discards_result_for_stale_candidate_revision(
    tmp_path,
) -> None:
    worker = ValidationWorker(
        TeamEvolverConfig(
            sharing_enabled=True,
            sharing_backend="local",
            sharing_session_backend="local",
            sharing_local_root=str(tmp_path),
            llm_api_key="",
            validation_enabled=True,
        ),
    )
    job = {
        "job_id": "job-1",
        "candidate_revision": 1,
        "candidate_skill": {
            "name": "candidate",
            "description": "candidate skill",
            "content": "Use this procedure.",
        },
        "replay_cases": [{"instruction": "do work"}],
    }
    worker._store.save_job(job)

    async def fake_validate(current):  # noqa: ANN001
        updated = dict(current)
        updated["candidate_revision"] = 2
        worker._store.save_job(updated)
        return {"accepted": True, "decision": "accept"}

    worker._validate_job = fake_validate  # type: ignore[method-assign]
    summary = await worker.run_once(force=True)

    assert summary["validated_jobs"] == 0
    assert summary["skipped_jobs"] == 1
    assert worker._store.load_result("job-1", worker._user_alias) is None


def test_source_tenant_ids_come_from_agentshub_session(tmp_path) -> None:
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="local",
        sharing_session_backend="local",
        sharing_local_root=str(tmp_path),
        llm_api_key="",
        validation_enabled=True,
    )
    SessionStore.from_config(config).save_queued(
        {
            "session_id": "agentshub-session",
            "turns": [{"prompt_text": "do work"}],
            "runtime": {"type": "agentshub"},
            "runtime_context": {"tenant_id": "tenant_demo"},
        }
    )
    worker = ValidationWorker(config)

    assert worker._source_tenant_ids(
        {"session_ids": ["agentshub-session", "missing"]}
    ) == ["tenant_demo"]


@pytest.mark.anyio
async def test_wait_for_published_commit_returns_manifest_version_and_sha(
    tmp_path,
) -> None:
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="local",
        sharing_session_backend="local",
        sharing_local_root=str(tmp_path / "store"),
        llm_api_key="",
        validation_enabled=True,
    )
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "candidate"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: candidate\n"
        "description: committed candidate\n"
        "---\n\n"
        "# Procedure\n\nDo the verified work.\n",
        encoding="utf-8",
    )
    SkillHub.team_from_config(config).push_skills(str(skills_dir))
    worker = ValidationWorker(config)
    worker._store.save_job(
        {
            "job_id": "job-published",
            "candidate_skill": {"name": "candidate"},
        }
    )
    worker._store.save_decision(
        "job-published",
        {"status": "published"},
    )

    expected = await worker._wait_for_published_commit(
        {
            "job_id": "job-published",
            "candidate_skill": {"name": "candidate"},
        },
        timeout_seconds=1,
    )

    assert expected is not None
    assert expected["name"] == "candidate"
    assert expected["version"] == 1
    assert len(expected["sha256"]) == 64
