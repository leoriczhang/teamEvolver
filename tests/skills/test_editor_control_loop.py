"""Shared control corrects submissions without assuming Skill asset contracts."""
from __future__ import annotations

import json

import pytest

from team_skills.evolution.agent.loop import LoopPlanResult, run_editor_loop
from tests.support.editor_loop import ScriptedEditorLLM


class ComponentAdapter:
    """A deliberately non-Skill contract with no classification or bundle fields."""

    def __init__(self):
        self.text = "existing business instruction"
        self.observations = []
        self.fallback_calls = 0

    def plan(self, plan, *, round_no, max_rounds):
        if plan.get("action_candidate") == "skip":
            return LoopPlanResult(done=True, result={"action": "skip"}, log_kind="plan_skip")
        return LoopPlanResult(message=self.observe(
            [{"ok": True, "scope": "component"}], round_no=round_no, max_rounds=max_rounds,
        ))

    async def tools(self, calls):
        results = []
        for call in calls:
            if call["tool"] == "replace_component" and call["args"].get("component_id") == "business-prompt":
                self.text = call["args"]["content"]
                results.append({"ok": True, "content": self.text})
            else:
                results.append({"ok": False, "error": "component outside edit scope"})
        return results

    def finalize(self, decision):
        if decision.get("component_id") != "business-prompt" or self.text == "existing business instruction":
            return None, ["Read and patch the selected business component first"]
        return {"action": "change_execution", "content": self.text}, []

    def observe(self, observations, *, round_no, max_rounds, must_submit=False):
        self.observations.append(observations)
        return json.dumps({"round": f"{round_no}/{max_rounds}", "observations": observations,
                           "must_submit": must_submit}, ensure_ascii=False)

    def fallback(self):
        self.fallback_calls += 1
        return None


@pytest.mark.asyncio
async def test_asset_adapter_repairs_failed_submission_then_patches_only_its_selected_component():
    model = ScriptedEditorLLM([
        {"type": "plan", "action_candidate": "change_execution"},
        {"type": "final", "decision": {"component_id": "outside"}},
        {"type": "tool_calls", "calls": [{"tool": "replace_component", "args": {
            "component_id": "business-prompt", "content": "clarified original requirement",
        }}]},
        {"type": "final", "decision": {"component_id": "business-prompt"}},
    ])
    adapter = ComponentAdapter()
    result = await run_editor_loop(model, adapter=adapter, system="business editor", first_user="complete evidence")

    assert result == {"action": "change_execution", "content": "clarified original requirement"}
    assert adapter.observations[1] == [{
        "tool": "(validation)", "ok": False, "errors": ["Read and patch the selected business component first"],
    }]
    assert adapter.fallback_calls == 0
    assert len(model.calls) == 4
    assert all(message["role"] != "assistant" for call in model.calls for message in call["messages"])


@pytest.mark.asyncio
async def test_provider_failure_propagates_without_accepting_staged_work_or_retrying_the_provider():
    model = ScriptedEditorLLM([RuntimeError("provider unavailable")])
    adapter = ComponentAdapter()

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await run_editor_loop(model, adapter=adapter, system="editor", first_user="evidence")

    assert len(model.calls) == 1
    assert adapter.fallback_calls == 0


@pytest.mark.asyncio
async def test_plan_can_skip_without_tool_or_final_calls():
    adapter = ComponentAdapter()
    model = ScriptedEditorLLM([{"type": "plan", "action_candidate": "skip"}])

    assert await run_editor_loop(model, adapter=adapter, system="editor", first_user="evidence") == {"action": "skip"}
    assert len(model.calls) == 1
    assert adapter.text == "existing business instruction"
    assert adapter.fallback_calls == 0


@pytest.mark.asyncio
async def test_parser_override_remains_strict_at_round_limit_and_only_adapter_decides_fallback():
    parsed = []

    def strict_parser(raw, *, allow_repair=False):
        parsed.append((raw, allow_repair))
        return {"ok": False, "error": "invalid protocol"}

    adapter = ComponentAdapter()
    model = ScriptedEditorLLM(["broken1", "broken2"])
    result = await run_editor_loop(model, adapter=adapter, system="editor", first_user="evidence",
                                   max_rounds=2, parse=strict_parser)

    assert result is None
    assert parsed == [("broken1", False), ("broken2", False), ("broken2", True)]
    assert adapter.fallback_calls == 1
    assert len(model.calls) == 2
    assert adapter.observations[0][0]["tool"] == "(protocol)"
    assert json.loads(model.calls[1]["messages"][-1]["content"])["must_submit"] is True


@pytest.mark.asyncio
async def test_validation_errors_cannot_be_accepted_even_if_an_adapter_also_returns_a_result():
    class ConflictingAdapter(ComponentAdapter):
        def finalize(self, decision):
            return {"action": "change_execution", "content": "unsafe"}, ["scope validation failed"]

    model = ScriptedEditorLLM([{"type": "final", "decision": {}}])
    log = []
    adapter = ConflictingAdapter()

    assert await run_editor_loop(model, adapter=adapter, system="editor", first_user="evidence",
                                 max_rounds=1, round_log=log) is None
    assert log == [{"round": 1, "type": "final_rejected", "errors": ["scope validation failed"]}]
    assert adapter.fallback_calls == 1


@pytest.mark.asyncio
async def test_assistant_protocol_turns_are_opt_in_and_trace_options_are_pinned_without_mutation():
    evidence = "原始评价与上下文" * 500
    model = ScriptedEditorLLM([
        {"type": "plan", "action_candidate": "change_execution"},
        {"type": "tool_calls", "calls": [{"tool": "replace_component", "args": {
            "component_id": "business-prompt", "content": "clarified",
        }}]},
        {"type": "final", "decision": {"component_id": "business-prompt"}},
    ])
    trace = {"trace_name": "pinned-editor", "trace_metadata": {"revision": 7}}
    options = {"max_tokens": 123, "temperature": 0.2}
    dumps = []

    result = await run_editor_loop(
        model, adapter=ComponentAdapter(), system="editor", first_user=evidence,
        call_options=options, trace_kwargs=trace, append_assistant=True,
        debug_dump=lambda *args: dumps.append(args),
    )

    assert result["content"] == "clarified"
    assert all(call["messages"][1]["content"] == evidence for call in model.calls)
    assert [call["options"]["trace_metadata"]["round"] for call in model.calls] == [1, 2, 3]
    assert [sum(m["role"] == "assistant" for m in call["messages"]) for call in model.calls] == [0, 1, 2]
    assert trace == {"trace_name": "pinned-editor", "trace_metadata": {"revision": 7}}
    assert options == {"max_tokens": 123, "temperature": 0.2}
    assert dumps[0] == (0, "editor", evidence, None)
    assert dumps[1][2] == evidence
