from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import teamEvolver.evolve.runtime.orchestrator as orchestrator_module
from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.evidence import SkillEvidenceStore
from teamEvolver.evolve.runtime.orchestrator import EvolveServer
from teamEvolver.storage import LocalObjectStore


def _session(session_id: str, score: float = 0.7) -> dict:
    return {
        "session_id": session_id,
        "timestamp": f"2026-01-{int(session_id[-1]):02d}T00:00:00+00:00",
        "_summary": f"summary {session_id}",
        "_trajectory": f"trajectory {session_id}",
        "_judge_scores": {"overall_score": score},
        "_has_tool_errors": score < 0.6,
        "turns": [
            {
                "turn_num": 1,
                "prompt_text": f"instruction {session_id}",
                "response_text": f"response {session_id}",
            }
        ],
    }


def test_evidence_store_preserves_recent_history_and_change_debt(
    tmp_path: Path,
) -> None:
    store = SkillEvidenceStore(
        LocalObjectStore(tmp_path),
        max_entries=10,
        recent_limit=2,
        historical_limit=2,
        replay_cases_per_window=1,
        change_debt_threshold=2,
    )

    store.record_skip("ppt-generation", [_session("session-1", 0.4)], "weak once")
    state = store.record_skip(
        "ppt-generation",
        [_session("session-2", 0.5)],
        "same issue repeated",
    )
    store.record_sessions("ppt-generation", [_session("session-3", 0.9)])

    assert state["change_debt"]["skip_count"] == 2
    assert state["change_debt"]["reconsideration_ready"] is True

    planning, context = store.build_context(
        "ppt-generation",
        [_session("session-3", 0.9)],
    )
    assert context["total_evidence_sessions"] == 3
    assert context["recent_session_ids"] == ["session-2", "session-3"]
    assert context["historical_session_ids"] == ["session-1"]
    assert {item["session_id"] for item in planning} == {
        "session-1",
        "session-2",
        "session-3",
    }

    windows = store.build_replay_windows("ppt-generation")
    assert windows["recent"][0]["session_id"] == "session-3"
    assert windows["recent"][0]["evidence_window"] == "recent"
    assert windows["historical"][0]["session_id"] == "session-1"
    assert windows["historical"][0]["evidence_window"] == "historical"

    published = store.mark_published("ppt-generation", "job-1")
    assert published["change_debt"]["skip_count"] == 0
    assert published["change_debt"]["pending_session_ids"] == []
    assert published["last_published_job_id"] == "job-1"

    store.record_sessions("ppt-generation", [_session("session-4", 0.8)])
    post_publish_windows = store.build_replay_windows("ppt-generation")
    assert post_publish_windows["recent"][0]["session_id"] == "session-4"
    assert post_publish_windows["historical"][0]["session_id"] in {
        "session-1",
        "session-2",
        "session-3",
    }


def test_validation_candidate_is_coalesced_and_stale_outputs_are_cleared(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
            publish_mode="validated",
        )
    )
    first = server._queue_validation_job(
        {
            "name": "ppt-generation",
            "description": "v1",
            "content": "first candidate",
        },
        DecisionAction.IMPROVE,
        [_session("session-1")],
        "first signal",
        "skill_group",
        evidence_key="ppt-generation",
    )
    job_id = first["validation_job_id"]
    server._validation_store.save_evaluation(job_id, {"score": 0.9})
    server._validation_store.save_result(
        job_id,
        "validator",
        {"accepted": True, "score": 0.9, "candidate_revision": 1},
    )
    legacy = dict(server._validation_store.load_job(job_id) or {})
    legacy["job_id"] = "legacy-duplicate"
    legacy["created_at"] = "2020-01-01T00:00:00+00:00"
    legacy["updated_at"] = "2020-01-01T00:00:00+00:00"
    server._validation_store.save_job(legacy)

    second = server._queue_validation_job(
        {
            "name": "ppt-generation",
            "description": "v2",
            "content": "updated candidate",
        },
        DecisionAction.IMPROVE,
        [_session("session-2")],
        "second signal",
        "skill_group",
        evidence_key="ppt-generation",
    )

    assert second["validation_job_id"] == job_id
    assert second["action"] == "updated_validation_candidate"
    assert second["candidate_revision"] == 2
    active_jobs = server._validation_store.list_open_jobs_for_skill(
        "ppt-generation"
    )
    assert len(active_jobs) == 1
    assert active_jobs[0]["candidate_skill"]["content"] == "updated candidate"
    assert active_jobs[0]["session_ids"] == ["session-1", "session-2"]
    assert active_jobs[0]["checklist"]["format"] == "common_checklist_v2"
    assert active_jobs[0]["checklist"]["source_session_ids"] == [
        "session-1",
        "session-2",
    ]
    superseded = server._validation_store.load_decision("legacy-duplicate")
    assert superseded["status"] == "superseded"
    assert superseded["superseded_by"] == job_id
    assert server._validation_store.load_evaluation(job_id) is None
    assert server._validation_store.load_result(job_id, "validator") is None


@pytest.mark.anyio
async def test_inconclusive_validation_stays_open_for_revision(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
            publish_mode="validated",
            validation_required_results=1,
            validation_required_approvals=1,
            validation_max_rejections=1,
            human_review_enabled=True,
        )
    )
    queued = server._queue_validation_job(
        {
            "name": "ppt-generation",
            "description": "Candidate",
            "content": "Run a deterministic layout gate.",
        },
        DecisionAction.IMPROVE,
        [_session("session-1")],
        "layout still needs proof",
        "skill_group",
        evidence_key="ppt-generation",
    )
    job_id = queued["validation_job_id"]
    server._validation_store.save_result(
        job_id,
        "validator",
        {
            "decision": "inconclusive",
            "accepted": False,
            "score": 0.6,
            "candidate_revision": 1,
        },
    )

    records, summary = await server._finalize_validation_jobs()

    assert server._validation_store.load_decision(job_id) is None
    assert server._validation_store.load_human_review_task(job_id) is not None
    assert summary["rejected"] == 0
    assert summary["inconclusive"] == 1
    assert summary["escalated_to_human"] == 1
    assert records[0]["action"] == "escalated_to_human_review"


@pytest.mark.anyio
async def test_repeated_skip_debt_is_visible_to_next_planner_cycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    contexts: list[dict] = []

    async def fake_evolve(
        _llm,
        _skill_name,
        _sessions,
        _current_skill,
        _existing_skill_names,
        *,
        evolution_context=None,
    ):
        contexts.append(dict(evolution_context or {}))
        return {"action": DecisionAction.SKIP, "rationale": "not enough yet"}

    monkeypatch.setattr(
        orchestrator_module,
        "evolve_skill_from_sessions",
        fake_evolve,
    )
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
            evidence_change_debt_threshold=2,
            use_skill_verifier=False,
            use_skill_dedup=False,
        )
    )

    first = await server._evolve_skill_group(
        "ppt-generation",
        [_session("session-1")],
        [],
    )
    second = await server._evolve_skill_group(
        "ppt-generation",
        [_session("session-2")],
        [],
    )
    await server._evolve_skill_group(
        "ppt-generation",
        [_session("session-3")],
        [],
    )

    assert first["change_debt"]["reconsideration_ready"] is False
    assert second["change_debt"]["reconsideration_ready"] is True
    assert contexts[2]["change_debt"]["reconsideration_ready"] is True
    assert contexts[2]["total_evidence_sessions"] == 3


def test_dual_window_gate_requires_recent_improvement_and_history_stability() -> None:
    checklist = {
        "commonality": {"passed": True},
        "items": [
            {
                "id": "execution_complete",
                "kind": "hard",
                "required": True,
            },
            {
                "id": "new_requirement",
                "kind": "soft",
                "required": True,
                "scope": "source_sessions",
                "source_session_ids": ["new"],
            },
            {
                "id": "old_requirement",
                "kind": "soft",
                "required": True,
                "scope": "source_sessions",
                "source_session_ids": ["old"],
            },
        ],
        "merge_context": {
            "checklist_sources": [
                {
                    "skill_name": "candidate_evidence",
                    "required_item_ids": ["new_requirement"],
                },
                {
                    "skill_name": "existing",
                    "version": 2,
                    "inherited": True,
                    "required_item_ids": ["old_requirement"],
                },
            ]
        },
    }

    def branch_result(*item_ids: str) -> dict:
        return {
            "passed": True,
            "hard_pass": True,
            "pass_rate": 1.0,
            "items": [
                {
                    "id": item_id,
                    "kind": "hard"
                    if item_id == "execution_complete"
                    else "soft",
                    "required": True,
                    "passed": True,
                }
                for item_id in item_ids
            ],
        }

    recent = {
        "status": "evaluated",
        "accepted": True,
        "no_regression": True,
        "quality_ok": True,
        "case_count": 1,
        "cases": [],
        "checklist_results": {
            "baseline": {
                **branch_result("execution_complete", "new_requirement"),
                "passed": False,
                "pass_rate": 0.5,
                "items": [
                    {
                        "id": "execution_complete",
                        "kind": "hard",
                        "required": True,
                        "passed": True,
                    },
                    {
                        "id": "new_requirement",
                        "kind": "soft",
                        "required": True,
                        "passed": False,
                    },
                ],
            },
            "candidate": branch_result(
                "execution_complete",
                "new_requirement",
            ),
        },
        "efficiency": {
            "baseline": {
                "interaction_turns": 4,
                "tool_call_count": 8,
                "total_tokens": 1000,
            },
            "candidate": {
                "interaction_turns": 2,
                "tool_call_count": 6,
                "total_tokens": 800,
            },
        },
    }
    historical = {
        "status": "evaluated",
        "accepted": False,
        "no_regression": True,
        "quality_ok": True,
        "case_count": 1,
        "cases": [],
        "checklist_results": {
            "baseline": branch_result(
                "execution_complete",
                "old_requirement",
            ),
            "candidate": branch_result(
                "execution_complete",
                "old_requirement",
            ),
        },
        "efficiency": {
            "baseline": {
                "interaction_turns": 2,
                "tool_call_count": 4,
                "total_tokens": 500,
            },
            "candidate": {
                "interaction_turns": 2,
                "tool_call_count": 4,
                "total_tokens": 500,
            },
        },
    }
    accepted = EvolveServer._aggregate_replay_windows(
        [("recent", recent), ("historical", historical)],
        checklist=checklist,
        threshold=0.75,
        tolerance=0.15,
        max_interactions=4,
    )
    assert accepted["accepted"] is True
    assert accepted["recent_improved"] is True
    assert accepted["historical_no_regression"] is True
    assert accepted["decision_policy"]["merge_union_pass"] is True

    regressed_historical = {
        **historical,
        "no_regression": False,
        "checklist_results": {
            **historical["checklist_results"],
            "candidate": {
                **historical["checklist_results"]["candidate"],
                "passed": False,
                "pass_rate": 0.5,
                "items": [
                    {
                        "id": "execution_complete",
                        "kind": "hard",
                        "required": True,
                        "passed": True,
                    },
                    {
                        "id": "old_requirement",
                        "kind": "soft",
                        "required": True,
                        "passed": False,
                    },
                ],
            },
        },
    }
    rejected = EvolveServer._aggregate_replay_windows(
        [("recent", recent), ("historical", regressed_historical)],
        checklist=checklist,
        threshold=0.75,
        tolerance=0.15,
        max_interactions=4,
    )
    assert rejected["accepted"] is False
    assert rejected["historical_no_regression"] is False


def test_team_config_maps_cross_cycle_evidence_settings(monkeypatch, tmp_path) -> None:
    for name in (
        "EVOLVE_EVIDENCE_ENABLED",
        "EVOLVE_EVIDENCE_MAX_ENTRIES",
        "EVOLVE_EVIDENCE_RECENT_LIMIT",
        "EVOLVE_EVIDENCE_HISTORICAL_LIMIT",
        "EVOLVE_EVIDENCE_REPLAY_CASES_PER_WINDOW",
        "EVOLVE_EVIDENCE_CHANGE_DEBT_THRESHOLD",
        "EVOLVE_CANDIDATE_COALESCE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    source = SimpleNamespace(
        sharing_backend="local",
        sharing_local_root=str(tmp_path),
        sharing_viking_endpoint="",
        sharing_viking_api_key="",
        sharing_viking_team_api_key="",
        sharing_viking_account="default",
        sharing_viking_user="default",
        sharing_viking_agent="team-skill-evolver",
        sharing_viking_agent_id="",
        sharing_viking_customer_id="",
        sharing_viking_root_prefix="team-skill-evolver",
        sharing_viking_group_id="",
        llm_api_key="",
        prm_api_key="",
        llm_api_base="",
        prm_url="",
        llm_model_id="model",
        llm_max_tokens=100_000,
        llm_temperature=0.4,
        proxy_api_key="",
        evolve_evidence_enabled=True,
        evolve_evidence_max_entries=50,
        evolve_evidence_recent_limit=5,
        evolve_evidence_historical_limit=7,
        evolve_evidence_replay_cases_per_window=2,
        evolve_evidence_change_debt_threshold=4,
        evolve_candidate_coalesce_enabled=False,
    )

    config = EvolveServerConfig.from_teamEvolver_config(source)

    assert config.evidence_max_entries == 50
    assert config.evidence_recent_limit == 5
    assert config.evidence_historical_limit == 7
    assert config.evidence_replay_cases_per_window == 2
    assert config.evidence_change_debt_threshold == 4
    assert config.candidate_coalesce_enabled is False
