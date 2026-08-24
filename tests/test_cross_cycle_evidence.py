from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import teamEvolver.evolve.runtime.orchestrator as orchestrator_module
from teamEvolver.dataset_store import SkillDatasetStore
from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.evidence import SkillEvidenceStore
from teamEvolver.evolve.runtime.orchestrator import EvolveServer
from teamEvolver.proxy.routes import _is_embedded_evolve_path
from teamEvolver.storage import InMemoryObjectStore


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


def test_validation_candidate_api_routes_to_embedded_evolver() -> None:
    assert _is_embedded_evolve_path("/validation/candidates") is True
    assert (
        _is_embedded_evolve_path("/validation/candidates/job-1")
        is True
    )


def test_evidence_store_preserves_recent_history_and_change_debt(
    tmp_path: Path,
) -> None:
    store = SkillEvidenceStore(
        InMemoryObjectStore(str(tmp_path)),
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
            llm_api_key="test-key",
            publish_mode="validated",
        ),
        mock=True,
        mock_root=str(tmp_path),
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
    assert "checklist" not in active_jobs[0]
    superseded = server._validation_store.load_decision("legacy-duplicate")
    assert superseded["status"] == "superseded"
    assert superseded["superseded_by"] == job_id
    assert server._validation_store.load_evaluation(job_id) is None
    assert server._validation_store.load_result(job_id, "validator") is None


def test_validation_job_uses_synthesized_test_dataset_as_replay_contract(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="test-key",
            publish_mode="validated",
        ),
        mock=True,
        mock_root=str(tmp_path),
    )
    dataset = {
        "dataset_id": "synth-case-1",
        "dataset_format": "teamEvolver-progressive-test-v1",
        "name": "Progressive test",
        "query": "完成测试任务。",
        "requirements": ["输出报告", "标注来源"],
        "trajectory_requirements": ["读取输入材料"],
        "checklist": [
            {"id": "R01", "text": "输出报告", "kind": "output"},
            {"id": "R02", "text": "标注来源", "kind": "output"},
            {"id": "T01", "text": "读取输入材料", "kind": "trajectory"},
        ],
        "source_session_ids": ["session-1"],
        "evidence_window": "recent",
        "progressive_disclosure": {
            "enabled": True,
            "initial_visibility": "query_only",
            "batch_size": 2,
        },
    }

    queued = server._queue_validation_job(
        {
            "name": "ppt-generation",
            "description": "Candidate",
            "content": "Use the shared SOP.",
        },
        DecisionAction.IMPROVE,
        [_session("session-1")],
        "shared SOP evidence",
        "skill_group",
        evidence_key="ppt-generation",
        test_datasets=[dataset],
    )

    job = server._validation_store.load_job(queued["validation_job_id"])
    assert job is not None
    assert queued["test_dataset_count"] == 1
    assert job["test_datasets"][0]["dataset_id"] == "synth-case-1"
    assert job["replay_cases"][0]["instruction"] == "完成测试任务。"
    assert "输出报告" not in job["replay_cases"][0]["instruction"]
    assert job["replay_cases"][0]["checklist"][0]["id"] == "R01"
    assert job["max_interactions"] == 3
    stored = server._skill_bucket.get_object(
        "evolution_datasets/"
        "6a8ce311cca2/"
        f"{queued['validation_job_id']}.json"
    ).read()
    assert b"synth-case-1" in stored


@pytest.mark.anyio
async def test_fixed_skill_dataset_is_reused_by_evolution_replay(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="test-key",
            publish_mode="validated",
            dataset_synthesis_enabled=False,
            dataset_test_cases=2,
        ),
        mock=True,
        mock_root=str(tmp_path),
    )
    SkillDatasetStore(server._skill_bucket).save_dataset(
        {
            "dataset_id": "regression-1",
            "skill_name": "ppt-generation",
            "name": "Pinned regression",
            "query": "Build the requested deck.",
            "requirements": "1. Save the PPTX\n2. Verify slide count",
            "trajectory_requirements": "1. Inspect the generated artifact",
            "progressive_disclosure": {
                "enabled": True,
                "initial_visibility": "query_only",
                "batch_size": 2,
            },
            "source": {
                "kind": "manual",
                "source_session_ids": ["session-1"],
                "evidence_window": "historical",
            },
            "read_only": False,
            "enabled_for_evolution": True,
        }
    )

    datasets = await server._synthesize_candidate_datasets(
        skill_name="ppt-generation",
        sessions=[_session("session-1")],
        candidate_skill={
            "name": "ppt-generation",
            "description": "Build decks",
            "content": "Build and verify.",
        },
        evidence_classification={},
        evolution_context={},
        replay_windows={"recent": [], "historical": []},
    )
    queued = server._queue_validation_job(
        {
            "name": "ppt-generation",
            "description": "Build decks",
            "content": "Build and verify.",
        },
        DecisionAction.IMPROVE,
        [_session("session-1")],
        "fixed regression",
        "skill_group",
        evidence_key="ppt-generation",
        test_datasets=datasets,
    )

    job = server._validation_store.load_job(queued["validation_job_id"])
    assert [item["dataset_id"] for item in datasets] == ["regression-1"]
    assert job is not None
    assert job["test_datasets"][0]["dataset_id"] == "regression-1"
    assert job["replay_cases"][0]["instruction"] == "Build the requested deck."
    assert job["replay_cases"][0]["checklist"][0]["text"] == "Save the PPTX"


@pytest.mark.anyio
async def test_inconclusive_validation_stays_open_for_revision(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="test-key",
            publish_mode="validated",
            validation_required_results=1,
            validation_required_approvals=1,
            validation_max_rejections=1,
            human_review_enabled=True,
        ),
        mock=True,
        mock_root=str(tmp_path),
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
    server._validation_store.save_evaluation(
        job_id,
        {
            "decision": "inconclusive",
            "score": 0.5,
            "candidate_revision": 1,
            "evaluated_at": "2026-08-10T10:00:00+00:00",
        },
    )
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


def test_candidate_read_api_projects_automatic_validator_result(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="test-key",
            publish_mode="validated",
        ),
        mock=True,
        mock_root=str(tmp_path),
    )
    queued = server._queue_validation_job(
        {
            "name": "html-ppt-methodology",
            "description": "Candidate",
            "content": "Validate after every write.",
        },
        DecisionAction.IMPROVE,
        [_session("session-1")],
        "post-write validation",
        "skill_group",
        evidence_key="html-ppt-methodology",
    )
    job_id = queued["validation_job_id"]
    server._validation_store.save_result(
        job_id,
        "validator",
        {
            "decision": "inconclusive",
            "accepted": False,
            "candidate_revision": 1,
            "created_at": "2026-08-10T11:00:00+00:00",
            "replay_summary": {
                "verdict": "inconclusive",
                "case_count": 1,
                "cases": [{"baseline": {}, "candidate": {}}],
                "efficiency": {
                    "dimensions": {
                        "interaction_turns": {
                            "baseline": 1,
                            "candidate": 1,
                            "delta": 0,
                            "winner": "tie",
                        }
                    }
                },
            },
        },
    )

    rows = server._list_validation_candidates("all")

    row = next(item for item in rows if item["job_id"] == job_id)
    assert row["review_status"] == "inconclusive"
    assert row["replay_verdict"] == "inconclusive"
    assert row["efficiency"]["dimensions"]["interaction_turns"]["delta"] == 0
    assert row["evaluation"]["replay"]["case_count"] == 1


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
            llm_api_key="test-key",
            evidence_change_debt_threshold=2,
        ),
        mock=True,
        mock_root=str(tmp_path),
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


@pytest.mark.anyio
async def test_run_once_evolves_skill_groups_concurrently(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Independent skill groups + the no-skill branch run in parallel and each
    produces its own candidate without clobbering the shared registry."""
    import asyncio

    active = 0
    peak = 0

    async def _observe_concurrency() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
        finally:
            active -= 1

    async def fake_evolve(
        _llm,
        skill_name,
        _sessions,
        _current_skill,
        _existing_skill_names,
        *,
        evolution_context=None,
    ):
        await _observe_concurrency()
        return {
            "action": DecisionAction.IMPROVE,
            "rationale": f"improve {skill_name}",
            "skill": {
                "name": skill_name,
                "description": f"desc {skill_name}",
                "content": f"# {skill_name}\n\nupdated body\n",
                "category": "general",
            },
            "evidence_classification": {
                "team_skill": [
                    {
                        "claim": f"reusable rule for {skill_name}",
                        "supporting_session_ids": ["s"],
                        "causal_link": "observed",
                    }
                ],
                "user_memory": [],
                "task_requirement": [],
                "agent_runtime": [],
                "insufficient_evidence": [],
            },
        }

    async def fake_create(
        _llm,
        _sessions,
        _existing_skill_names,
        *,
        evolution_context=None,
    ):
        await _observe_concurrency()
        return {
            "action": DecisionAction.CREATE,
            "rationale": "new pattern",
            "skill": {
                "name": "fresh-skill",
                "description": "brand new",
                "content": "# fresh-skill\n\nnew body\n",
                "category": "general",
            },
            "evidence_classification": {
                "team_skill": [
                    {
                        "claim": "reusable new rule",
                        "supporting_session_ids": ["s"],
                        "causal_link": "observed",
                    }
                ],
                "user_memory": [],
                "task_requirement": [],
                "agent_runtime": [],
                "insufficient_evidence": [],
            },
        }

    monkeypatch.setattr(orchestrator_module, "evolve_skill_from_sessions", fake_evolve)
    monkeypatch.setattr(orchestrator_module, "create_skill_from_sessions", fake_create)

    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="test-key",
            publish_mode="immediate",
            dataset_synthesis_enabled=False,
            bundle_static_checks_enabled=False,
            use_session_judge=False,
            max_parallel_groups=4,
        ),
        mock=True,
        mock_root=str(tmp_path),
    )
    # Two skill groups (explicit references) + one no-skill session. Sessions
    # live under the ``sessions/`` prefix the drain step scans.
    for idx, skill in enumerate(("alpha-skill", "beta-skill")):
        session = _session(f"has-skill-{idx}")
        session["turns"][0]["read_skills"] = [{"skill_name": skill}]
        server._bucket.put_object(
            f"sessions/sess-{idx}.json",
            json.dumps(session).encode("utf-8"),
        )
    server._bucket.put_object(
        "sessions/sess-noskill.json",
        json.dumps(_session("no-skill-1")).encode("utf-8"),
    )

    summary = await server._run_once()

    evolved_names = {
        record.get("skill_name")
        for record in summary["evolutions"]
        if record.get("uploaded")
    }
    assert {"alpha-skill", "beta-skill", "fresh-skill"} <= evolved_names
    # All branches were in flight together (proves real parallelism).
    assert peak >= 2
    # Registry recorded every skill — no clobbering under fan-out.
    for name in ("alpha-skill", "beta-skill", "fresh-skill"):
        assert server._id_registry.get_version(name) >= 1


def test_dual_window_gate_aggregates_metrics_across_windows() -> None:
    recent = {
        "status": "evaluated",
        "accepted": True,
        "no_regression": True,
        "case_count": 1,
        "cases": [],
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
        "case_count": 1,
        "cases": [],
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
        max_interactions=4,
    )
    assert accepted["accepted"] is True
    assert accepted["verdict"] == "accept"
    assert accepted["decision_policy"]["improved_metrics"] == [
        "interaction_turns",
        "tool_call_count",
        "total_tokens",
    ]

    regressed_historical = {
        **historical,
        "efficiency": {
            **historical["efficiency"],
            "candidate": {
                **historical["efficiency"]["candidate"],
                "tool_call_count": 7,
            },
        },
    }
    still_accepted = EvolveServer._aggregate_replay_windows(
        [("recent", recent), ("historical", regressed_historical)],
        max_interactions=4,
    )
    assert still_accepted["accepted"] is True
    assert still_accepted["verdict"] == "accept"
    assert still_accepted["decision_policy"]["decision_basis"] == (
        "interaction_turns_decreased"
    )
    assert still_accepted["decision_policy"]["regressed_metrics"] == [
        "tool_call_count",
    ]


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
        sharing_backend="viking",
        sharing_viking_deployment="cloud",
        sharing_viking_endpoint="https://viking.example/openviking",
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
        evolve_max_parallel_groups=6,
    )

    config = EvolveServerConfig.from_teamEvolver_config(source)

    assert config.evidence_max_entries == 50
    assert config.evidence_recent_limit == 5
    assert config.evidence_historical_limit == 7
    assert config.evidence_replay_cases_per_window == 2
    assert config.evidence_change_debt_threshold == 4
    assert config.candidate_coalesce_enabled is False
    assert config.max_parallel_groups == 6
