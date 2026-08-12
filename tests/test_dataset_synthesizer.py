from __future__ import annotations

import json

import pytest

from teamEvolver.dataset_synthesizer import (
    DATASET_FORMAT,
    SynthesizedDatasetStore,
    checklist_items,
    dataset_to_replay_case,
    flatten_requirements,
    synthesize_evolution_datasets,
)
from teamEvolver.progressive_replay import (
    next_disclosure_prompt,
    progressive_replay_decision,
    select_replay_cases,
)
from teamEvolver.storage import LocalObjectStore
from teamEvolver.true_replay import compare_efficiency


def test_flatten_requirements_and_checklist_ids_are_stable() -> None:
    requirements = flatten_requirements(
        ["1. 输出报告", "- 不得编造\n  2. 标注来源", "输出报告"]
    )
    checklist = checklist_items(requirements, ["1. 读取材料"])

    assert requirements == ["输出报告", "不得编造", "标注来源"]
    assert [(item["id"], item["kind"]) for item in checklist] == [
        ("R01", "output"),
        ("R02", "output"),
        ("R03", "output"),
        ("T01", "trajectory"),
    ]


@pytest.mark.anyio
async def test_synthesizer_uses_sessions_evidence_and_candidate_skill() -> None:
    captured = {}

    class FakeLLM:
        async def chat(self, messages, **kwargs):  # noqa: ANN001
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return json.dumps(
                {
                    "test_datasets": [
                        {
                            "name": "组合规划测试",
                            "query": "请基于材料给出组合规划。",
                            "requirements": [
                                f"要求 {index}" for index in range(1, 13)
                            ],
                            "trajectory_requirements": [
                                "读取输入材料",
                                "校验最终产物",
                            ],
                            "source_session_ids": ["session-1"],
                            "evidence_window": "recent",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    datasets = await synthesize_evolution_datasets(
        FakeLLM(),
        skill_name="portfolio-planning",
        sessions=[
            {
                "session_id": "session-1",
                "_summary": "The shared SOP requires deterministic validation.",
                "_trajectory": "read -> calculate -> write -> validate",
                "turns": [{"prompt_text": "历史用户任务"}],
            }
        ],
        candidate_skill={
            "description": "Portfolio workflow",
            "content": "Validate after writing.",
            "_evidence_classification": {
                "team_skill": [{"claim": "写入后校验最终产物"}]
            },
        },
        evidence_context={"total_evidence_sessions": 8},
        replay_windows={"recent": [], "historical": []},
        case_count=1,
    )

    assert len(datasets) == 1
    dataset = datasets[0]
    assert dataset["dataset_format"] == DATASET_FORMAT
    assert dataset["split"] == "test"
    assert dataset["requirement_count"] == 12
    assert dataset["progressive_disclosure"]["initial_visibility"] == "query_only"
    assert dataset["checklist"][0]["id"] == "R01"
    user_payload = json.loads(captured["messages"][1]["content"])
    assert user_payload["sessions"][0]["session_id"] == "session-1"
    assert user_payload["team_sop_evidence"]["context"]["total_evidence_sessions"] == 8
    assert "Validate after writing." in user_payload["candidate_skill"]["content"]


@pytest.mark.anyio
async def test_synthesizer_fallback_does_not_invent_ungrounded_checklist() -> None:
    class FailedLLM:
        async def chat(self, *_args, **_kwargs):
            raise RuntimeError("offline")

    datasets = await synthesize_evolution_datasets(
        FailedLLM(),
        skill_name="demo-skill",
        sessions=[
            {
                "session_id": "session-1",
                "turns": [
                    {
                        "prompt_text": (
                            "### query\n\n执行任务\n\n### 要求\n\n"
                            "1. 输出 CSV\n2. 标注来源"
                        )
                    }
                ],
            }
        ],
        candidate_skill={"description": "Demo", "content": "Procedure"},
        replay_windows={
            "recent": [
                {
                    "session_id": "session-1",
                    "instruction": "执行任务",
                }
            ],
            "historical": [],
        },
        case_count=1,
    )

    assert datasets[0]["synthesis_mode"] == "grounded_fallback"
    assert datasets[0]["requirements"] == ["输出 CSV", "标注来源"]
    assert all("通用" not in item for item in datasets[0]["requirements"])


def test_dataset_projection_keeps_checklist_hidden_from_initial_query() -> None:
    dataset = {
        "dataset_id": "synth-1",
        "dataset_format": DATASET_FORMAT,
        "query": "执行初始任务。",
        "requirements": ["输出报告", "注明来源"],
        "trajectory_requirements": ["读取材料"],
        "checklist": checklist_items(["输出报告", "注明来源"], ["读取材料"]),
        "progressive_disclosure": {"enabled": True, "batch_size": 2},
        "evidence_window": "recent",
        "source_session_ids": ["session-1"],
    }

    case = dataset_to_replay_case(dataset)

    assert case["instruction"] == "执行初始任务。"
    assert "输出报告" not in case["instruction"]
    assert len(case["checklist"]) == 3
    assert case["session_id"] == "session-1"


def test_progressive_disclosure_reveals_only_next_unmet_batch() -> None:
    checklist = checklist_items(["A", "B", "C", "D"], [])
    report = {
        "items": [
            {"id": "R01", "satisfied": True},
            {"id": "R02", "satisfied": False},
            {"id": "R03", "satisfied": False},
            {"id": "R04", "satisfied": False},
        ]
    }

    prompt, disclosed = next_disclosure_prompt(
        checklist=checklist,
        report=report,
        disclosed_ids=set(),
        round_number=2,
        batch_size=2,
    )

    assert disclosed == ["R02", "R03"]
    assert "[R02] B" in prompt
    assert "[R03] C" in prompt
    assert "R04" not in prompt
    assert "R01" not in prompt


def test_checklist_completion_precedes_efficiency_policy() -> None:
    efficiency = compare_efficiency(
        {"interaction_turns": 2, "tool_call_count": 3, "total_tokens": 300},
        {"interaction_turns": 4, "tool_call_count": 6, "total_tokens": 600},
    )
    candidate_only_passes = progressive_replay_decision(
        efficiency=efficiency,
        baseline_checklist={"total": 2, "all_satisfied": False},
        candidate_checklist={"total": 2, "all_satisfied": True},
    )
    candidate_fails = progressive_replay_decision(
        efficiency=compare_efficiency(
            {"interaction_turns": 4, "tool_call_count": 6, "total_tokens": 600},
            {"interaction_turns": 1, "tool_call_count": 1, "total_tokens": 100},
        ),
        baseline_checklist={"total": 2, "all_satisfied": True},
        candidate_checklist={"total": 2, "all_satisfied": False},
    )

    assert candidate_only_passes["accepted"] is True
    assert candidate_only_passes["decision_basis"] == (
        "candidate_only_completed_checklist"
    )
    assert candidate_fails["accepted"] is False
    assert candidate_fails["verdict"] == "reject"
    assert candidate_fails["decision_basis"] == "candidate_checklist_incomplete"
    assert candidate_fails["policy"] == (
        "progressive_checklist_then_turn_priority_v1"
    )


def test_progressive_jobs_replay_all_cases_while_legacy_keeps_one_per_window() -> None:
    progressive = select_replay_cases(
        [
            {
                "dataset_id": "test-1",
                "evidence_window": "recent",
                "checklist": [{"id": "R01", "text": "A"}],
            },
            {
                "dataset_id": "test-2",
                "evidence_window": "recent",
                "checklist": [{"id": "R01", "text": "B"}],
            },
        ]
    )
    legacy = select_replay_cases(
        [
            {"evidence_window": "recent", "instruction": "A"},
            {"evidence_window": "recent", "instruction": "B"},
            {"evidence_window": "historical", "instruction": "C"},
        ]
    )

    assert progressive == [("recent:test-1", 0), ("recent:test-2", 1)]
    assert legacy == [("recent", 0), ("historical", 2)]


def test_synthesized_dataset_store_round_trip(tmp_path) -> None:
    store = SynthesizedDatasetStore(LocalObjectStore(tmp_path))
    saved = store.save_generation(
        skill_name="demo-skill",
        generation_id="job-1",
        datasets=[{"dataset_id": "synth-1", "query": "Do work"}],
        source_session_ids=["session-1"],
        candidate_revision=2,
    )

    loaded = store.load_generation(
        skill_name="demo-skill",
        generation_id="job-1",
    )

    assert loaded == saved
    assert loaded["candidate_revision"] == 2
