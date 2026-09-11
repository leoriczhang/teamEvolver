"""Tests for the skill-evolution agent loop (plan → act → submit).

Scripted FakeLLM responses drive the whole loop: round-1 plan, tool-call
rounds with observations, final validation with correction rounds, the
plan gate, and best-effort close at round exhaustion.
"""

from __future__ import annotations

import asyncio
import json

from teamEvolver.evolve.agent import run_skill_agent
from teamEvolver.evolve.kernel.enums import DecisionAction


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
                "supporting_session_ids": ["s1"],
                "causal_link": "Retried identical grep parameters.",
            }
        ],
        "user_memory": [],
        "task_requirement": [],
        "agent_runtime": [],
        "insufficient_evidence": [],
    }


def _plan(action: str = "improve_skill", *, team_skill: list | None = None) -> str:
    return _turn(
        {
            "type": "plan",
            "action_candidate": action,
            "evidence_classification": _classification(team_skill),
            "plan": ["read evidence", "edit", "submit"],
            "sessions_to_read": ["s1"],
            "rationale": "gap",
        }
    )


def _tool_calls(*calls: dict) -> str:
    return _turn({"type": "tool_calls", "calls": list(calls)})


def _final(decision: dict) -> str:
    return _turn({"type": "final", "decision": decision})


CURRENT_SKILL = {
    "name": "llm-wiki",
    "description": "查询业务知识库",
    "category": "general",
    "content": "line1\nline2\n",
}
SESSIONS = [{"session_id": "s1", "_summary": "用户要求查询知识库", "_trajectory": "tool: search"}]
EVOLVE_KW = dict(
    stage="evolve_skill",
    system_prompt="You evolve skill {skill_name}.",
    skill_name="llm-wiki",
    sessions=SESSIONS,
    current_skill=CURRENT_SKILL,
    existing_skill_names=["llm-wiki"],
)


def _last_user(llm: FakeLLM) -> dict:
    return llm.calls[-1][-1]


def test_evolve_happy_path_plan_read_propose_final() -> None:
    llm = FakeLLM(
        [
            _plan(),
            _tool_calls({"tool": "read_session", "args": {"session_id": "s1", "part": "summary"}}),
            _tool_calls(
                {
                    "tool": "propose_edits",
                    "args": {
                        "edits": [
                            {
                                "operation": "replace",
                                "old_string": "line1",
                                "new_string": "LINE1",
                            }
                        ]
                    },
                }
            ),
            _final(
                {
                    "action": "improve_skill",
                    "skill": {"name": "llm-wiki"},
                    "rationale": "fix",
                    "evidence_classification": _classification(),
                }
            ),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))

    assert result is not None
    assert result["action"] == DecisionAction.IMPROVE
    skill = result["skill"]
    assert skill["content"] == "LINE1\nline2\n"
    assert skill["description"] == "查询业务知识库"
    assert skill["category"] == "general"
    assert skill["edit_stats"] == {"applied": 1, "attempts": 1}
    assert "edits" not in skill
    # 4 rounds: plan / read / propose / final
    assert len(llm.calls) == 4
    # The read observation carried the session summary back to the model.
    read_obs = llm.calls[2][-1]["content"]
    assert "用户要求查询知识库" in read_obs


def test_propose_failure_retried_after_observation() -> None:
    bad = _tool_calls(
        {
            "tool": "propose_edits",
            "args": {
                "edits": [
                    {"operation": "replace", "old_string": "ghost", "new_string": "x"}
                ]
            },
        }
    )
    good = _tool_calls(
        {
            "tool": "propose_edits",
            "args": {
                "edits": [
                    {"operation": "replace", "old_string": "line2", "new_string": "LINE2"}
                ]
            },
        }
    )
    llm = FakeLLM(
        [
            _plan(),
            bad,
            good,
            _final(
                {
                    "action": "improve_skill",
                    "skill": {"name": "llm-wiki"},
                    "rationale": "fix",
                    "evidence_classification": _classification(),
                }
            ),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))

    assert result is not None
    assert result["skill"]["content"] == "line1\nLINE2\n"
    assert result["skill"]["edit_stats"] == {"applied": 1, "attempts": 2}
    # The failure observation fed the model the anchor it hallucinated.
    failure_obs = llm.calls[2][-1]["content"]
    assert "编辑应用失败" in failure_obs
    assert "ghost" in failure_obs


def test_plan_skip_exits_after_one_call() -> None:
    llm = FakeLLM([_plan("skip", team_skill=[])])
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    assert len(llm.calls) == 1


def test_plan_gate_corrects_once_then_accepts() -> None:
    empty_plan = _plan("improve_skill", team_skill=[])
    skip_final = _final(
        {"action": "skip", "rationale": "no", "evidence_classification": _classification([])}
    )
    llm = FakeLLM([empty_plan, _plan(), skip_final])
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    # Correction round was sent for the empty team_skill bucket.
    correction = llm.calls[1][-1]["content"]
    assert "team_skill" in correction
    assert result["action"] == DecisionAction.SKIP
    assert len(llm.calls) == 3


def test_plan_gate_second_empty_plan_skips() -> None:
    empty_plan = _plan("improve_skill", team_skill=[])
    llm = FakeLLM([empty_plan, empty_plan])
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    assert "team_skill" in result["rationale"]
    assert len(llm.calls) == 2


def test_invalid_final_gets_correction_round() -> None:
    missing_skill = _final(
        {
            "action": "improve_skill",
            "rationale": "oops",
            "evidence_classification": _classification(),
        }
    )
    llm = FakeLLM(
        [
            _plan(),
            missing_skill,
            _final({"action": "skip", "rationale": "give up", "evidence_classification": _classification([])}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    validation_obs = llm.calls[2][-1]["content"]
    assert "缺少 skill 对象" in validation_obs


def test_final_rejects_inline_edits() -> None:
    with_edits = _final(
        {
            "action": "improve_skill",
            "skill": {
                "name": "llm-wiki",
                "edits": [{"operation": "replace", "old_string": "line1", "new_string": "X"}],
            },
            "rationale": "shortcut",
            "evidence_classification": _classification(),
        }
    )
    llm = FakeLLM(
        [
            _plan(),
            with_edits,
            _final({"action": "skip", "rationale": "redo", "evidence_classification": _classification([])}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    rejection = llm.calls[2][-1]["content"]
    assert "propose_edits" in rejection


def test_round_exhaustion_submits_staged_edits() -> None:
    propose = _tool_calls(
        {
            "tool": "propose_edits",
            "args": {
                "edits": [
                    {"operation": "replace", "old_string": "line1", "new_string": "LINE1"}
                ]
            },
        }
    )
    # max_rounds=3: plan / propose / (wasted read) → best-effort from staged.
    llm = FakeLLM(
        [
            _plan(),
            propose,
            _tool_calls({"tool": "read_session", "args": {"session_id": "s1"}}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW, max_rounds=3))

    assert result is not None
    assert result["action"] == DecisionAction.IMPROVE
    assert result["skill"]["content"] == "LINE1\nline2\n"
    assert result["skill"]["edit_stats"]["applied"] == 1
    # Penultimate round warned that a final is mandatory.
    warning = llm.calls[2][-1]["content"]
    assert "必须输出 final" in warning


def test_round_exhaustion_without_staging_returns_none() -> None:
    llm = FakeLLM(
        [
            _plan(),
            _tool_calls({"tool": "read_session", "args": {"session_id": "s1"}}),
            _tool_calls({"tool": "read_session", "args": {"session_id": "s1"}}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW, max_rounds=3))
    assert result is None


def test_legacy_one_shot_skip_payload_exits_round_one() -> None:
    legacy = json.dumps(
        {
            "action": "skip",
            "rationale": "no evidence",
            "evidence_classification": _classification([]),
        }
    )
    llm = FakeLLM([legacy])
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    assert len(llm.calls) == 1


def test_turn_parse_error_gets_protocol_correction() -> None:
    llm = FakeLLM(
        [
            "this is not json at all",
            _final({"action": "skip", "rationale": "ok", "evidence_classification": _classification([])}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    correction = llm.calls[1][-1]["content"]
    assert "无法解析" in correction


def test_file_changes_prevalidation_rejects_skill_md_write() -> None:
    final = _final(
        {
            "action": "improve_skill",
            "skill": {
                "name": "llm-wiki",
                "content": "new body",
                "file_changes": [
                    {"path": "SKILL.md", "operation": "upsert", "content": "x", "reason": "bad"}
                ],
            },
            "rationale": "bad change",
            "evidence_classification": _classification(),
        }
    )
    llm = FakeLLM(
        [
            _plan(),
            final,
            _final({"action": "skip", "rationale": "ok", "evidence_classification": _classification([])}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    rejection = llm.calls[2][-1]["content"]
    assert "file_changes" in rejection


def test_merge_final_returns_skill_object() -> None:
    merged = _turn(
        {
            "type": "final",
            "skill": {
                "name": "example-skill",
                "description": "merged",
                "content": "Merged body.",
            },
        }
    )
    llm = FakeLLM([merged])
    result = asyncio.run(
        run_skill_agent(
            llm,
            stage="merge",
            system_prompt="Merge two versions.",
            merge_versions=(
                {"name": "example-skill", "description": "a", "content": "A", "_version": 1},
                {"name": "example-skill", "description": "b", "content": "B"},
            ),
        )
    )
    assert result == {"name": "example-skill", "description": "merged", "content": "Merged body."}
    assert len(llm.calls) == 1


def test_merge_exhaustion_returns_none() -> None:
    llm = FakeLLM(["not final", "still not final"])
    result = asyncio.run(
        run_skill_agent(
            llm,
            stage="merge",
            system_prompt="Merge two versions.",
            merge_versions=(
                {"name": "example-skill", "description": "a", "content": "A", "_version": 1},
                {"name": "example-skill", "description": "b", "content": "B"},
            ),
            max_rounds=2,
        )
    )
    assert result is None


def test_create_flow_with_name_collision_check() -> None:
    async def fake_reader(name: str) -> str:
        return f"---\nname: {name}\n---\nexisting body"

    create_kw = dict(
        stage="create_skill",
        system_prompt="Create new skills.",
        sessions=SESSIONS,
        existing_skill_names=["taken-name"],
        library_reader=fake_reader,
    )
    final = _final(
        {
            "action": "create_skill",
            "skill": {
                "name": "new-skill",
                "description": "Does something.",
                "content": "Body.",
            },
            "rationale": "new pattern",
            "evidence_classification": _classification(),
        }
    )
    llm = FakeLLM(
        [
            _plan("create_skill"),
            _tool_calls({"tool": "check_name", "args": {"name": "taken-name"}}),
            _tool_calls({"tool": "check_name", "args": {"name": "new-skill"}}),
            final,
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **create_kw))

    assert result is not None
    assert result["action"] == DecisionAction.CREATE
    assert result["skill"]["name"] == "new-skill"
    collision_obs = llm.calls[2][-1]["content"]
    assert "已存在同名技能" in collision_obs


def test_create_read_library_skill_uses_async_reader() -> None:
    async def fake_reader(name: str) -> str:
        return "existing skill body"

    llm = FakeLLM(
        [
            _plan("create_skill"),
            _tool_calls({"tool": "read_library_skill", "args": {"name": "existing"}}),
            _final(
                {
                    "action": "create_skill",
                    "skill": {
                        "name": "new-skill",
                        "description": "d",
                        "content": "c",
                    },
                    "rationale": "r",
                    "evidence_classification": _classification(),
                }
            ),
        ]
    )
    result = asyncio.run(
        run_skill_agent(
            llm,
            stage="create_skill",
            system_prompt="Create.",
            sessions=SESSIONS,
            existing_skill_names=["existing"],
            library_reader=fake_reader,
        )
    )
    assert result["action"] == DecisionAction.CREATE
    obs = llm.calls[2][-1]["content"]
    assert "existing skill body" in obs


def test_search_sessions_returns_snippets() -> None:
    llm = FakeLLM(
        [
            _plan(),
            _tool_calls({"tool": "search_sessions", "args": {"query": "知识库"}}),
            _final({"action": "skip", "rationale": "ok", "evidence_classification": _classification([])}),
        ]
    )
    result = asyncio.run(run_skill_agent(llm, **EVOLVE_KW))
    assert result["action"] == DecisionAction.SKIP
    obs = llm.calls[2][-1]["content"]
    assert "s1" in obs


def test_round_log_records_loop_summary() -> None:
    rounds: list[dict] = []
    llm = FakeLLM([_plan("skip", team_skill=[])])
    asyncio.run(run_skill_agent(llm, **EVOLVE_KW, round_log=rounds))
    assert rounds == [{"round": 1, "type": "plan_skip"}]
