"""End-to-end tests for the agent-loop improve_skill flow (execute stage).

Uses a scripted fake LLM so the whole loop runs: round-1 plan → tool
rounds with observations → final validation → materialized candidate.
Legacy one-shot shapes (old skip / full-content improve payloads) must
still exit on round one unchanged.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from teamEvolver.evolve.agent.tools import build_protocol_appendix
from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.stages import execute as ex
from teamEvolver.evolve.stages.execute import evolve_skill_from_sessions


class FakeLLM:
    """Scripted chat double; records every message list it receives."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **_kwargs) -> str:
        self.calls.append([dict(m) for m in messages])
        return self.responses.pop(0)


def _turn(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _classification(team_skill: list | None = None) -> dict:
    return {
        "team_skill": team_skill
        if team_skill is not None
        else [
            {
                "claim": "Add a timeout recovery rule.",
                "supporting_session_ids": ["s1", "s2"],
                "causal_link": "Both runs retried identical grep parameters.",
            }
        ],
        "user_memory": [],
        "task_requirement": [],
        "agent_runtime": [],
        "insufficient_evidence": [],
    }


def _plan() -> str:
    return _turn(
        {
            "type": "plan",
            "action_candidate": "improve_skill",
            "evidence_classification": _classification(),
            "plan": ["read skill", "edit", "submit"],
            "rationale": "Two sessions hit the same gap.",
        }
    )


def _propose(edits: list[dict]) -> str:
    return _turn({"type": "tool_calls", "calls": [{"tool": "propose_edits", "args": {"edits": edits}}]})


def _final_improve() -> str:
    return _turn(
        {
            "type": "final",
            "decision": {
                "action": "improve_skill",
                "skill": {"name": "llm-wiki"},
                "rationale": "Two sessions hit the same gap.",
                "evidence_classification": _classification(),
            },
        }
    )


def _final_skip() -> str:
    return _turn(
        {
            "type": "final",
            "decision": {
                "action": "skip",
                "rationale": "not enough evidence",
                "evidence_classification": _classification([]),
            },
        }
    )


def _legacy_improve(skill: dict) -> str:
    return _turn(
        {
            "action": "improve_skill",
            "skill": skill,
            "rationale": "Two sessions hit the same gap.",
            "evidence_classification": _classification(),
        }
    )


CURRENT_SKILL = {
    "name": "llm-wiki",
    "description": "查询业务知识库",
    "category": "general",
    "content": "line1\nline2\n",
}

SESSIONS = [{"session_id": "s1"}, {"session_id": "s2"}]


def test_edits_are_materialized_with_defaults() -> None:
    llm = FakeLLM(
        [
            _plan(),
            _propose(
                [{"operation": "replace", "old_string": "line1", "new_string": "LINE1"}]
            ),
            _final_improve(),
        ]
    )
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )

    assert result is not None
    assert result["action"] == DecisionAction.IMPROVE
    skill = result["skill"]
    # Edit applied; untouched line byte-identical.
    assert skill["content"] == "LINE1\nline2\n"
    # description/category defaulted from the current skill when omitted.
    assert skill["description"] == "查询业务知识库"
    assert skill["category"] == "general"
    assert skill["edit_stats"] == {"applied": 1, "attempts": 1}
    # The raw edits list is not leaked into the candidate payload.
    assert "edits" not in skill


def test_failed_anchor_retried_after_observation() -> None:
    llm = FakeLLM(
        [
            _plan(),
            _propose(
                [{"operation": "replace", "old_string": "not-in-content", "new_string": "x"}]
            ),
            _propose(
                [{"operation": "replace", "old_string": "line2", "new_string": "LINE2"}]
            ),
            _final_improve(),
        ]
    )
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )

    assert result is not None
    assert result["skill"]["content"] == "line1\nLINE2\n"
    assert result["skill"]["edit_stats"]["attempts"] == 2
    # The failed propose round fed the anchor failure back as an observation.
    assert len(llm.calls) == 4
    feedback = llm.calls[2][-1]
    assert feedback["role"] == "user"
    assert "编辑应用失败" in feedback["content"]
    assert "not-in-content" in feedback["content"]


def test_persistently_failed_edits_reject_the_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ex, "_AGENT_LIMITS", {"max_rounds": 3, "max_tool_calls_per_round": 4}
    )
    bad = _propose([{"operation": "replace", "old_string": "ghost", "new_string": "x"}])
    llm = FakeLLM([_plan(), bad, bad])
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )
    assert result is None
    assert len(llm.calls) == 3


def test_legacy_full_content_output_still_accepted() -> None:
    llm = FakeLLM([_legacy_improve({"name": "llm-wiki", "content": "brand new body"})])
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )
    assert result is not None
    assert result["skill"]["content"] == "brand new body"
    assert len(llm.calls) == 1


def test_improve_without_content_gets_correction_then_skip() -> None:
    llm = FakeLLM([_plan(), _legacy_improve({"name": "llm-wiki"}), _final_skip()])
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )
    assert result is not None
    assert result["action"] == DecisionAction.SKIP
    # The correction observation explained the missing content/edits.
    correction = llm.calls[2][-1]["content"]
    assert "既没有 content" in correction


def test_skip_action_passes_through_without_llm_retry() -> None:
    payload = _turn(
        {
            "action": "skip",
            "rationale": "no evidence",
            "evidence_classification": {
                "team_skill": [],
                "user_memory": [],
                "task_requirement": [],
                "agent_runtime": [],
                "insufficient_evidence": ["weak"],
            },
        }
    )
    llm = FakeLLM([payload])
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )
    assert result is not None
    assert result["action"] == DecisionAction.SKIP
    assert len(llm.calls) == 1


def test_read_skill_returns_staged_content_for_anchors() -> None:
    read = _turn({"type": "tool_calls", "calls": [{"tool": "read_skill", "args": {}}]})
    llm = FakeLLM(
        [
            _plan(),
            read,
            _propose(
                [{"operation": "replace", "old_string": "line1", "new_string": "LINE1"}]
            ),
            _final_improve(),
        ]
    )
    result = asyncio.run(
        evolve_skill_from_sessions(llm, "llm-wiki", SESSIONS, CURRENT_SKILL, [])
    )
    assert result is not None
    # The read_skill observation carried the byte-exact current content.
    obs = llm.calls[2][-1]["content"]
    assert "line1" in obs
    assert "llm-wiki" in obs


def test_prompt_contract_lives_in_appendix_and_card() -> None:
    appendix = build_protocol_appendix("evolve_skill")

    # The anchor contract (formerly in the task card) is code-owned now.
    assert "insert_after" in appendix
    assert "old_string" in appendix
    assert "propose_edits" in appendix
    # The final-decision schema is part of the appendix, not the card.
    assert '"edits"' in appendix
    # The task card describes the loop workflow without one-shot output specs.
    assert "propose_edits" in ex._EVOLVE_FROM_SESSIONS_SYSTEM
    assert "read_skill" in ex._EVOLVE_FROM_SESSIONS_SYSTEM
    assert "输出格式" not in ex._EVOLVE_FROM_SESSIONS_SYSTEM
    # Shared blocks still expand into the card at import time.
    assert "用户优先级声明" in ex._EVOLVE_FROM_SESSIONS_SYSTEM
