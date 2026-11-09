"""A real editing protocol script for Prompt entry-point evidence tests.

The fake only answers the shared editing protocol, never a separate review or business Replay.
It patches through public tools and preserves all original template variables.
"""
from __future__ import annotations

import copy
import json

from teamEvolver.evidence_transport import decode_evidence_transport


class PromptEditorLLM:
    def __init__(self, refs, *, component_id=None, replacement=None):
        self.refs = refs
        self.component_id = component_id
        self.replacement = replacement
        self.calls = []
        self.editor_calls = []
        self.payload = None
        self.component = None
        self.plan_rejection = None

    async def chat(self, messages, **options):
        captured = (copy.deepcopy(messages), copy.deepcopy(options))
        self.calls.append(captured)
        if options.get("trace_name") != "team-skill-evolver:prompt_edit":
            raise AssertionError("Prompt editing must not dispatch an independent review")

        step = len(self.editor_calls)
        self.editor_calls.append(captured)
        if step == 0:
            body = json.loads(messages[1]["content"])
            self.payload = decode_evidence_transport(body) if body.get("schema") else body
            components = self.payload["execution_plan"]["components"]
            self.component = copy.deepcopy(next(
                c for c in components if self.component_id is None or c["component_id"] == self.component_id
            ))
            return json.dumps({
                "type": "plan", "action_candidate": "change_execution", "rationale": "Resolve a recorded correction",
                "evidence_classification": {"business_prompt": [{
                    "component_id": self.component["component_id"], "problem": "Existing handling needs clarification",
                    "existing_guidance": self.component["content"],
                    "proposed_change": "Add a short clarification while preserving original requirements",
                    "evidence_refs": self.refs,
                }]},
            }, ensure_ascii=False)

        observation = json.loads(messages[-1]["content"])
        failures = [item for item in observation.get("observations") or [] if item.get("ok") is False]
        if failures:
            if step == 1:
                self.plan_rejection = copy.deepcopy(failures)
            return json.dumps({"type": "plan", "action_candidate": "skip",
                               "rationale": "The recorded correction did not pass the editing contract"})

        cid = self.component["component_id"]
        if step == 1:
            value = {"type": "tool_calls", "calls": [{"tool": "read_component", "args": {
                "component_id": cid, "version": "current",
            }}]}
        elif step == 2:
            edit = ({"operation": "replace", "old_string": self.component["content"],
                     "new_string": self.replacement} if self.replacement is not None else {
                         "operation": "insert_after", "anchor": self.component["content"],
                         "new_string": "\nClarify handling of the recorded correction.",
                     })
            value = {"type": "tool_calls", "calls": [{"tool": "propose_component_edits", "args": {
                "component_id": cid, "reason": "Clarify the exact recorded correction without changing requirements",
                "evidence_refs": self.refs, "edits": [edit],
            }}]}
        elif step == 3:
            value = {"type": "tool_calls", "calls": [{"tool": "validate_changes", "args": {}}]}
        elif step == 4:
            value = {"type": "final", "decision": {"action": "change_execution",
                "rationale": "Clarified the recorded correction while preserving original business requirements"}}
        else:
            raise AssertionError("Unexpected additional editing round; do not silently fake success")
        return json.dumps(value, ensure_ascii=False)


def assert_editor_options(options, *, source_session_count):
    assert options["max_tokens"] == 16384
    assert options["temperature"] == 0.2
    assert options["trace_name"] == "team-skill-evolver:prompt_edit"
    assert options["trace_tags"] == ["prompt-editor", "asset-evolution"]
    assert options["trace_metadata"]["round"] == 1
    assert options["trace_metadata"]["source_session_count"] == source_session_count


def assert_successful_prompt_edit(writer, result):
    """Require a submitted mechanical tool edit rather than only a readable initial input."""
    assert result["action"] == "change_execution"
    assert len(writer.editor_calls) == 5
    assert len(writer.calls) == 5
    assert result["component_changes"] == [{
        "component_id": writer.component["component_id"],
        "content": (writer.replacement if writer.replacement is not None else
                    writer.component["content"] + "\nClarify handling of the recorded correction."),
        "reason": "Clarify the exact recorded correction without changing requirements",
        "evidence_refs": list(dict.fromkeys(writer.refs)),
    }]
    assert result["editor_audit"]["applied_edits"] == 1
    assert result["editor_audit"]["mechanical_only"] is True
    assert "business_contract_review" not in result["editor_audit"]
    assert "business_review_hash" not in result["editor_audit"]
