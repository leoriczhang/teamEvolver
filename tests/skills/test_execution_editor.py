"""Body editing reuses the Skill loop without its package-specific contract."""
from __future__ import annotations

import copy
import json

import pytest

from team_skills.evolution.agent.loop import run_editor_loop
from team_skills.evolution.agent.runner import run_skill_agent
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.editor import EDITOR_PROTOCOL, PromptEditorAdapter
from team_skills.execution.editor_tools import canonical
from teamEvolver.evidence_transport import TRANSPORT_INSTRUCTIONS, decode_evidence_transport
from tests.support.execution import profile_raw


def payload():
    raw = profile_raw()
    raw["components"][0]["content"] = "Keep the business condition.\nOriginal business Prompt\nRetain {{domain}}.\n"
    raw["environment"]["template_variables"] = {"domain": "existing business"}
    profile = validate_profile(raw)
    plan = plan_for(profile, profile["components"])
    return {"execution_plan": plan, "allowed_evidence_refs": ["session-one", "trace-one"],
            "evidence": [{"session_id": "session-one", "turns": [{"source_trace_id": "trace-one",
                          "request_context": {"generation_requests": [{"input": {"unclipped": "原文"}}]},
                          "messages": [{"role": "user", "content": "原始请求"},
                                       {"role": "assistant", "content": "原回答"},
                                       {"role": "user", "content": "原评分与详细原因"}]}]}],
            "evidence_context": {"examples": [], "coverage": {"included_count": 1}}}


def plan():
    return {"type": "plan", "action_candidate": "change_execution", "rationale": "A repeated business defect",
            "evidence_classification": {"business_prompt": [{"component_id": "main-prompt",
                "problem": "Ambiguous guidance", "proposed_change": "Clarify only the recorded defect",
                "existing_guidance": "Original business Prompt", "evidence_refs": ["trace-one"]}]}}


def patch(old="Original business Prompt", new="Clarified business Prompt"):
    return {"tool": "propose_component_edits", "args": {"component_id": "main-prompt",
            "edits": [{"operation": "replace", "old_string": old, "new_string": new}],
            "reason": "Resolve observed ambiguity", "evidence_refs": ["trace-one"]}}


def act(*calls):
    return {"type": "tool_calls", "calls": list(calls)}


FINAL = {"type": "final", "decision": {"action": "change_execution", "rationale": "Precise source-backed edit"}}


class FakeModel:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.calls = []

    async def chat(self, messages, **options):
        self.calls.append(copy.deepcopy(messages))
        return json.dumps(next(self.turns), ensure_ascii=False)


@pytest.mark.asyncio
async def test_editor_corrects_a_bad_exact_patch_and_submits_without_an_independent_review():
    frozen = payload()
    original = copy.deepcopy(frozen)
    adapter = PromptEditorAdapter(frozen)
    model = FakeModel([
        plan(),
        act({"tool": "read_component", "args": {"component_id": "main-prompt"}},
            {"tool": "read_evidence", "args": {"ref": "trace-one"}}),
        act(patch("Hallucinated anchor")),
        act(patch()),
        act({"tool": "inspect_changes", "args": {}}, {"tool": "validate_changes", "args": {}}),
        FINAL,
    ])
    result = await run_editor_loop(model, adapter=adapter, system=EDITOR_PROTOCOL,
                                   first_user=json.dumps(frozen), append_assistant=True)
    assert result["action"] == "change_execution" and len(model.calls) == 6
    assert result["component_changes"][0]["content"] == original["execution_plan"]["components"][0]["content"].replace(
        "Original business Prompt", "Clarified business Prompt",
    )
    patch_error = json.loads(model.calls[3][-1]["content"])["observations"][0]
    assert not patch_error["ok"] and "anchor" in patch_error["error"]
    checked = json.loads(model.calls[-1][-1]["content"])["observations"][-1]
    assert checked["ok"] and checked["mechanical_only"] and checked["mechanical_passed"]
    assert "business_contract_review" not in checked and "business_review_hash" not in adapter.audit()
    assert adapter.tools_state.working["components"][0]["revision"] == 2
    assert frozen == original


@pytest.mark.asyncio
async def test_round_exhaustion_preserves_a_valid_staged_prompt_like_skill_best_effort():
    frozen = payload()
    original = copy.deepcopy(frozen)
    adapter = PromptEditorAdapter(frozen)
    model = FakeModel([
        plan(),
        act({"tool": "read_component", "args": {"component_id": "main-prompt"}}, patch()),
        act({"tool": "validate_changes", "args": {}}),
    ])
    result = await run_editor_loop(model, adapter=adapter, system=EDITOR_PROTOCOL,
                                   first_user=json.dumps(frozen), max_rounds=3, append_assistant=True)
    old = frozen["execution_plan"]["components"][0]["content"]
    skill = await run_skill_agent(
        FakeModel([
            {"type": "plan", "action_candidate": "improve_skill", "rationale": "Reusable correction",
             "evidence_classification": {"team_skill": [{"supporting_session_ids": ["session-one"]}]}},
            act({"tool": "read_skill", "args": {}}, {"tool": "propose_edits", "args": {"edits": [
                {"operation": "replace", "old_string": "Original business Prompt",
                 "new_string": "Clarified business Prompt"},
            ]}}),
            act({"tool": "check_generalization", "args": {}}),
        ]), stage="evolve_skill", system_prompt="Reuse the recorded correction", skill_name="demo",
        current_skill={"name": "demo", "content": old, "description": "Reusable business guidance"},
        sessions=frozen["evidence"], max_rounds=3,
    )
    assert len(model.calls) == 3 and adapter.tools_state.applied_edits == 1
    assert result["action"] == "change_execution" and skill["action"] == "improve_skill"
    assert result["component_changes"][0]["content"] == skill["skill"]["content"]
    explicit, errors = adapter.finalize(FINAL["decision"])
    assert not errors and result["component_changes"] == explicit["component_changes"]
    assert result["component_changes"][0]["evidence_refs"] == ["trace-one"]
    assert adapter.tools_state.working["binding"] == frozen["execution_plan"]["binding"]
    assert adapter.tools_state.working["environment"] == frozen["execution_plan"]["environment"]
    assert adapter.tools_state.working["components"][1] == frozen["execution_plan"]["components"][1]
    assert frozen == original


@pytest.mark.asyncio
async def test_round_exhaustion_keeps_the_last_valid_atomic_edit_after_a_failed_patch():
    adapter = PromptEditorAdapter(payload())
    invalid_batch = patch("Clarified business Prompt", "An uncommitted replacement")
    invalid_batch["args"]["edits"].append({"operation": "replace", "old_string": "Missing anchor",
                                           "new_string": "Another uncommitted replacement"})
    model = FakeModel([
        plan(),
        act({"tool": "read_component", "args": {"component_id": "main-prompt"}}, patch()),
        act(invalid_batch),
    ])
    result = await run_editor_loop(model, adapter=adapter, system=EDITOR_PROTOCOL,
                                   first_user="frozen", max_rounds=3, append_assistant=True)
    assert result["action"] == "change_execution" and adapter.tools_state.applied_edits == 1
    content = result["component_changes"][0]["content"]
    assert "Clarified business Prompt" in content and "uncommitted replacement" not in content
    assert result["component_changes"] == adapter.tools_state.changes()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["no_plan", "no_edit", "failed_edit", "reverted_edit"])
async def test_round_exhaustion_does_not_create_a_candidate_without_valid_real_changes(state):
    adapter = PromptEditorAdapter(payload())
    if state != "no_plan":
        adapter.plan(plan(), round_no=1, max_rounds=3)
    await adapter.tools([{"tool": "read_component", "args": {"component_id": "main-prompt"}}])
    if state in {"no_plan", "failed_edit"}:
        result = await adapter.tools([patch("Missing anchor") if state == "failed_edit" else patch()])
        assert not result[0]["ok"]
    elif state == "reverted_edit":
        assert (await adapter.tools([patch()]))[0]["ok"]
        assert (await adapter.tools([patch("Clarified business Prompt", "Original business Prompt")]))[0]["ok"]
    assert not adapter.tools_state.changes()
    assert adapter.fallback()["action"] == "skip"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["binding", "variables", "references", "plan_support"])
async def test_round_exhaustion_revalidates_mechanical_protection_before_preserving_staged_edits(mutation):
    adapter = PromptEditorAdapter(payload())
    adapter.plan(plan(), round_no=1, max_rounds=3)
    assert all(item["ok"] for item in await adapter.tools([
        {"tool": "read_component", "args": {"component_id": "main-prompt"}}, patch(),
    ]))
    tools = adapter.tools_state
    if mutation == "binding":
        tools.working["binding"]["agent_id"] = "another-agent"
    elif mutation == "variables":
        tools.working["environment"]["template_variables"]["domain"] = "an invented business value"
    elif mutation == "references":
        tools.references["main-prompt"] = ["not-an-original-target"]
    else:
        tools.plan_claims["main-prompt"].clear()
    explicit, errors = adapter.finalize(FINAL["decision"])
    assert explicit is None and errors
    assert tools.applied_edits == 1 and adapter.fallback()["action"] == "skip"


@pytest.mark.asyncio
async def test_final_whole_rewrite_cannot_bypass_plan_read_and_patch():
    adapter = PromptEditorAdapter(payload())
    invalid = {"action": "change_execution", "rationale": "Claim success",
               "component_changes": [{"component_id": "main-prompt", "content": "Whole rewrite"}]}
    result, errors = adapter.finalize(invalid)
    assert result is None and errors
    model = FakeModel([{"type": "final", "decision": invalid}] * 2)
    result = await run_editor_loop(model, adapter=adapter, system=EDITOR_PROTOCOL, first_user="frozen", max_rounds=2)
    assert result["action"] == "skip"
    assert adapter.tools_state.applied_edits == 0


@pytest.mark.asyncio
async def test_invalid_plan_cannot_use_skill_evidence_bucket_or_unknown_quote():
    adapter = PromptEditorAdapter(payload())
    invalid = plan()
    invalid["evidence_classification"] = {"team_skill": [{"evidence_refs": ["trace-one"]}]}
    rejected = adapter.plan(invalid, round_no=1, max_rounds=12)
    assert rejected.log_kind == "plan_rejected"
    invalid = plan()
    invalid["evidence_classification"]["business_prompt"][0]["existing_guidance"] = "Never recorded"
    rejected = adapter.plan(invalid, round_no=2, max_rounds=12)
    assert rejected.log_kind == "plan_rejected"
    observations = await adapter.tools([{"tool": "read_component", "args": {"component_id": "main-prompt"}},
                                        patch()])
    assert observations[0]["ok"] and not observations[1]["ok"]
    assert not adapter.tools_state.changes()


@pytest.mark.asyncio
async def test_patch_batch_rolls_back_and_templates_are_render_validated():
    adapter = PromptEditorAdapter(payload())
    adapter.plan(plan(), round_no=1, max_rounds=12)
    await adapter.tools([{"tool": "read_component", "args": {"component_id": "main-prompt"}}])
    bad = patch()
    bad["args"]["edits"].append({"operation": "replace", "old_string": "Absent", "new_string": "Wrong"})
    results = await adapter.tools([bad])
    assert not results[0]["ok"]
    assert not adapter.tools_state.changes()
    bad = patch(new="Clarified business Prompt {{unsupported.expression}}")
    results = await adapter.tools([bad])
    assert not results[0]["ok"]
    assert not adapter.tools_state.changes()
    good = await adapter.tools([patch()])
    assert good[0]["ok"]
    assert adapter.tools_state.working["components"][1] == payload()["execution_plan"]["components"][1]


@pytest.mark.asyncio
async def test_final_validates_the_latest_staged_combination_and_rejects_out_of_band_metadata_changes():
    adapter = PromptEditorAdapter(payload())
    adapter.plan(plan(), round_no=1, max_rounds=12)
    await adapter.tools([{"tool": "read_component", "args": {"component_id": "main-prompt"}}, patch()])
    first_hash = adapter.audit()["staged_hash"]
    assert adapter.finalize(FINAL["decision"])[0]
    await adapter.tools([patch("Clarified business Prompt", "Another specific correction")])
    result, errors = adapter.finalize(FINAL["decision"])
    assert result and not errors and "Another specific correction" in result["component_changes"][0]["content"]
    assert adapter.audit()["staged_hash"] != first_hash
    adapter.tools_state.working["binding"]["agent_id"] = "unapproved-agent"
    assert not (await adapter.tools([{"tool": "validate_changes", "args": {}}]))[0]["ok"]
    assert adapter.finalize(FINAL["decision"])[0] is None


def test_large_observation_shares_repeated_original_strings_losslessly_and_small_is_unchanged():
    adapter = PromptEditorAdapter(payload())
    original = {"tool": "read_evidence", "ok": True, "complete": True, "records": [
        {"request": "完整请求与原始评分的详细原因：\n" * 10000,
         "generation": {"original_wire_observed": False, "nested": [None, True, 2, "原文"]}}
        for _ in range(4)
    ]}
    raw = adapter.observe([original], round_no=2, max_rounds=12)
    assert raw.startswith(TRANSPORT_INSTRUCTIONS)
    envelope = json.loads(raw[len(TRANSPORT_INSTRUCTIONS):])
    restored = decode_evidence_transport(envelope, max_bytes=8 * 1024 * 1024)
    assert restored["observations"] == [original]
    assert len(raw.encode()) < len(canonical(restored).encode())
    small = adapter.observe([{"tool": "(loop)", "ok": True}], round_no=2, max_rounds=12)
    assert small == canonical(json.loads(small))
    assert not small.startswith(TRANSPORT_INSTRUCTIONS)
