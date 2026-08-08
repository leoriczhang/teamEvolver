from __future__ import annotations

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.session_store import SessionStore
from teamEvolver.skills.hub import SkillHub
from teamEvolver.validation.worker import ValidationWorker


class _FakeClient:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return "已完成重放任务。"


def test_true_replay_window_aggregation_enforces_merge_union() -> None:
    checklist = {
        "commonality": {"passed": True},
        "items": [
            {"id": "base", "kind": "hard", "required": True},
            {"id": "new", "kind": "soft", "required": True},
            {"id": "old", "kind": "soft", "required": True},
        ],
        "merge_context": {
            "checklist_sources": [
                {
                    "skill_name": "candidate_evidence",
                    "required_item_ids": ["new"],
                },
                {
                    "skill_name": "existing",
                    "version": 4,
                    "inherited": True,
                    "required_item_ids": ["old"],
                },
            ]
        },
    }

    def result(
        baseline_items,
        candidate_items,
        baseline_turns,
        candidate_turns,
    ):
        return {
            "status": "evaluated",
            "accepted": True,
            "no_regression": True,
            "case_count": 1,
            "cases": [],
            "checklist_results": {
                "baseline": {"items": baseline_items},
                "candidate": {"items": candidate_items},
            },
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
                    [
                        {"id": "base", "kind": "hard", "passed": True},
                        {"id": "new", "kind": "soft", "passed": False},
                    ],
                    [
                        {"id": "base", "kind": "hard", "passed": True},
                        {"id": "new", "kind": "soft", "passed": True},
                    ],
                    4,
                    2,
                ),
            ),
            (
                "historical",
                result(
                    [
                        {"id": "base", "kind": "hard", "passed": True},
                        {"id": "old", "kind": "soft", "passed": True},
                    ],
                    [
                        {"id": "base", "kind": "hard", "passed": True},
                        {"id": "old", "kind": "soft", "passed": True},
                    ],
                    2,
                    2,
                ),
            ),
        ],
        checklist=checklist,
    )

    assert replay["accepted"] is True
    assert replay["score"] == 1.0
    assert replay["baseline_mean"] == 0.6667
    assert replay["decision_policy"]["merge_union_pass"] is True
    assert replay["decision_policy"]["merge_source_results"][1]["passed"] is True


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


@pytest.mark.anyio
async def test_replay_validation_requires_history_not_to_regress(tmp_path) -> None:
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

    async def fake_branch(case, _skill, *, label):  # noqa: ANN001
        scores = {
            ("recent", "baseline"): 0.5,
            ("recent", "candidate"): 0.9,
            ("historical", "baseline"): 0.9,
            ("historical", "candidate"): 0.5,
        }
        return {
            "label": label,
            "normalized_score": scores[(case["evidence_window"], label)],
        }

    worker._run_replay_branch = fake_branch  # type: ignore[method-assign]
    result = await worker._replay_validate_job(
        {
            "candidate_skill": {
                "name": "candidate",
                "description": "candidate skill",
                "content": "Use this procedure.",
            },
            "replay_cases": [
                {"instruction": "new behavior", "evidence_window": "recent"},
                {"instruction": "old behavior", "evidence_window": "historical"},
            ],
            "min_score": 0.75,
        }
    )

    assert result["accepted"] is False
    assert result["replay_summary"]["recent_improved"] is True
    assert result["replay_summary"]["historical_no_regression"] is False


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
        llm_client=_FakeClient(),
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
        return {"accepted": True, "score": 0.9}

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
    worker = ValidationWorker(config, llm_client=_FakeClient())

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
    worker = ValidationWorker(config, llm_client=_FakeClient())
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
