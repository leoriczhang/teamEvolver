from __future__ import annotations

from types import SimpleNamespace

from teamEvolver.checklist import (
    aggregate_branch_checklist_results,
    compile_common_checklist,
    evaluate_branch_checklist,
    objective_replay_decision,
    scope_checklist_for_case,
)
from teamEvolver.true_replay import (
    _agentshub_endpoint,
    annotate_cases,
    branch_efficiency,
    build_sandbox,
    compare_efficiency,
    count_tool_calls,
    replay_candidate_accepted,
    spawn_agentshub_branch,
)


def test_efficiency_compares_interactions_tools_and_tokens() -> None:
    baseline = {
        "interaction_turns": 3,
        "tool_call_count": 10,
        "total_tokens": 1000,
        "input_tokens": 800,
        "output_tokens": 200,
    }
    candidate = {
        "interaction_turns": 2,
        "tool_call_count": 6,
        "total_tokens": 700,
        "input_tokens": 560,
        "output_tokens": 140,
    }

    result = compare_efficiency(baseline, candidate)

    assert result["score"] > 0
    assert result["improved_dimensions"] == [
        "interaction_turns",
        "tool_call_count",
        "total_tokens",
    ]
    assert result["regressed_dimensions"] == []
    assert result["dimensions"]["interaction_turns"]["delta"] == 1
    assert result["dimensions"]["tool_call_count"]["delta"] == 4
    assert result["dimensions"]["total_tokens"]["delta"] == 300
    assert result["weights"] == {
        "interaction_turns": 0.60,
        "tool_call_count": 0.25,
        "total_tokens": 0.15,
    }


def test_turn_reduction_is_the_primary_efficiency_signal() -> None:
    turns = compare_efficiency(
        {"interaction_turns": 4, "tool_call_count": 10, "total_tokens": 1000},
        {"interaction_turns": 2, "tool_call_count": 10, "total_tokens": 1000},
    )
    tools = compare_efficiency(
        {"interaction_turns": 4, "tool_call_count": 10, "total_tokens": 1000},
        {"interaction_turns": 4, "tool_call_count": 5, "total_tokens": 1000},
    )
    tokens = compare_efficiency(
        {"interaction_turns": 4, "tool_call_count": 10, "total_tokens": 1000},
        {"interaction_turns": 4, "tool_call_count": 10, "total_tokens": 500},
    )

    assert turns["score"] == 0.30
    assert tools["score"] == 0.125
    assert tokens["score"] == 0.075


def test_efficiency_regression_is_bounded_when_baseline_is_zero() -> None:
    result = compare_efficiency(
        {"interaction_turns": 0, "tool_call_count": 0, "total_tokens": 0},
        {"interaction_turns": 2, "tool_call_count": 9, "total_tokens": 978_938},
    )

    assert result["score"] == -1.0
    assert all(
        metric["reduction_ratio"] == -1.0
        for metric in result["dimensions"].values()
    )


def test_agentshub_endpoint_falls_back_to_service_config(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENTSHUB_REPLAY_URL", raising=False)
    monkeypatch.setattr(
        "teamEvolver.config_store.ConfigStore.to_config",
        lambda _self: SimpleNamespace(
            validation_agentshub_url="http://127.0.0.1:5173"
        ),
    )

    assert _agentshub_endpoint({"runtime": {"type": "agentshub"}}) == (
        "http://127.0.0.1:5173"
    )


def test_common_checklist_requires_cross_session_support_and_excludes_personal() -> None:
    job = {
        "proposed_action": "improve_skill",
        "evidence_classification": {
            "team_skill": [
                {
                    "claim": "Always validate after the final file edit.",
                    "supporting_session_ids": ["s1", "s2"],
                    "causal_link": "Both users needed a correction.",
                }
            ],
            "user_memory": ["User A likes blue."],
        },
        "session_evidence": [
            {"session_id": "s1", "user_alias": "u1"},
            {"session_id": "s2", "user_alias": "u2"},
        ],
        "replay_cases": [],
    }

    checklist = compile_common_checklist(job)

    assert checklist["commonality"]["passed"] is True
    assert checklist["commonality"]["distinct_session_count"] == 2
    assert checklist["excluded_personal_evidence"] == ["User A likes blue."]
    assert any(item["claim"].startswith("Always validate") for item in checklist["items"])


def test_objective_policy_uses_checklist_gate_and_turn_gain() -> None:
    checklist = {
        "commonality": {"passed": True},
        "items": [{"id": "execution_complete", "kind": "hard"}],
    }
    baseline = {
        "passed": True,
        "pass_rate": 1.0,
        "hard_pass": True,
        "items": [
            {"id": "execution_complete", "passed": True},
        ],
    }
    candidate = {
        "passed": True,
        "pass_rate": 1.0,
        "hard_pass": True,
        "items": [
            {"id": "execution_complete", "passed": True},
        ],
    }
    efficiency = compare_efficiency(
        {"interaction_turns": 4, "tool_call_count": 8, "total_tokens": 1000},
        {"interaction_turns": 2, "tool_call_count": 8, "total_tokens": 1000},
    )

    decision = objective_replay_decision(
        checklist=checklist,
        baseline=baseline,
        candidate=candidate,
        efficiency=efficiency,
    )

    assert decision["accepted"] is True
    assert decision["turn_gain"] == 0.5
    assert decision["policy"] == "checklist_efficiency_v1"


def test_objective_policy_rejects_itemwise_regression_at_equal_coverage() -> None:
    checklist = {
        "commonality": {"passed": True},
        "items": [
            {"id": "old", "kind": "soft", "required": True},
            {"id": "new", "kind": "soft", "required": True},
        ],
    }
    decision = objective_replay_decision(
        checklist=checklist,
        baseline={
            "passed": False,
            "hard_pass": True,
            "pass_rate": 0.5,
            "items": [
                {"id": "old", "passed": True},
                {"id": "new", "passed": False},
            ],
        },
        candidate={
            "passed": False,
            "hard_pass": True,
            "pass_rate": 0.5,
            "items": [
                {"id": "old", "passed": False},
                {"id": "new", "passed": True},
            ],
        },
        efficiency=compare_efficiency(
            {"interaction_turns": 4, "tool_call_count": 4, "total_tokens": 400},
            {"interaction_turns": 2, "tool_call_count": 4, "total_tokens": 400},
        ),
    )

    assert decision["accepted"] is False
    assert decision["no_regression"] is False
    assert decision["regressed_item_ids"] == ["old"]


def test_scoped_results_aggregate_back_into_merge_union() -> None:
    checklist = {
        "commonality": {"passed": True},
        "items": [
            {
                "id": "base",
                "kind": "hard",
                "required": True,
                "scope": "all_cases",
            },
            {
                "id": "new",
                "kind": "soft",
                "required": True,
                "scope": "source_sessions",
                "source_session_ids": ["new-session"],
            },
            {
                "id": "old",
                "kind": "soft",
                "required": True,
                "scope": "source_sessions",
                "source_session_ids": ["old-session"],
            },
        ],
    }
    recent = scope_checklist_for_case(
        checklist,
        {"session_id": "new-session", "evidence_window": "recent"},
    )
    historical = scope_checklist_for_case(
        checklist,
        {"session_id": "old-session", "evidence_window": "historical"},
    )
    assert [item["id"] for item in recent["items"]] == ["base", "new"]
    assert [item["id"] for item in historical["items"]] == ["base", "old"]

    aggregated = aggregate_branch_checklist_results(
        checklist,
        [
            {
                "items": [
                    {"id": "base", "passed": True},
                    {"id": "new", "passed": True},
                ]
            },
            {
                "items": [
                    {"id": "base", "passed": True},
                    {"id": "old", "passed": True},
                ]
            },
        ],
    )

    assert aggregated["passed"] is True
    assert aggregated["pass_rate"] == 1.0


def test_hard_checklist_is_evaluated_from_real_branch_evidence() -> None:
    checklist = {
        "items": [
            {"id": "execution_complete", "kind": "hard", "evaluator": "branch_ok"},
            {
                "id": "artifact_contract",
                "kind": "hard",
                "evaluator": "artifact_contract",
            },
            {
                "id": "post_write_validation",
                "kind": "hard",
                "evaluator": "post_write_validation",
            },
        ]
    }
    result = evaluate_branch_checklist(
        checklist,
        {
            "ok": True,
            "artifact_gap_report": {"passed": True},
            "post_write_validation_passed": True,
        },
    )

    assert result["hard_pass"] is True
    assert result["pass_rate"] == 1.0


def test_branch_efficiency_counts_tool_calls_from_messages() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "1"}, {"id": "2"}],
        },
        {"role": "tool", "content": "ok"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "3"}],
        },
    ]

    assert count_tool_calls(messages) == 3
    assert branch_efficiency({"messages": messages})["tool_call_count"] == 3


def test_baseline_sandbox_installs_current_skill(tmp_path) -> None:
    harness = {
        "base_url": "http://model",
        "api_key": "key",
        "model": "model",
        "api_mode": "chat",
        "max_tokens": 1024,
    }

    sandbox = build_sandbox(
        tmp_path,
        "baseline",
        harness,
        {"name": "existing-skill", "content": "current procedure"},
    )

    skill_file = (
        tmp_path / "baseline" / ".hermes" / "skills" / "existing-skill" / "SKILL.md"
    )
    assert sandbox["home"] == str(tmp_path / "baseline")
    assert skill_file.read_text("utf-8") == "current procedure"


def test_agentshub_branch_uses_native_replay_endpoint(monkeypatch) -> None:
    captured = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"branch": "candidate", "runtime": "agentshub", "ok": True}

    def fake_post(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["timeout"] = kwargs["timeout"]
        return _Response()

    monkeypatch.setenv("AGENTSHUB_REPLAY_URL", "http://127.0.0.1:5173")
    monkeypatch.setattr("httpx.post", fake_post)
    result = spawn_agentshub_branch(
        "candidate",
        "执行任务",
        {"name": "candidate", "content": "procedure"},
        {
            "candidate_skill_name": "candidate",
            "candidate_skill": {"name": "candidate"},
        },
        {"turn_num": 1},
        {"runtime": {"type": "agentshub"}, "turns": []},
        1200,
        1,
    )

    assert result["ok"] is True
    assert captured["url"].endswith("/api/internal/team-evolver/replay")
    assert captured["json"]["skill"]["name"] == "candidate"
    assert captured["json"]["timeout_seconds"] == 1200
    assert captured["timeout"] == 1230


def test_annotated_case_preserves_evaluation_profile() -> None:
    cases = annotate_cases(
        {
            "replay_cases": [
                {
                    "session_id": "session-1",
                    "turn_num": 1,
                    "instruction": "Create an HTML deck.",
                    "evaluation_profile": "html_ppt_methodology_v1",
                }
            ]
        },
        [],
    )

    assert cases[0]["evaluation_profile"] == "html_ppt_methodology_v1"


def test_true_replay_does_not_call_a_tiny_efficiency_tie_an_improvement() -> None:
    assert replay_candidate_accepted(
        baseline_score=1.0,
        candidate_score=1.0,
        min_score=0.75,
        tolerance=0.15,
        efficiency_score=0.0021,
    ) is False
    assert replay_candidate_accepted(
        baseline_score=1.0,
        candidate_score=1.0,
        min_score=0.75,
        tolerance=0.15,
        efficiency_score=0.08,
    ) is True


def test_true_replay_accepts_large_efficiency_gain_within_quality_tolerance() -> None:
    assert replay_candidate_accepted(
        baseline_score=0.97,
        candidate_score=0.93,
        min_score=0.75,
        tolerance=0.15,
        efficiency_score=0.63,
    ) is True
    assert replay_candidate_accepted(
        baseline_score=0.97,
        candidate_score=0.70,
        min_score=0.75,
        tolerance=0.15,
        efficiency_score=0.80,
    ) is False
