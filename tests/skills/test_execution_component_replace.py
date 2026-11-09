"""A Prompt body-hash macro expands through the existing exact-edit kernel."""
from __future__ import annotations

import copy

import pytest

from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.editor import EDITOR_PROTOCOL, PromptEditorAdapter
from team_skills.execution.editor_tools import sha
from tests.support.execution import profile_raw

OLD = "Keep the external condition.\r\n{{domain}}\nRepeated direction.\nRepeat that direction.\n"
NEW = "Keep the external condition.\r\n{{domain}}\nOne merged direction. é / e\u0301\n"
REASON = "Merge repeated implementation guidance while covering the original condition."


def _payload():
    raw = profile_raw()
    raw["components"][0]["content"] = OLD
    raw["environment"]["template_variables"] = {"domain": "Frozen business fact"}
    profile = validate_profile(raw)
    return {"execution_plan": plan_for(profile, profile["components"]),
            "allowed_evidence_refs": ["source", "target-1", "target-2"],
            "evidence": [{"session_id": "source", "turns": [
                {"trace_id": f"target-{i}", "prompt_text": "Original request",
                 "messages": [{"role": "user", "content": "Remove repetition while preserving the condition."}]}
                for i in (1, 2)]}]}


def _editor(*, read=True, plan=True):
    editor = PromptEditorAdapter(_payload(), editable_component_ids={"main-prompt"})
    if plan:
        editor.plan({"action_candidate": "change_execution", "rationale": REASON,
                     "evidence_classification": {"business_prompt": [{
                         "component_id": "main-prompt", "problem": "Repeated implementation guidance",
                         "existing_guidance": "", "proposed_change": REASON, "evidence_refs": ["target-1"],
                     }]}}, round_no=1, max_rounds=12)
    if read:
        editor.tools_state.read_component({"component_id": "main-prompt", "version": "current"})
    return editor


async def _tool(editor, name, args):
    values = await editor.tools([{"tool": name, "args": args}])
    assert len(values) == 1
    return values[0]


def _args(**changes):
    return {"component_id": "main-prompt", "content": NEW, "expected_content_hash": sha(OLD),
            "reason": REASON, "evidence_refs": ["target-1"], **changes}


def _final(editor):
    return editor.finalize({"action": "change_execution", "rationale": REASON})


@pytest.mark.asyncio
async def test_macro_is_exact_kernel_equivalent_for_body_diff_refs_provenance_validation_and_final():
    macro = _editor()
    direct = _editor()
    frozen = copy.deepcopy(macro.tools_state.payload)
    reading = macro.tools_state.read_component({"component_id": "main-prompt", "version": "current"})
    assert reading["content_sha256"] == sha(OLD) != sha(macro.tools_state.working)
    expanded = await _tool(macro, "replace_component_content", _args(expected_content_hash=reading["content_sha256"]))
    manual = await _tool(direct, "propose_component_edits", {"component_id": "main-prompt", "reason": REASON,
        "evidence_refs": ["target-1"], "edits": [{"operation": "replace", "old_string": OLD, "new_string": NEW}]})
    assert expanded["ok"] and manual["ok"]
    assert {k: v for k, v in expanded.items() if k != "tool"} == {k: v for k, v in manual.items() if k != "tool"}
    assert macro.tools_state.working == direct.tools_state.working
    assert macro.tools_state.changes() == direct.tools_state.changes()
    assert macro.tools_state.edit_provenance == direct.tools_state.edit_provenance
    assert macro.tools_state.inspect_changes({}) == direct.tools_state.inspect_changes({})
    assert (await _tool(macro, "validate_changes", {}))["ok"]
    assert (await _tool(direct, "validate_changes", {}))["ok"]
    assert _final(macro) == _final(direct)
    result, errors = _final(macro)
    assert not errors and result["component_changes"][0]["content"] == NEW
    assert macro.tools_state.working["binding"] == frozen["execution_plan"]["binding"]
    assert macro.tools_state.working["environment"] == frozen["execution_plan"]["environment"]
    assert macro.tools_state.working["components"][1] == frozen["execution_plan"]["components"][1]
    assert macro.tools_state.payload == frozen
    assert "不能用editor_state.staged_hash" in EDITOR_PROTOCOL


@pytest.mark.asyncio
@pytest.mark.parametrize("mistake", ["unread", "baseline_only_read", "unplanned", "stale_hash", "composition_hash",
    "wrong_component", "unknown_ref", "unplanned_ref", "missing_variable", "invalid_template", "nonstring", "metadata",
    "no_change"])
async def test_invalid_macro_is_atomic_and_cannot_produce_a_valid_new_final(mistake):
    editor = _editor(read=mistake not in {"unread", "baseline_only_read"}, plan=mistake != "unplanned")
    args = _args()
    if mistake == "baseline_only_read":
        editor.tools_state.read_component({"component_id": "main-prompt", "version": "baseline"})
    elif mistake == "stale_hash":
        args["expected_content_hash"] = "0" * 64
    elif mistake == "composition_hash":
        args["expected_content_hash"] = sha(editor.tools_state.working)
    elif mistake == "wrong_component":
        args["component_id"] = "shared-context"
    elif mistake == "unknown_ref":
        args["evidence_refs"] = ["unknown-original-ref"]
    elif mistake == "unplanned_ref":
        args["evidence_refs"] = ["target-2"]
    elif mistake == "missing_variable":
        args["content"] = NEW.replace("{{domain}}", "invented value")
    elif mistake == "invalid_template":
        args["content"] += "{{domain.unsupported}}"
    elif mistake == "nonstring":
        args["content"] = {"text": NEW}
    elif mistake == "metadata":
        args["slot"] = "developer"
    elif mistake == "no_change":
        args["content"] = OLD
    state = copy.deepcopy((editor.tools_state.working, editor.tools_state.references,
                           editor.tools_state.edit_provenance, editor.tools_state.reasons))
    observation = await _tool(editor, "replace_component_content", args)
    assert not observation["ok"] and "unknown tool" not in observation["error"]
    assert (editor.tools_state.working, editor.tools_state.references,
            editor.tools_state.edit_provenance, editor.tools_state.reasons) == state
    result, errors = _final(editor)
    assert result is None and errors


@pytest.mark.asyncio
async def test_macro_uses_the_latest_body_hash_and_final_materializes_the_latest_exact_content():
    editor = _editor()
    first = await _tool(editor, "replace_component_content", _args())
    assert first["ok"] and first["content_sha256"] == sha(NEW)
    assert _final(editor)[0] is not None
    second_text = NEW + "Keep the original exception explicit.\n"
    second = await _tool(editor, "replace_component_content", _args(
        content=second_text, expected_content_hash=first["content_sha256"],
    ))
    assert second["ok"] and editor.tools_state.read_current == {"main-prompt"}
    stale = await _tool(editor, "replace_component_content", _args(expected_content_hash=first["content_sha256"]))
    assert not stale["ok"] and "expected_content_hash" in stale["error"]
    assert (await _tool(editor, "validate_changes", {}))["ok"]
    result, errors = _final(editor)
    assert not errors and result["component_changes"][0]["content"] == second_text


@pytest.mark.asyncio
async def test_macro_round_limit_uses_the_same_validation_without_allowing_final_body_submission():
    editor = _editor()
    assert (await _tool(editor, "replace_component_content", _args()))["ok"]
    assert (await _tool(editor, "validate_changes", {}))["mechanical_only"]
    fallback = editor.fallback()
    assert fallback["action"] == "change_execution"
    assert fallback["component_changes"] == _final(editor)[0]["component_changes"]
    result, errors = editor.finalize({"action": "change_execution", "rationale": REASON,
                                     "component_changes": [{"component_id": "main-prompt", "content": NEW}]})
    assert result is None and errors
    assert _final(editor)[0] is not None
