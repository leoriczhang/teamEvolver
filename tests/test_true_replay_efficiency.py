from __future__ import annotations

from teamEvolver.true_replay import branch_efficiency, compare_efficiency, count_tool_calls


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
