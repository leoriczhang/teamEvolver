"""Variable Context edits use original input ownership and the shared editor loop."""
from __future__ import annotations

import copy
import json

import pytest

from team_replay.execution_plan import ExecutionPlanError, render_components
from team_replay.variable_context import source_value_hash, variable_context_groups
from team_skills.execution.contracts import apply_changes
from team_skills.execution.editor import PromptEditorAdapter
from team_skills.execution.planner import prompt_editor_input, propose_prompt
from team_skills.execution.writer_runtime import editor_messages
from tests.support.shared_template_sources import shared_template_plan, shared_template_source

INVITATION = "收到邀约先拒绝"
PURCHASE = "购买请求中的 {{literal_example}} 仅是参考原文"


def baseline_plan():
    plan = shared_template_plan()
    for order, (cid, value) in enumerate((("invitation", INVITATION), ("purchase", PURCHASE)), 1):
        plan["components"].append({
            "component_id": cid, "kind": "context_fragment", "slot": "context", "order": order,
            "revision": "1", "content": value, "content_hash": source_value_hash(value),
            "binding": {"inline_variable": {"component_id": "main-prompt", "name": "aiTask",
                                            "source_value_hash": source_value_hash(value)}},
        })
    plan["environment"]["evolution"] = {"editable_component_ids": ["invitation", "purchase"]}
    return plan


def original_source(plan, index, value):
    source = shared_template_source(index, plan=plan)
    turn = source["turns"][0]
    inputs = turn["request_context"]["trace_input"]["template_inputs"]
    inputs["template_variables"]["aiTask"] = value
    effective = copy.deepcopy(plan)
    effective["environment"]["template_variables"] = copy.deepcopy(inputs["template_variables"])
    turn["request_context"]["generation_requests"][0]["input"]["messages"] = [
        {"role": part["role"], "content": part["content"]} for part in render_components(effective)
    ] + copy.deepcopy(inputs["input_messages"]) + [{"role": "user", "content": turn["prompt_text"]}]
    return source


def materials():
    baseline = baseline_plan()
    sources = [original_source(baseline, index, value)
               for index, value in enumerate((INVITATION, PURCHASE, ""), 1)]
    return baseline, sources


def diagnosis(component="invitation", ref="trace-1"):
    return {"type": "plan", "action_candidate": "change_execution", "rationale": "Recorded rule ambiguity",
            "evidence_classification": {"business_prompt": [{
                "component_id": component, "problem": "The original task condition needs clarification",
                "proposed_change": "Clarify the condition without changing other task values",
                "existing_guidance": "", "evidence_refs": [ref],
            }]}}


def edit(component="invitation", ref="trace-1", old="先拒绝", new="首次收到明确邀约时先拒绝"):
    return {"tool": "propose_component_edits", "args": {
        "component_id": component, "reason": "Clarify the recorded rule only", "evidence_refs": [ref],
        "edits": [{"operation": "replace", "old_string": old, "new_string": new}],
    }}


async def tools(editor, *calls):
    return await editor.tools(list(calls))


def test_writer_attributes_each_verified_trace_to_its_original_variable_asset_without_mutating_sources():
    baseline, sources = materials()
    original = copy.deepcopy((baseline, sources))
    system, payload = prompt_editor_input(sources, {"baseline_execution_plan": baseline})
    active = {row["source_ref"]["trace_id"]: row["active_component_ids"]
              for row in payload["component_source_bindings"]}
    assert active == {"trace-1": ["invitation", "main-prompt"],
                      "trace-2": ["main-prompt", "purchase"], "trace-3": ["main-prompt"]}
    assert payload["component_evidence_refs"]["invitation"] == ["source-1", "trace-1"]
    assert payload["component_evidence_refs"]["purchase"] == ["source-2", "trace-2"]
    assert payload["editable_component_ids"] == ["invitation", "purchase"]
    assert "原始变量值匹配" in system
    assert payload["evidence"][0]["turns"][0]["messages"][-1]["content"] == "PRIVATE_OLD_GRADE"
    assert (baseline, sources) == original


def test_component_attribution_rejects_source_values_not_matching_the_recorded_request():
    baseline, sources = materials()
    variables = sources[0]["turns"][0]["request_context"]["trace_input"]["template_inputs"]["template_variables"]
    variables["aiTask"] = PURCHASE
    with pytest.raises(ExecutionPlanError, match="recorded model request"):
        prompt_editor_input(sources, {"baseline_execution_plan": baseline})


def test_context_only_trace_is_not_promoted_to_an_active_component_evidence_reference():
    baseline, sources = materials()
    current = sources[0]
    current["_evidence_turns"] = copy.deepcopy(current["turns"])
    current["turns"].extend(copy.deepcopy(sources[1]["turns"]))
    _, payload = prompt_editor_input([current], {"baseline_execution_plan": baseline})
    assert [r["source_ref"]["trace_id"] for r in payload["component_source_bindings"]] == ["trace-1"]
    assert payload["component_evidence_refs"]["purchase"] == []


@pytest.mark.asyncio
async def test_registered_readonly_main_template_cannot_be_reenabled_by_an_editor_argument():
    baseline, sources = materials()
    _, payload = prompt_editor_input(sources, {"baseline_execution_plan": baseline})
    editor = PromptEditorAdapter(payload, editable_component_ids=["main-prompt", "invitation"])
    assert editor.tools_state.editable == {"invitation"}
    rejected = editor.plan(diagnosis("main-prompt"), round_no=1, max_rounds=6)
    assert rejected.log_kind == "plan_rejected"
    result = await tools(editor, {"tool": "read_component", "args": {"component_id": "main-prompt"}},
                         edit("main-prompt"))
    assert result[0]["ok"] and not result[0]["editable"]
    assert not result[1]["ok"] and "editable scope" in result[1]["error"]
    assert editor.tools_state.working == payload["execution_plan"]


@pytest.mark.asyncio
async def test_inactive_asset_and_empty_value_references_cannot_support_a_component_edit():
    baseline, sources = materials()
    _, payload = prompt_editor_input(sources, {"baseline_execution_plan": baseline})
    editor = PromptEditorAdapter(payload)
    editor.plan(diagnosis(), round_no=1, max_rounds=6)
    for ref in ("trace-2", "trace-3", "source-2", "source-3"):
        rejected = editor.plan(diagnosis(ref=ref), round_no=2, max_rounds=6)
        assert rejected.log_kind == "plan_rejected"
    await tools(editor, {"tool": "read_component", "args": {"component_id": "invitation"}})
    bad = (await tools(editor, edit(ref="trace-2")))[0]
    assert not bad["ok"] and "does not activate" in bad["error"]
    assert editor.tools_state.applied_edits == 0
    assert editor.tools_state.plan_claims["invitation"] == {"trace-1"}
    good = (await tools(editor, edit()))[0]
    assert good["ok"]
    assert editor.finalize({"action": "change_execution", "rationale": "Supported condition clarification"})[0]


@pytest.mark.asyncio
async def test_literal_fragment_braces_are_not_readonly_template_placeholders():
    baseline, sources = materials()
    _, payload = prompt_editor_input(sources, {"baseline_execution_plan": baseline})
    editor = PromptEditorAdapter(payload)
    editor.plan(diagnosis("purchase", "trace-2"), round_no=1, max_rounds=6)
    result = await tools(editor, {"tool": "read_component", "args": {"component_id": "purchase"}},
                         edit("purchase", "trace-2", "{{literal_example}}", "字面示例"))
    assert result[-1]["ok"]
    final, errors = editor.finalize({"action": "change_execution", "rationale": "Clarify literal reference"})
    assert not errors and final["component_changes"][0]["component_id"] == "purchase"
    assert editor.tools_state.working["components"][0] == baseline["components"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_group_plan", [False, True])
async def test_continuation_uses_old_selector_and_existing_feedback_in_the_shared_edit_loop(use_group_plan):
    baseline, sources = materials()
    pending = apply_changes(baseline, [{
        "component_id": "invitation", "content": INVITATION + "，保留原始列表含义。",
        "reason": "Previous supported candidate", "evidence_refs": ["prior-trace"],
    }], allowed_modules=["prompt"])
    feedback = {"candidate_revision": 2, "cases": [{"query": "Original development question",
                  "candidate": {"response": "Original public Replay answer", "reason": "Original check result"}}]}
    context = {"baseline_execution_plan": baseline, "working_execution_plan": pending,
               "active_candidate": {"candidate_id": "same-candidate", "revision": 2},
               "active_candidate_replay_feedback": feedback}
    original = copy.deepcopy((baseline, sources, context))
    planned = diagnosis()
    read_call = {"tool": "read_component", "args": {"component_id": "invitation"}}
    if use_group_plan:
        gid = variable_context_groups(pending)[0]["group_id"]
        claim = planned["evidence_classification"]["business_prompt"][0]
        claim["variable_group_id"] = gid
        claim["members"] = [{key: claim.pop(key) for key in ("component_id", "existing_guidance", "evidence_refs")}]
        read_call = {"tool": "read_variable_group", "args": {"group_id": gid, "component_id": "invitation"}}

    class Model:
        def __init__(self):
            self.calls = []
            self.answers = iter([
                planned,
                {"type": "tool_calls", "calls": [
                    read_call,
                    edit(old="先拒绝", new="首次明确收到邀约时先拒绝"),
                ]},
                {"type": "tool_calls", "calls": [{"tool": "validate_changes", "args": {}}]},
                {"type": "final", "decision": {"action": "change_execution", "rationale": "Source-led edit"}},
            ])

        async def chat(self, messages, **options):
            self.calls.append(copy.deepcopy(messages))
            return json.dumps(next(self.answers), ensure_ascii=False)

    model = Model()
    result = await propose_prompt(model, sessions=sources, context=context, max_rounds=6)
    assert result["action"] == "change_execution" and len(model.calls) == 4
    assert result["component_changes"][0]["component_id"] == "invitation"
    assert "首次明确收到邀约时先拒绝" in result["component_changes"][0]["content"]
    assert result["editor_audit"]["applied_edits"] == 1
    _, payload = prompt_editor_input(sources, context)
    assert payload["active_candidate_replay_feedback"] == feedback
    assert payload["component_evidence_refs"]["invitation"] == ["source-1", "trace-1"]
    assert (baseline, sources, context) == original


def test_group_identity_and_version_are_per_variable_not_other_runtime_values_or_hosts():
    plan = baseline_plan()
    original = copy.deepcopy(plan)
    group = variable_context_groups(plan)[0]
    assert plan == original
    assert group["group_id"] == f"variable-group:{plan['binding']['profile_id']}:aiTask"
    assert group["value_count"] == 2
    assert {m["component_id"] for m in group["members"]} == {"invitation", "purchase"}
    plan["environment"]["template_variables"]["userProfile"] = "Another original runtime value"
    assert variable_context_groups(plan) == [group]
    other = copy.deepcopy(plan["components"][-1])
    other.update(component_id="personality-member", content="Original personality rule")
    other["content_hash"] = source_value_hash(other["content"])
    other["binding"]["inline_variable"].update(name="userPersonality", source_value_hash=source_value_hash("careful"))
    plan["components"].append(other)
    groups = variable_context_groups(plan)
    assert len(groups) == 2 and groups[0] == group
    host_member = copy.deepcopy(plan["components"][1])
    host_member["component_id"] = "other-host-invitation"
    host_member["binding"]["inline_variable"]["component_id"] = "other-host"
    plan["components"].append(host_member)
    expanded = variable_context_groups(plan)[0]
    assert expanded["group_id"] == group["group_id"] and expanded["value_count"] == 2
    assert {m["host_component_id"] for m in expanded["members"]} == {"main-prompt", "other-host"}
    assert expanded["version_hash"] != group["version_hash"]


def test_initial_group_view_defers_only_member_bodies_and_keeps_original_payload_complete():
    baseline, sources = materials()
    system, payload = prompt_editor_input(sources, {"baseline_execution_plan": baseline})
    original = copy.deepcopy(payload)
    visible = json.loads(editor_messages(system, payload)[1]["content"])
    assert visible["variable_groups"][0]["value_count"] == 2
    assert "content" not in visible["execution_plan"]["components"][1]
    assert visible["execution_plan"]["components"][0]["content"] == baseline["components"][0]["content"]
    assert visible["evidence"] == payload["evidence"]
    assert payload == original
    legacy = {"execution_plan": shared_template_plan(), "evidence": []}
    assert json.loads(editor_messages("Original instructions", legacy)[1]["content"]) == legacy
    legacy_editor = PromptEditorAdapter({**legacy, "allowed_evidence_refs": []})
    assert "read_variable_group" not in legacy_editor.tools_state.available_tools


@pytest.mark.asyncio
async def test_group_diagnosis_reads_and_edits_only_the_selected_evidence_bound_member():
    baseline, sources = materials()
    _, payload = prompt_editor_input(sources, {"baseline_execution_plan": baseline})
    editor = PromptEditorAdapter(payload)
    gid = payload["variable_groups"][0]["group_id"]
    plan = diagnosis()
    claim = plan["evidence_classification"]["business_prompt"][0]
    claim["variable_group_id"] = gid
    claim["members"] = [{key: claim.pop(key) for key in ("component_id", "existing_guidance", "evidence_refs")}]
    assert editor.plan(plan, round_no=1, max_rounds=6).log_kind != "plan_rejected"
    directory = (await tools(editor, {"tool": "read_variable_group", "args": {"group_id": gid}}))[0]
    assert directory["ok"] and directory["content_deferred"]
    assert all("content" not in member for member in directory["variable_group"]["members"])
    assert editor.tools_state.read_current == set()
    unread = (await tools(editor, edit()))[0]
    assert not unread["ok"]
    result = await tools(editor, {"tool": "read_variable_group", "args": {
        "group_id": gid, "component_id": "invitation"}}, edit())
    assert result[0]["component"]["content"] == INVITATION and result[1]["ok"]
    final, errors = editor.finalize({"action": "change_execution", "rationale": "Clarify one member"})
    assert not errors and [row["component_id"] for row in final["component_changes"]] == ["invitation"]
    assert editor.tools_state.working["components"][2] == baseline["components"][2]
    assert editor.tools_state.working["components"][1]["binding"] == baseline["components"][1]["binding"]
    changed_group = variable_context_groups(editor.tools_state.working)[0]
    assert changed_group["version_hash"] != payload["variable_groups"][0]["version_hash"]
    bad = copy.deepcopy(plan)
    bad["evidence_classification"]["business_prompt"][0]["members"][0]["evidence_refs"] = ["trace-2"]
    assert editor.plan(bad, round_no=2, max_rounds=6).log_kind == "plan_rejected"
    bad["evidence_classification"]["business_prompt"][0]["members"][0]["component_id"] = "main-prompt"
    assert editor.plan(bad, round_no=2, max_rounds=6).log_kind == "plan_rejected"
    wrong = (await tools(editor, {"tool": "read_variable_group", "args": {
        "group_id": gid, "component_id": "main-prompt"}}))[0]
    assert not wrong["ok"] and "outside" in wrong["error"]
