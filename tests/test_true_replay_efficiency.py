from __future__ import annotations

from teamEvolver.true_replay import (
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
        30,
        1,
    )

    assert result["ok"] is True
    assert captured["url"].endswith("/api/internal/team-evolver/replay")
    assert captured["json"]["skill"]["name"] == "candidate"
