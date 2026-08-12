from __future__ import annotations

from types import SimpleNamespace

from teamEvolver.replay_metrics import objective_replay_decision
from teamEvolver.true_replay import (
    _agentshub_endpoint,
    annotate_cases,
    branch_efficiency,
    build_sandbox,
    compare_efficiency,
    count_tool_calls,
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

    assert result["improved_dimensions"] == [
        "interaction_turns",
        "tool_call_count",
        "total_tokens",
    ]
    assert result["regressed_dimensions"] == []
    assert result["dimensions"]["interaction_turns"]["delta"] == 1
    assert result["dimensions"]["tool_call_count"]["delta"] == 4
    assert result["dimensions"]["total_tokens"]["delta"] == 300
    assert result["unchanged_dimensions"] == []


def test_each_efficiency_metric_is_compared_without_weights() -> None:
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

    assert turns["improved_dimensions"] == ["interaction_turns"]
    assert tools["improved_dimensions"] == ["tool_call_count"]
    assert tokens["improved_dimensions"] == ["total_tokens"]
    assert "score" not in turns
    assert "weights" not in turns


def test_efficiency_regression_is_bounded_when_baseline_is_zero() -> None:
    result = compare_efficiency(
        {"interaction_turns": 0, "tool_call_count": 0, "total_tokens": 0},
        {"interaction_turns": 2, "tool_call_count": 9, "total_tokens": 978_938},
    )

    assert result["regressed_dimensions"] == [
        "interaction_turns",
        "tool_call_count",
        "total_tokens",
    ]
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


def test_objective_policy_accepts_metric_reduction_without_regression() -> None:
    efficiency = compare_efficiency(
        {"interaction_turns": 4, "tool_call_count": 8, "total_tokens": 1000},
        {"interaction_turns": 2, "tool_call_count": 8, "total_tokens": 1000},
    )

    decision = objective_replay_decision(
        efficiency=efficiency,
    )

    assert decision["accepted"] is True
    assert decision["improved_metrics"] == ["interaction_turns"]
    assert decision["policy"] == "true_replay_turn_priority_v2"
    assert decision["decision_basis"] == "interaction_turns_decreased"


def test_turn_reduction_wins_even_when_tools_and_tokens_increase() -> None:
    decision = objective_replay_decision(
        efficiency=compare_efficiency(
            {"interaction_turns": 4, "tool_call_count": 4, "total_tokens": 400},
            {"interaction_turns": 2, "tool_call_count": 8, "total_tokens": 800},
        ),
    )

    assert decision["accepted"] is True
    assert decision["verdict"] == "accept"
    assert decision["decision_basis"] == "interaction_turns_decreased"
    assert decision["regressed_metrics"] == ["tool_call_count", "total_tokens"]


def test_turn_increase_loses_even_when_tools_and_tokens_decrease() -> None:
    decision = objective_replay_decision(
        efficiency=compare_efficiency(
            {"interaction_turns": 2, "tool_call_count": 8, "total_tokens": 800},
            {"interaction_turns": 3, "tool_call_count": 4, "total_tokens": 400},
        ),
    )

    assert decision["accepted"] is False
    assert decision["verdict"] == "reject"
    assert decision["decision_basis"] == "interaction_turns_increased"


def test_equal_turns_compare_tools_and_tokens() -> None:
    decision = objective_replay_decision(
        efficiency=compare_efficiency(
            {"interaction_turns": 2, "tool_call_count": 8, "total_tokens": 800},
            {"interaction_turns": 2, "tool_call_count": 6, "total_tokens": 800},
        ),
    )

    assert decision["accepted"] is True
    assert decision["decision_basis"] == "secondary_metrics_decreased"


def test_objective_policy_does_not_accept_quality_score_without_metric_gain() -> None:
    decision = objective_replay_decision(
        efficiency=compare_efficiency(
            {"interaction_turns": 2, "tool_call_count": 4, "total_tokens": 400},
            {"interaction_turns": 2, "tool_call_count": 4, "total_tokens": 400},
        ),
    )

    assert decision["accepted"] is False
    assert decision["verdict"] == "inconclusive"
    assert decision["unchanged_metrics"] == [
        "interaction_turns",
        "tool_call_count",
        "total_tokens",
    ]


def test_equal_turns_reject_when_a_secondary_metric_increases() -> None:
    decision = objective_replay_decision(
        efficiency=compare_efficiency(
            {"interaction_turns": 2, "tool_call_count": 4, "total_tokens": 400},
            {"interaction_turns": 2, "tool_call_count": 5, "total_tokens": 500},
        ),
    )

    assert decision["accepted"] is False
    assert decision["verdict"] == "reject"
    assert decision["no_regression"] is False
    assert decision["decision_basis"] == "secondary_metrics_increased"


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
    installed = skill_file.read_text("utf-8")
    assert "name: existing-skill" in installed
    assert installed.rstrip().endswith("current procedure")


def test_sandbox_installs_complete_candidate_bundle(tmp_path) -> None:
    from teamEvolver.skills.bundle import attach_bundle_payload

    harness = {
        "base_url": "http://model",
        "api_key": "key",
        "model": "model",
        "api_mode": "chat",
        "max_tokens": 1024,
    }
    skill = attach_bundle_payload(
        {
            "name": "bundle-skill",
            "description": "Bundle",
            "content": "Run scripts/run.py.",
        },
        {
            "SKILL.md": b"stale",
            "scripts/run.py": b"print('candidate')\n",
            "assets/data.bin": b"\x00\xff",
        },
    )

    sandbox = build_sandbox(tmp_path, "candidate", harness, skill)
    skill_root = tmp_path / "candidate" / ".hermes" / "skills" / "bundle-skill"

    assert (skill_root / "scripts" / "run.py").read_text() == "print('candidate')\n"
    assert (skill_root / "assets" / "data.bin").read_bytes() == b"\x00\xff"
    assert sandbox["skill_tree_sha256"] == skill["bundle"]["tree_sha256"]


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
