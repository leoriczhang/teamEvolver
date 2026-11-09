"""Independent contracts for Prompt-specific adaptation of the shared editor.

These tests exercise frozen Evidence, component scope and correction behavior;
they never call a business Agent, provider, database or Replay Judge.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.editor import PromptEditorAdapter
from team_skills.execution.editor_tools import component_size_summary
from team_skills.execution.planner import propose_prompt, raw_evidence
from teamEvolver.evidence_context import build_evidence_context
from tests.support.execution import profile_raw
from tests.support.prompt_editor import PromptEditorLLM


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _payload(*, long_record=False):
    raw = profile_raw()
    raw["affected_skill_ids"] = []
    raw["components"][0]["content"] = "Keep this policy.\nFIXME {{business}}\nRetain this last line.\n"
    raw["components"][1]["content"] = "Unchanged shared policy: é / e\u0301 / 中文\r\n"
    raw["environment"]["template_variables"] = {"business": "frozen business value"}
    profile = validate_profile(raw)
    plan = plan_for(profile, profile["components"])
    declaration = {"constructed_import_batch": True, "conversation_relationship": "independent_samples",
                   "actual_original_request_observed": False, "original_session_independence": "unknown"}

    def turn(trace, *, comparison=False):
        feedback = "model_eval_feedback: " + json.dumps({
            "score": 100 if comparison else 0,
            "reason": "保留原文、用户纠正与来源；不得把外部机器评分冒充人为验收。",
            "raw_detail": ("原始评分理由。" * 8000 if long_record and not comparison else "positive comparison"),
        }, ensure_ascii=False)
        return {"source_trace_id": trace, "trace_id": trace, "turn_num": 1,
                "prompt_text": "Recorded original request " + trace,
                "response_text": "Recorded original answer " + trace,
                "messages": [{"role": "assistant", "content": "Recorded original answer " + trace},
                             {"role": "user", "content": feedback}],
                "_langfuse": {"tags": ["machine-evaluation-feedback"]},
                "request_context": {
                    "trace_input": {"reconstruction": declaration, "business_context": {"ctx_mode": "fixed"}},
                    "generation_requests": [{"input": {"reconstruction": declaration,
                        "messages": [{"role": "system", "content": "Observed template value"},
                                     {"role": "user", "content": "Original wire-shaped query"}],
                        "provider_fields": {"unfamiliar_array": [None, False, 1, "TAIL_PROVIDER_FIELD"]}}}],
                }}

    target, control = turn("target-a"), turn("context-a", comparison=True)
    ref = lambda trace: {"source_session_key": "source-a", "trace_id": trace,
                         "revision": "record-revision", "context_revision": "context-revision"}
    session = {"session_id": "formal-a", "turns": [target], "_evidence_turns": [target],
               "provenance": {"schema_version": "session-source.v1", "source_session_key": "source-a",
                   "identity_verified": True, "trace_identity_verified": True,
                   "target_trace_refs": [ref("target-a")], "context_trace_refs": [ref("context-a")]},
               "analysis_context": {"context_only": True, "turns": [target, control],
                                    "context_trace_refs": [ref("context-a")]}}
    packet = build_evidence_context([session], include_content=False)
    assert len(packet["context_examples"]) == 1
    return {"execution_plan": plan, "evidence": raw_evidence([session]),
            "evidence_context": packet, "allowed_evidence_refs": ["formal-a", "target-a"]}


def _plan(editor, *, ref="target-a", component="main-prompt", refs=None):
    return editor.plan({"action_candidate": "change_execution", "rationale": "Resolve a recorded defect",
                        "evidence_classification": {"business_prompt": [{
                            "component_id": component, "problem": "A local instruction was unclear",
                            "existing_guidance": "", "proposed_change": "Clarify the affected line only",
                            "evidence_refs": refs or [ref],
                        }]}}, round_no=1, max_rounds=12)


async def _tool(editor, name, **args):
    results = await editor.tools([{"tool": name, "args": args}])
    assert len(results) == 1
    return results[0]


def _editor(payload=None, **kwargs):
    return PromptEditorAdapter(payload or _payload(), **kwargs)


async def _patch(editor, old="FIXME", new="Clarified instruction", *, refs=None, **extra):
    return await _tool(editor, "propose_component_edits", component_id="main-prompt",
                       edits=[{"operation": "replace", "old_string": old, "new_string": new}],
                       reason="Keep original business meaning while fixing this recorded local defect",
                       evidence_refs=refs or ["target-a"], **extra)


@pytest.mark.asyncio
async def test_full_evidence_keeps_large_original_feedback_and_unknown_request_fields():
    payload = _payload(long_record=True)
    original = copy.deepcopy(payload)
    editor = _editor(payload)
    result = await _tool(editor, "read_evidence", ref="target-a")
    assert result["ok"] and result["complete"] and not result["has_more"]
    document = result["value"]
    recorded = document["records"][0]["recorded_turn"]
    assert recorded == payload["evidence"][0]["turns"][0]
    assert len(recorded["messages"][-1]["content"]) > 36000
    assert recorded["request_context"]["generation_requests"][0]["input"]["provider_fields"] == {
        "unfamiliar_array": [None, False, 1, "TAIL_PROVIDER_FIELD"],
    }
    assert result["full_sha256"] == hashlib.sha256(_canonical(document).encode()).hexdigest()
    assert result["full_utf8_bytes"] == len(_canonical(document).encode())
    assert payload == original


@pytest.mark.asyncio
async def test_explicit_pages_reconstruct_the_exact_document_without_claiming_partial_pages_are_full():
    editor = _editor(_payload(long_record=True))
    full = await _tool(editor, "read_evidence", ref="target-a")
    offset, pieces = 0, []
    while True:
        page = await _tool(editor, "read_evidence", ref="target-a", offset=offset, limit=7000)
        assert page["ok"] and page["full_sha256"] == full["full_sha256"]
        assert page["offset_units"] == "characters"
        assert not page["complete"]
        pieces.append(page["text"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert json.loads("".join(pieces)) == full["value"]


@pytest.mark.asyncio
async def test_context_is_readable_as_comparison_but_never_becomes_a_defect_reference():
    payload = _payload()
    context_ref = "context:" + payload["evidence_context"]["context_examples"][0]["example_id"]
    editor = _editor(payload)
    comparison = await _tool(editor, "read_evidence", ref=context_ref)
    assert comparison["ok"] and comparison["usage"] == "context_only"
    assert comparison["value"]["eligible_as_evidence_ref"] is False
    assert comparison["value"]["eligible_as_case_anchor"] is False
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    rejected = await _patch(editor, refs=[context_ref])
    assert not rejected["ok"]
    final, errors = editor.finalize({"action": "change_execution", "rationale": "Cannot promote Context"})
    assert final is None and errors
    assert context_ref not in payload["allowed_evidence_refs"]


@pytest.mark.asyncio
async def test_atomic_patch_failure_preserves_original_and_later_correction_preserves_other_components():
    payload = _payload()
    original = copy.deepcopy(payload)
    editor = _editor(payload)
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    rejected = await _tool(editor, "propose_component_edits", component_id="main-prompt", reason="Recorded fix",
                           evidence_refs=["target-a"], edits=[
                               {"operation": "replace", "old_string": "FIXME", "new_string": "Should not apply"},
                               {"operation": "replace", "old_string": "MISSING_ANCHOR", "new_string": "Rejected"},
                           ])
    assert not rejected["ok"]
    unchanged = await _tool(editor, "read_component", component_id="main-prompt")
    assert unchanged["component"] == payload["execution_plan"]["components"][0]
    assert (await _patch(editor))["ok"]
    checked = await _tool(editor, "validate_changes")
    assert checked["ok"] and checked["mechanical_passed"]
    assert checked["mechanical_only"] is True
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Verified the local correction"})
    assert result and not errors
    candidate = apply_changes(payload["execution_plan"], result["component_changes"], allowed_modules=["prompt"])
    assert candidate["components"][0]["content"] == original["execution_plan"]["components"][0]["content"].replace(
        "FIXME", "Clarified instruction",
    )
    assert candidate["components"][1] == original["execution_plan"]["components"][1]
    assert candidate["environment"] == original["execution_plan"]["environment"]
    assert candidate["binding"] == original["execution_plan"]["binding"]
    assert payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["literal value", "{{unknown}}", "{{business}} {{unknown.expression}}",
                                        "{{business}} {% if business %}changed{% endif %}"])
async def test_template_removal_new_variables_and_unsupported_syntax_cannot_reach_a_candidate(replacement):
    editor = _editor()
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    patch = await _patch(editor, old="{{business}}", new=replacement)
    assert not patch["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Invalid template attempt"})
    assert result is None and errors


@pytest.mark.asyncio
async def test_unplanned_edit_and_whole_body_final_cannot_bypass_the_patch_tools():
    editor = _editor()
    result, errors = editor.finalize({"action": "change_execution", "rationale": "No diagnosis yet"})
    assert result is None and errors
    await _tool(editor, "read_component", component_id="main-prompt")
    assert not (await _patch(editor))["ok"]
    _plan(editor)
    assert (await _patch(editor))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Bypass attempt",
                                     "component_changes": [{"component_id": "main-prompt", "content": "REPLACED"}]})
    assert result is None and errors
    fallback = editor.fallback()
    assert fallback["action"] == "change_execution"
    assert fallback["component_changes"] == editor.tools_state.changes()
    assert fallback["component_changes"][0]["content"] != "REPLACED"


@pytest.mark.asyncio
async def test_prompt_diagnosis_does_not_use_the_skill_bucket_or_expand_the_component_scope():
    editor = _editor(editable_component_ids=["main-prompt"])
    wrong_asset = editor.plan({"action_candidate": "change_execution", "rationale": "Wrong asset classification",
                              "evidence_classification": {"team_skill": ["target-a"]}},
                             round_no=1, max_rounds=12)
    assert wrong_asset.log_kind == "plan_rejected"
    _plan(editor)
    shared = await _tool(editor, "read_component", component_id="shared-context")
    assert shared["ok"] and not shared["editable"]
    rejected = await _tool(editor, "propose_component_edits", component_id="shared-context", reason="Out of scope",
                           evidence_refs=["target-a"], edits=[{
                               "operation": "replace", "old_string": "Unchanged", "new_string": "Changed",
                           }])
    assert not rejected["ok"]
    await _tool(editor, "read_component", component_id="main-prompt")
    assert not (await _patch(editor, slot="developer"))["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "No authorized change"})
    assert result is None and errors


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", ["native_false", "drop_candidate_flag", "true", "null", "agent", "environment"])
async def test_pending_prompt_preserves_derived_skill_flag_and_all_frozen_metadata(metadata):
    frozen = _payload()
    baseline = copy.deepcopy(frozen["execution_plan"])
    first = baseline["components"][0]
    pending = apply_changes(baseline, [{
        "component_id": first["component_id"], "content": first["content"].replace("FIXME", "Pending A"),
        "reason": "Prior native Candidate edit", "evidence_refs": ["target-a"],
    }], allowed_modules=["prompt"])
    pending["binding"]["allow_skill_change"] = False
    frozen.update(execution_plan=pending, baseline_execution_plan=baseline)
    editor = _editor(frozen)
    _plan(editor, component="shared-context")
    await _tool(editor, "read_component", component_id="shared-context")
    shared = next(part for part in pending["components"] if part["component_id"] == "shared-context")
    edited = await _tool(editor, "propose_component_edits", component_id="shared-context",
        edits=[{"operation": "replace", "old_string": shared["content"], "new_string": "Pending B"}],
        reason="A second evidence-backed component edit", evidence_refs=["target-a"])
    assert edited["ok"]
    # An out-of-band mutation must fail even without an independent review model.
    working = editor.tools_state.working
    if metadata == "drop_candidate_flag":
        working["binding"].pop("allow_skill_change")
    elif metadata in {"true", "null"}:
        working["binding"]["allow_skill_change"] = True if metadata == "true" else None
    elif metadata == "agent":
        working["binding"]["agent_id"] = "unapproved-agent"
    elif metadata == "environment":
        working["environment"]["template_variables"]["business"] = "changed readonly value"
    checked = await _tool(editor, "validate_changes")
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Continue the pending Candidate"})
    if metadata != "native_false":
        assert not checked["ok"] and result is None and errors
        return
    assert checked["ok"] and checked["mechanical_only"] and result and not errors
    assert working["components"][0]["content"] == first["content"].replace("FIXME", "Pending A")
    assert [change["component_id"] for change in result["component_changes"]] == ["shared-context"]
    assert working["binding"]["allow_skill_change"] is False
    assert frozen["baseline_execution_plan"] == baseline


@pytest.mark.asyncio
async def test_exact_editor_can_remove_redundant_correct_wording_without_changing_templates_or_other_components():
    frozen = _payload()
    old = frozen["execution_plan"]["components"][0]["content"]
    redundant = "正确但重复的格式自检说明。\n"
    raw = copy.deepcopy(frozen["execution_plan"])
    part = raw["components"][0]
    part["content"] = old + redundant
    part["content_hash"] = hashlib.sha256(part["content"].encode()).hexdigest()
    frozen["execution_plan"] = raw
    editor = _editor(frozen)
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert (await _patch(editor, redundant, ""))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Remove equivalent repeated wording"})
    assert result and not errors and result["component_changes"][0]["content"] == old
    assert "{{business}}" in old
    assert editor.tools_state.working["components"][1] == frozen["execution_plan"]["components"][1]
    assert editor.tools_state.working["environment"] == frozen["execution_plan"]["environment"]


@pytest.mark.asyncio
async def test_inspection_shows_a_smaller_new_edit_can_still_exceed_the_published_baseline():
    frozen = _payload()
    baseline = copy.deepcopy(frozen["execution_plan"])
    inherited = "Inherited correct wording, with a repeated explanation that is still longer than the baseline."
    content = baseline["components"][0]["content"] + inherited
    pending = apply_changes(baseline, [{"component_id": "main-prompt", "content": content,
        "reason": "Prior candidate wording", "evidence_refs": ["target-a"]}], allowed_modules=["prompt"])
    frozen.update(execution_plan=pending, baseline_execution_plan=baseline)
    before = copy.deepcopy(frozen)
    editor = _editor(frozen)
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert (await _patch(editor, inherited, "Inherited correct wording."))["ok"]
    observed = await _tool(editor, "inspect_changes")
    summary = observed["component_size_summary"]
    item = summary["components"][0]
    assert summary["is_token_usage"] is False and summary["character_unit"] == "unicode_code_points"
    assert summary["measurement"] == "raw_component_content_before_template_substitution"
    assert item["published_characters"] < item["staged_characters"] < item["working_characters"]
    assert item["delta_vs_working_characters"] < 0 < item["delta_vs_published_characters"]
    shared = summary["components"][1]
    assert shared["published_utf8_bytes"] > shared["published_characters"]
    assert shared["published_characters"] == shared["working_characters"] == shared["staged_characters"]
    assert summary["totals"]["delta_vs_working_characters"] == item["delta_vs_working_characters"]
    assert frozen == before


@pytest.mark.asyncio
async def test_initial_writer_receives_inherited_size_and_only_the_original_three_efficiency_metrics():
    frozen = _payload()
    baseline = copy.deepcopy(frozen["execution_plan"])
    part = baseline["components"][0]
    pending = apply_changes(baseline, [{"component_id": "main-prompt", "content": part["content"] + "Inherited rule.",
        "reason": "Earlier candidate", "evidence_refs": ["target-a"]}], allowed_modules=["prompt"])
    metrics = {"interaction_turns": 1, "tool_call_count": 0, "total_tokens": 100}
    context = {"baseline_execution_plan": baseline, "working_execution_plan": pending,
        "active_candidate": {"candidate_id": "pending", "revision": 1}, "active_candidate_feedback": {
            "baseline": {**metrics, "input_tokens": 70, "output_tokens": 30},
            "candidate": {**metrics, "input_tokens": 90, "output_tokens": 10},
            "cases": [{"query": "HIDDEN_QUERY", "answer": "HIDDEN_ANSWER", "checks": ["HIDDEN_CHECK"]}],
        }}
    model = PromptEditorLLM(["target-a"])
    result = await propose_prompt(model, sessions=frozen["evidence"], context=context)
    assert result["action"] == "change_execution"
    body = json.loads(model.editor_calls[0][0][1]["content"])
    assert body["component_size_summary"] == component_size_summary(baseline, pending)
    assert body["active_candidate_feedback"] == {"baseline": metrics, "candidate": metrics}
    assert "HIDDEN_" not in _canonical(model.calls)


@pytest.mark.asyncio
async def test_frozen_pending_candidate_is_the_edit_basis_and_published_baseline_remains_read_only():
    payload = _payload()
    baseline = copy.deepcopy(payload["execution_plan"])
    pending = {"component_id": "main-prompt", "content": "Pending FIXME {{business}}\n",
               "reason": "Earlier candidate change", "evidence_refs": ["old-only-reference"]}
    payload["execution_plan"] = apply_changes(baseline, [pending], allowed_modules=["prompt"])
    payload["baseline_execution_plan"] = baseline
    editor = _editor(payload)
    _plan(editor)
    current = await _tool(editor, "read_component", component_id="main-prompt")
    original = await _tool(editor, "read_component", component_id="main-prompt", version="baseline")
    assert current["component"]["content"].startswith("Pending")
    assert original["component"]["content"] == baseline["components"][0]["content"]
    assert (await _patch(editor))["ok"]
    assert (await _tool(editor, "validate_changes"))["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Continue the pending candidate"})
    assert result and not errors
    assert result["component_changes"][0]["content"] == "Pending Clarified instruction {{business}}\n"
    assert result["component_changes"][0]["evidence_refs"] == ["target-a"]
    assert payload["baseline_execution_plan"] == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["empty_targets", "context_trace", "missing_body", "context_turn"])
async def test_reference_whitelist_cannot_launder_absent_or_context_only_target_content(scope):
    frozen = _payload()
    source = frozen["evidence"][0]
    if scope == "empty_targets":
        source["provenance"]["target_trace_refs"] = []
    elif scope == "context_trace":
        source["provenance"]["target_trace_refs"][0]["trace_id"] = "different-target"
    elif scope == "missing_body":
        source["turns"] = []
    else:
        source["turns"][0]["context_only"] = True
    editor = _editor(frozen)
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    before = copy.deepcopy(editor.tools_state.working)
    assert not (await _patch(editor))["ok"]
    assert editor.tools_state.working == before
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Cannot cite missing target body"})
    assert result is None and errors


@pytest.mark.asyncio
@pytest.mark.parametrize("strings", [False, True])
async def test_recorded_target_boundaries_accept_object_and_legacy_string_refs(strings):
    frozen = _payload()
    if strings:
        frozen["evidence"][0]["provenance"]["target_trace_refs"] = ["target-a"]
        frozen["evidence"][0]["provenance"]["context_trace_refs"] = ["context-a"]
    editor = _editor(frozen)
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert (await _patch(editor))["ok"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": "Preserve the recorded target source"})
    assert result and not errors


@pytest.mark.asyncio
@pytest.mark.parametrize("strings", [False, True])
async def test_legacy_context_boundary_cannot_become_a_target_ref_without_an_explicit_target_declaration(strings):
    frozen = _payload()
    provenance = frozen["evidence"][0]["provenance"]
    provenance.pop("target_trace_refs")
    provenance["context_trace_refs"] = ["target-a"] if strings else [{"trace_id": "target-a"}]
    editor = _editor(frozen)
    _plan(editor)
    await _tool(editor, "read_component", component_id="main-prompt")
    assert not (await _patch(editor))["ok"]
    assert not editor.tools_state.changes()
