"""Accepted Prompt plans authorize exact edits cumulatively within their components.

Mechanical validation verifies edit provenance, not business quality.
No historical artifacts, services or model calls are required.
"""
from __future__ import annotations

import copy

import pytest

from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.editor import EDITOR_PROTOCOL, PromptEditorAdapter
from team_skills.execution.editor_tools import sha
from tests.support.execution import profile_raw


def _payload():
    raw = profile_raw()
    raw["components"][0]["content"] = (
        "Keep the original role and conditions.\n{{domain}}\n"
        "## Repeated instructions\nReturn one reply.\nReturn only one reply.\n"
        "## Unique condition\nUse the original time window.\n"
    )
    raw["environment"]["template_variables"] = {"domain": "Original domain"}
    profile = validate_profile(raw)
    return {"execution_plan": plan_for(profile, profile["components"]), "allowed_evidence_refs": ["a", "b", "c"],
            "evidence": [{"session_id": "raw-source", "turns": [
                {"trace_id": ref, "prompt_text": "Original task " + ref, "messages": [
                    {"role": "user", "content": "Original task " + ref},
                    {"role": "assistant", "content": "Original answer"},
                    {"role": "user", "content": "Please preserve the original requirements."},
                ]} for ref in ("a", "b", "c")
            ]}], "evidence_context": {"examples": [], "coverage": {"included_count": 3}}}


def _plan(component="main-prompt", refs=("a",), *, quote="", **extra):
    return {"type": "plan", "action_candidate": "change_execution", "rationale": "Check a refinement hypothesis",
            "evidence_classification": {"business_prompt": [{"component_id": component,
                "problem": "Original wording may contain redundant guidance", "proposed_change": "Consolidate it",
                "existing_guidance": quote, "evidence_refs": list(refs), **extra}]}}


def _accept(editor, plan=None, *, round_no=1):
    result = editor.plan(_plan() if plan is None else plan, round_no=round_no, max_rounds=20)
    assert result.log_kind != "plan_rejected"
    return result


async def _tool(editor, name, **args):
    return (await editor.tools([{"tool": name, "args": args}]))[0]


async def _patch(editor, old="Return only one reply.\n", new="", refs=("a",), component="main-prompt"):
    return await _tool(editor, "propose_component_edits", component_id=component, evidence_refs=list(refs),
                       reason="Semantics-preserving refinement hypothesis", edits=[{
                           "operation": "replace", "old_string": old, "new_string": new,
                       }])


def _state(editor):
    tools = editor.tools_state
    return copy.deepcopy((editor.plan_record, editor.plan_round, tools.plan_claims, tools.diagnoses,
                          tools.plan_history, tools.edit_provenance, tools.working, tools.references,
                          tools.reasons))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["quote", "unknown_ref", "other_component", "bad_summary", "invalid_tail"])
async def test_invalid_replan_is_atomic_and_preserves_accepted_edits_and_source_support(mutation):
    editor = PromptEditorAdapter(_payload())
    _accept(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert (await _patch(editor))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    before = _state(editor)
    invalid = _plan(refs=("b",))
    claim = invalid["evidence_classification"]["business_prompt"][0]
    if mutation == "quote":
        claim["existing_guidance"] = "A paraphrase that never appeared in the original body"
    elif mutation == "unknown_ref":
        claim["evidence_refs"] = ["context:or-invented-reference"]
    elif mutation == "other_component":
        claim["component_id"] = "not-frozen"
    elif mutation == "bad_summary":
        claim["guidance_summary"] = {"not": "text"}
    else:
        invalid["evidence_classification"]["business_prompt"].append({**claim, "component_id": "not-frozen"})
    rejected = editor.plan(invalid, round_no=7, max_rounds=20)
    assert rejected.log_kind == "plan_rejected"
    assert _state(editor) == before
    result, errors = editor.finalize({"action": "change_execution",
                                     "rationale": "Explicit final for a mechanically valid draft"})
    assert result and not errors
    fallback = editor.fallback()
    assert fallback["action"] == "change_execution"
    assert fallback["component_changes"] == result["component_changes"]


@pytest.mark.asyncio
async def test_successive_component_plans_keep_prior_staged_refs_and_record_exact_edit_support():
    editor = PromptEditorAdapter(_payload())
    _accept(editor, _plan(refs=("a",)), round_no=2)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert (await _patch(editor))["ok"]
    _accept(editor, _plan(refs=("b",), guidance_summary="A new hypothesis; no verbatim quote supplied"), round_no=5)
    assert (await _patch(editor, "Return one reply.", "Return a single reply.", refs=("b",)))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    changes = editor.tools_state.changes()
    assert changes[0]["evidence_refs"] == ["a", "b"]
    assert editor.tools_state.plan_claims["main-prompt"] == {"a", "b"}
    diagnoses = editor.tools_state.planned_diagnoses()
    assert [row["analysis_status"] for row in diagnoses] == [
        "prior_accepted_hypothesis", "current_accepted_hypothesis",
    ]
    assert [row["accepted_plan_id"] for row in diagnoses] == ["plan-1", "plan-2"]
    events = editor.audit()["edit_provenance"]
    assert [row["support"] for row in events] == [
        [{"evidence_ref": "a", "accepted_plan_ids": ["plan-1"]}],
        [{"evidence_ref": "b", "accepted_plan_ids": ["plan-2"]}],
    ]
    assert events[0]["after_content_sha256"] == events[1]["before_content_sha256"]
    assert editor.audit()["accepted_plan_history"][0]["diagnoses"][0]["analysis_status"] == (
        "prior_accepted_hypothesis"
    )


@pytest.mark.asyncio
async def test_component_local_ref_accumulation_never_borrows_authority_from_another_component():
    editor = PromptEditorAdapter(_payload())
    _accept(editor, _plan(refs=("a",)))
    _accept(editor, _plan("shared-context", refs=("b",)), round_no=3)
    for component in ("main-prompt", "shared-context"):
        await _tool(editor, "read_component", component_id=component)
    assert not (await _patch(editor, refs=("b",)))["ok"]
    assert not (await _patch(editor, "Keep", "Preserve", refs=("a",), component="shared-context"))["ok"]
    assert (await _patch(editor, refs=("a",)))["ok"]
    assert (await _patch(editor, "Keep", "Preserve", refs=("b",), component="shared-context"))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    assert editor.tools_state.plan_claims == {"main-prompt": {"a"}, "shared-context": {"b"}}


@pytest.mark.asyncio
async def test_replan_does_not_authorize_unread_components_or_unknown_refs_or_template_changes():
    editor = PromptEditorAdapter(_payload())
    _accept(editor, _plan(refs=("a", "b"), quote=""))
    assert not (await _patch(editor))["ok"]
    await _tool(editor, "read_component", component_id="main-prompt", version="baseline")
    assert not (await _patch(editor))["ok"]
    await _tool(editor, "read_component", component_id="main-prompt", version="current")
    before = _state(editor)
    assert not (await _patch(editor, refs=("unknown",)))["ok"]
    assert not (await _patch(editor, "{{domain}}", "{{new_variable}}"))["ok"]
    assert not (await _patch(editor, "{{domain}}", ""))["ok"]
    assert _state(editor) == before


@pytest.mark.asyncio
async def test_empty_plan_quote_is_not_a_missing_rule_claim_and_a_section_patch_can_remove_redundancy():
    payload = _payload()
    original = copy.deepcopy(payload)
    editor = PromptEditorAdapter(payload)
    _accept(editor, _plan(quote="", guidance_summary="Hypothesis: the repeated section can be consolidated"))
    await _tool(editor, "read_component", component_id="main-prompt")
    old = "## Repeated instructions\nReturn one reply.\nReturn only one reply.\n"
    assert (await _patch(editor, old, "## Response\nReturn one reply.\n"))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Exact scoped section consolidation"})
    assert result and not errors
    text = result["component_changes"][0]["content"]
    assert "{{domain}}" in text and "Use the original time window." in text
    assert editor.tools_state.working["components"][1] == original["execution_plan"]["components"][1]
    assert editor.tools_state.working["binding"] == original["execution_plan"]["binding"]
    assert editor.tools_state.working["environment"] == original["execution_plan"]["environment"]
    assert payload == original
    assert editor.tools_state.planned_diagnoses()[0]["existing_guidance"] == ""
    assert "未提供逐字引文，不表示原文没有规则" in EDITOR_PROTOCOL


@pytest.mark.asyncio
async def test_final_rebuilds_the_latest_patch_combination_without_another_model_gate():
    editor = PromptEditorAdapter(_payload())
    _accept(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert (await _patch(editor))["ok"]
    old_hash = (await _tool(editor, "validate_changes"))["staged_hash"]
    _accept(editor, _plan(refs=("b",)), round_no=5)
    assert (await _patch(editor, "Return one reply.", "Return a single reply.", refs=("b",)))["ok"]
    assert sha(editor.tools_state.working) != old_hash
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Submit the current exact edits"})
    assert result and not errors
    assert "Return a single reply." in result["component_changes"][0]["content"]
    assert editor.audit()["staged_hash"] == sha(editor.tools_state.working)


def test_model_cannot_spoof_server_plan_provenance_fields():
    editor = PromptEditorAdapter(_payload())
    _accept(editor, _plan(accepted_plan_id="fake", accepted_round=-1, analysis_status="proven_fact"), round_no=4)
    diagnosis = editor.tools_state.diagnoses[0]
    assert diagnosis["accepted_plan_id"] == "plan-1" and diagnosis["accepted_round"] == 4
    assert diagnosis["analysis_status"] == "current_accepted_hypothesis"
