"""A complete judge payload with an omitted closing brace remains usable."""
import asyncio
import json

import pytest

from team_skills.evolution.stages.analyze import SessionAnalysisError, analyze_session


def session():
    return {"session_id": "json-completion", "turns": [
        {"prompt_text": "完成交付", "response_text": "已核对交付", "tool_calls": [], "tool_results": []},
    ]}


class Client:
    model = "fixture"

    def __init__(self, judge):
        self.judge = judge

    async def chat(self, messages, **kwargs):
        return (
            '<classification>{"decision":"valuable","confidence":0.9,"reason":"证据完整"}</classification>'
            "<summary>工具证据与交付结果已核对。</summary>"
            f"<judge>{self.judge}</judge>"
        )


def test_complete_scores_with_missing_outer_brace_preserve_values_and_reasons():
    judge = {
        "task_completion": 0.3, "response_quality": 0.4, "efficiency": 0.5, "tool_usage": 0.5,
        "evolution_evidence": "none", "reasons": {"tool_usage": ["命令包含 {text} 和 [value]。"]},
    }
    current = session()
    asyncio.run(analyze_session(Client(json.dumps(judge, ensure_ascii=False)[:-1]), current))
    assert current["value_judge"]["decision"] == "valuable"
    assert current["_judge_scores"]["task_completion"] == 0.3
    assert current["_judge_scores"]["response_quality"] == 0.4
    assert current["_judge_scores"]["reasons"] == judge["reasons"]
    assert current["_judge_scores"]["source"] == "llm"


@pytest.mark.parametrize("judge", [
    '{"task_completion":0.3,"response_quality":0.4,"efficiency":0.5',
    '{"task_completion":0.3,"response_quality":0.4,"efficiency":0.5,"tool_usage":',
    '{"task_completion":0.3,"response_quality":0.4,"efficiency":0.5,"tool_usage":0.5,"rationale":"unfinished',
    '{"task_completion":0.3,"response_quality":0.4,"efficiency":0.5,"tool_usage":0.5]',
])
def test_incomplete_values_or_strings_never_create_analysis_outputs(judge):
    current = session()
    with pytest.raises(SessionAnalysisError, match="judge"):
        asyncio.run(analyze_session(Client(judge), current))
    assert "_judge_scores" not in current
    assert "_summary" not in current
