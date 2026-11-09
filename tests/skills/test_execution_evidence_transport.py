"""Lossless evidence transport and the Prompt writer's unchanged evidence boundaries."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_skills.evolution.kernel.enums import DecisionAction
from team_skills.execution import planner
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.editor import EDITOR_PROTOCOL
from team_skills.execution.evidence_transport import (
    TRANSPORT_INSTRUCTIONS,
    decode_evidence_transport,
    encode_evidence,
)
from teamEvolver.evidence_context import build_evidence_context
from tests.support.execution import profile_raw
from tests.support.prompt_editor import PromptEditorLLM as _Writer
from tests.support.prompt_editor import assert_editor_options, assert_successful_prompt_edit


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _document():
    original = ('原始 System\r\n保留空白\t、Unicode：é\u2028，以及字面文本 {"$string": 0}。\n' * 256)
    context = {"source_session_key": "root-a", "trace_id": "trace-a", "revision": "r1",
               "context_revision": "prefix-one"}
    target = {**context, "revision": "r2", "context_revision": "prefix-two"}
    return {
        "execution_plan": {"components": [{"role": "system", "content": original}]},
        "evidence": [{
            "session_id": "formal-a",
            "provenance": {"target_trace_refs": [target], "context_trace_refs": [context]},
            "turns": [{"source_trace_id": "trace-a", "request_context": {
                "evidence_level": "generation_input", "completeness": "unknown",
                "trace_input": original,
                "generation_requests": [{"model": "model-a", "model_parameters": {
                    "temperature": 0, "stream": False, "tools": [], "optional": None,
                }, "input": [
                    {"role": "system", "content": original},
                    {"role": "developer", "content": ""},
                    {"role": "user", "content": original},
                    {"role": "assistant", "content": None, "tool_calls": []},
                    {"role": "tool", "tool_call_id": "call-a", "content": original},
                ]}],
            }}],
            "analysis_context": {"context_only": True, "turns": [{"content": original}]},
        }],
        "allowed_evidence_refs": ["formal-a", "trace-a", original],
        "typed": {"0": [original, 0, 0.0, False, None, "", [], {}], "a/b~c": original},
        "literal": {"$string": 0},
        "literal_envelope": {"schema": "literal", "body": {"$string": 0}, "references": []},
    }


def _packed():
    document = _document()
    raw = _json(document)
    encoded, packed = encode_evidence(document, max_bytes=len(raw.encode()) - 1)
    assert packed
    assert len(encoded.encode()) < len(raw.encode())
    return document, json.loads(encoded)


def test_small_evidence_is_byte_identical_at_budget_boundary_and_does_not_mutate():
    document = {"evidence": [{"content": "中文\n  原文", "optional": None}], "allowed_evidence_refs": []}
    before = copy.deepcopy(document)
    expected = _json(document)
    encoded, packed = encode_evidence(document, max_bytes=len(expected.encode()))
    assert (encoded, packed) == (expected, False)
    assert document == before


def test_packed_evidence_preserves_full_strings_types_order_scope_and_citation_whitelist():
    document, envelope = _packed()
    before = copy.deepcopy(envelope)
    restored = decode_evidence_transport(envelope)
    assert _json(restored) == _json(document)
    assert envelope == before
    assert envelope["schema"] == "teamevolver.evidence-string-table.v1"
    assert envelope["source_bytes"] == len(_json(document).encode())
    assert envelope["source_sha256"] == hashlib.sha256(_json(document).encode()).hexdigest()
    assert envelope["body"]["allowed_evidence_refs"] == document["allowed_evidence_refs"]
    assert all(reference["path"][0] != "allowed_evidence_refs" for reference in envelope["references"])
    assert all(type(part) in (str, int) for ref in envelope["references"] for part in ref["path"])
    assert "missing" not in restored["typed"]
    assert type(restored["typed"]["0"][1]) is int
    assert type(restored["typed"]["0"][2]) is float
    assert type(restored["typed"]["0"][3]) is bool


def test_decoder_only_replaces_explicit_paths_and_preserves_literal_reference_shapes():
    document, envelope = _packed()
    restored = decode_evidence_transport(envelope)
    assert restored["literal"] == {"$string": 0}
    assert restored["literal_envelope"] == document["literal_envelope"]
    assert restored["typed"]["0"][0] == document["typed"]["0"][0]
    assert restored["typed"]["a/b~c"] == document["typed"]["a/b~c"]


def test_single_unshared_long_string_does_not_add_an_unhelpful_envelope():
    document = {"evidence": [{"content": "单个原文不重复\n" * 4096}], "allowed_evidence_refs": ["formal-a"]}
    raw = _json(document)
    assert encode_evidence(document, max_bytes=len(raw.encode()) - 1) == (raw, False)


@pytest.mark.parametrize("index", [True, -1, 10**9, "0", None])
def test_decoder_rejects_invalid_pool_indices(index):
    _, envelope = _packed()
    envelope["references"][0]["index"] = index
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope)


@pytest.mark.parametrize("path", [[True], [0.0], [None], ["missing"], ["evidence", -1],
                                  ["evidence", "0"], ["typed", 0], []])
def test_decoder_rejects_wrong_missing_and_ambiguous_path_components(path):
    _, envelope = _packed()
    envelope["references"][0]["path"] = path
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope)


def test_decoder_rejects_duplicate_paths_and_refs_to_non_placeholder_values():
    _, envelope = _packed()
    envelope["references"].append(copy.deepcopy(envelope["references"][0]))
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope)
    _, envelope = _packed()
    envelope["references"][0]["path"] = ["typed", "0", 1]
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope)


@pytest.mark.parametrize("field", ["source_sha256", "source_bytes", "strings", "placeholder", "references"])
def test_decoder_rejects_tampering_in_metadata_pooled_content_and_reference_coverage(field):
    _, envelope = _packed()
    if field == "source_sha256":
        envelope[field] = "0" * 64
    elif field == "source_bytes":
        envelope[field] += 1
    elif field == "strings":
        envelope[field][0] += "tampered"
    elif field == "references":
        envelope[field].pop()
    else:
        reference = envelope["references"][0]
        parent = envelope["body"]
        for part in reference["path"][:-1]:
            parent = parent[part]
        parent[reference["path"][-1]] = {"$string": reference["index"], "unexpected": True}
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope)


def test_decoder_enforces_expanded_and_encoded_byte_limits():
    document, envelope = _packed()
    assert len(_json(envelope).encode()) < len(_json(document).encode())
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope, max_bytes=len(_json(document).encode()) - 1)
    envelope["strings"].append("unreferenced oversized input" * 5000)
    with pytest.raises(ValueError):
        decode_evidence_transport(envelope, max_bytes=len(_json(document).encode()))


def _planner_inputs(*, large):
    profile = validate_profile(profile_raw())
    context = {"baseline_execution_plan": plan_for(profile, profile["components"])}
    original = "原始业务 System\n" * (4096 if large else 1)
    turns = [{"source_trace_id": "context-" + str(index), "request_context": {
        "evidence_level": "generation_input", "completeness": "unknown",
        "generation_requests": [{"input": [{"role": "system", "content": original},
                                            {"role": "user", "content": "Input " + str(index)}]}],
    }} for index in range(24 if large else 1)]
    target = {"source_trace_id": "target-a", "request_context": copy.deepcopy(turns[-1]["request_context"])}
    sessions = [{"session_id": "formal-a", "turns": [*turns, target], "_evidence_turns": [target],
                 "provenance": {"target_trace_refs": [{"trace_id": "target-a"}],
                                "context_trace_refs": [{"trace_id": turn["source_trace_id"]} for turn in turns]}}]
    return sessions, context


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
async def test_planner_only_adds_te_transport_instructions_when_packed_and_preserves_writer_contract(large):
    sessions, context = _planner_inputs(large=large)
    before = copy.deepcopy((sessions, context))
    expected = {"execution_plan": context["baseline_execution_plan"], "evidence": planner.raw_evidence(sessions),
                "allowed_evidence_refs": ["formal-a", "target-a"],
                "evidence_context": build_evidence_context(sessions, include_content=False)}
    # Independently counted ASCII fixture text: 24 + 31 code points/UTF-8 bytes.
    # The first request has no inherited or staged edits, so all deltas are zero.
    expected["component_size_summary"] = {
        "measurement": "raw_component_content_before_template_substitution",
        "character_unit": "unicode_code_points", "byte_unit": "utf8_bytes", "is_token_usage": False,
        "components": [{
            "component_id": cid, "published_characters": size, "published_utf8_bytes": size,
            "working_characters": size, "working_utf8_bytes": size,
            "staged_characters": size, "staged_utf8_bytes": size,
            "delta_vs_published_characters": 0, "delta_vs_working_characters": 0,
            "delta_vs_published_utf8_bytes": 0, "delta_vs_working_utf8_bytes": 0,
        } for cid, size in (("main-prompt", 24), ("shared-context", 31))],
        "totals": {
            "delta_vs_published_characters": 0, "delta_vs_published_utf8_bytes": 0,
            "delta_vs_working_characters": 0, "delta_vs_working_utf8_bytes": 0,
            "published_characters": 55, "published_utf8_bytes": 55,
            "staged_characters": 55, "staged_utf8_bytes": 55,
            "working_characters": 55, "working_utf8_bytes": 55,
        },
    }
    assert (len(_json(expected).encode()) > 786432) is large
    writer = _Writer(["formal-a", "target-a"])
    result = await planner.propose_prompt(writer, sessions=sessions, context=context)
    assert_successful_prompt_edit(writer, result)
    assert result["action"] == "change_execution"
    assert len(writer.editor_calls) == 5
    assert result["editor_audit"]["mechanical_only"] is True
    assert len(writer.calls) == 5
    messages, options = writer.calls[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    body = json.loads(messages[1]["content"])
    if large:
        assert messages[0]["content"].startswith(planner.SYSTEM)
        assert messages[0]["content"].removeprefix(planner.SYSTEM + EDITOR_PROTOCOL).strip() == (
            TRANSPORT_INSTRUCTIONS.strip()
        )
        assert _json(decode_evidence_transport(body)) == _json(expected)
    else:
        assert messages[0]["content"] == planner.SYSTEM + EDITOR_PROTOCOL
        assert messages[1]["content"] == _json(expected)
    assert len(messages[1]["content"].encode()) <= 786432
    assert_editor_options(options, source_session_count=len(sessions))
    assert (sessions, context) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_ref", ["context-0", "formal-a/target-a", "unknown"])
async def test_packed_planner_keeps_context_and_constructed_refs_out_of_citation_whitelist(invalid_ref):
    sessions, context = _planner_inputs(large=True)
    writer = _Writer([invalid_ref])
    result = await planner.propose_prompt(writer, sessions=sessions, context=context)
    assert result["action"] == DecisionAction.SKIP
    assert not result.get("component_changes")
    assert writer.plan_rejection[0]["tool"] == "(plan)"
    assert "evidence_refs" in writer.plan_rejection[0]["error"]
    assert len(writer.editor_calls) == 2 and len(writer.calls) == 2
    initial = json.loads(writer.calls[0][0][1]["content"])
    assert decode_evidence_transport(initial)["allowed_evidence_refs"] == ["formal-a", "target-a"]


@pytest.mark.asyncio
async def test_still_oversized_non_repeating_evidence_skips_without_calling_writer():
    _, context = _planner_inputs(large=False)
    sessions = [{"session_id": "formal-a", "turns": [{"source_trace_id": "target-a", "request_context": {
        "generation_requests": [{"input": "只出现一次的完整原文\n" * 90000}],
    }}]}]
    before = copy.deepcopy(sessions)
    writer = _Writer(["formal-a", "target-a"])
    result = await planner.propose_prompt(writer, sessions=sessions, context=context)
    assert result == {"action": DecisionAction.SKIP, "rationale": "raw_prompt_evidence_exceeds_budget"}
    assert writer.calls == []
    assert sessions == before
