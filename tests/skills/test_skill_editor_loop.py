"""Skill keeps its existing gates and exact-edit behavior on the shared loop."""
from __future__ import annotations

import json

import pytest

from team_skills.evolution.agent.runner import run_skill_agent
from tests.support.editor_loop import ScriptedEditorLLM

CLASSIFICATION = {"team_skill": [{"claim": "Confirmed shared correction", "supporting_session_ids": ["s1", "s2"]}]}
ORIGINAL = '# Existing behavior\nOriginal rule.\n\n```json\n{"quoted": "\\\\value"}\n```\n\n## Keep\nUntouched.\n'


def plan():
    return {"type": "plan", "action_candidate": "improve_skill", "evidence_classification": CLASSIFICATION}


def patch(old="Original rule.", new="Clarified rule."):
    return {"type": "tool_calls", "calls": [{"tool": "propose_edits", "args": {"edits": [
        {"operation": "replace", "old_string": old, "new_string": new},
    ]}}]}


def final(skill=None):
    return {"type": "final", "decision": {"action": "improve_skill", "skill": skill or {"name": "demo"},
                                           "evidence_classification": CLASSIFICATION}}


async def run(model, **kwargs):
    return await run_skill_agent(
        model, stage="evolve_skill", system_prompt="Keep {skill_name} behavior", skill_name="demo",
        current_skill={
            "name": "demo", "content": ORIGINAL, "description": "Existing description", "category": "general",
        },
        sessions=[{"session_id": "s1", "_summary": "Correction", "_trajectory": "Recorded correction"}],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_failed_anchor_is_observed_and_corrected_while_unrelated_skill_bytes_are_preserved():
    model = ScriptedEditorLLM([
        plan(),
        patch(old="invented anchor"),
        {"type": "tool_calls", "calls": [{"tool": "read_skill", "args": {}}]},
        patch(),
        final(),
    ])
    log = []

    result = await run(model, round_log=log)

    assert result["skill"]["content"] == ORIGINAL.replace("Original rule.", "Clarified rule.")
    assert result["skill"]["edit_stats"] == {"applied": 1, "attempts": 2}
    assert result["skill"]["description"] == "Existing description"
    assert "编辑应用失败" in model.calls[2]["messages"][-1]["content"]
    observed = json.loads(model.calls[3]["messages"][-1]["content"].split("## Observations\n", 1)[1])
    assert ORIGINAL in observed[0]["content"]
    assert [entry["type"] for entry in log] == ["plan_accepted", "tool_calls", "tool_calls", "tool_calls", "final"]
    assert all(message["role"] != "assistant" for call in model.calls for message in call["messages"])


@pytest.mark.asyncio
async def test_skill_evidence_gate_corrects_once_then_skips_without_executing_tools():
    model = ScriptedEditorLLM([
        {"type": "plan", "action_candidate": "improve_skill"},
        {"type": "plan", "action_candidate": "improve_skill"},
    ])
    log = []

    result = await run(model, round_log=log)

    assert result["action"] == "skip"
    assert "team_skill 证据仍为空" in result["rationale"]
    assert [entry["type"] for entry in log] == ["plan_correction", "plan_gate_skip"]
    assert len(model.calls) == 2
    assert model.calls[1]["messages"][-1]["content"].startswith("## Loop state\n{}")


@pytest.mark.asyncio
@pytest.mark.parametrize("apply_patch", [True, False])
async def test_round_exhaustion_only_retains_staged_edits_with_accepted_shared_evidence(apply_patch):
    action = patch() if apply_patch else {"type": "tool_calls", "calls": [{"tool": "check_generalization", "args": {}}]}
    model = ScriptedEditorLLM([plan(), action])

    result = await run(model, max_rounds=2)

    assert len(model.calls) == 2
    if apply_patch:
        assert result["action"] == "improve_skill"
        assert result["skill"]["content"] == ORIGINAL.replace("Original rule.", "Clarified rule.")
        assert result["rationale"] == "Agent loop 轮数耗尽；提交已暂存的编辑（best-effort）。"
    else:
        assert result is None


@pytest.mark.asyncio
async def test_final_without_content_or_applied_edit_is_rejected_then_can_skip():
    model = ScriptedEditorLLM([
        plan(), final(), {"type": "final", "decision": {"action": "skip", "rationale": "Insufficient correction"}},
    ])

    result = await run(model)

    assert result["action"] == "skip"
    assert "没有 content" in model.calls[2]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_merge_keeps_its_asset_contract_and_default_final_log_action():
    model = ScriptedEditorLLM([{"type": "final", "decision": {"name": "demo", "content": "Merged instructions"}}])
    log = []

    result = await run_skill_agent(
        model, stage="merge", system_prompt="Keep both versions", merge_versions=(
            {"name": "demo", "content": "first"}, {"name": "demo", "content": "second"},
        ), round_log=log,
    )

    assert result == {"name": "demo", "content": "Merged instructions"}
    assert log == [{"round": 1, "type": "final", "action": "merge"}]


@pytest.mark.asyncio
async def test_skill_tool_allowlist_does_not_execute_business_replay_requests():
    model = ScriptedEditorLLM([
        plan(), {"type": "tool_calls", "calls": [{"tool": "run_replay", "args": {}}]},
        {"type": "final", "decision": {"action": "skip"}},
    ])

    result = await run(model)

    assert result["action"] == "skip"
    assert "unknown or unavailable tool: run_replay" in model.calls[2]["messages"][-1]["content"]
