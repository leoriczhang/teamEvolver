"""Recorded Evidence retains task boundaries, uncertainty and external feedback."""
from __future__ import annotations

import copy
import json

import pytest

from teamEvolver.evidence_context import (
    EvidenceContextError,
    build_evidence_context,
    has_isolated_records,
)


def recorded_turn(trace_id="trace-a", *, isolated=True, query="History: none. Current request: choose a time."):
    reconstruction = {
        "origin": "offline_historical_reconstruction", "constructed_import_batch": isolated,
        "batch_id": "transport-only", "original_session_independence": "unknown",
        "actual_original_request_observed": False,
    }
    feedback = {"score": 10, "explanation": "Final score is zero", "criteria": {"relevance": "failed"}}
    return {
        "trace_id": trace_id, "prompt_text": query, "response_text": "Hello, who is this?",
        "_langfuse": {"tags": ["machine-evaluation-feedback"]},
        "messages": [
            {"role": "assistant", "content": "Hello, who is this?"},
            {"role": "user", "content": "model_eval_feedback: " + json.dumps(feedback)},
        ],
        "request_context": {
            "evidence_level": "generation_input",
            "trace_input": {"reconstruction": copy.deepcopy(reconstruction),
                            "business_context": {"history": "", "task": "First refuse; later choose from the list."}},
            "generation_requests": [{"input": {"reconstruction": reconstruction}}],
        },
    }


def source(turns):
    return {"session_id": "existing-container", "turns": turns,
            "provenance": {"source_session_key": "recorded-source"}}


def test_explicit_batch_records_do_not_establish_conversation_or_source_independence():
    # Identical outputs in two records with different own histories are not
    # evidence that the second record repeated a preceding conversation turn.
    data = [source([recorded_turn(), recorded_turn("trace-b", query="My own history is empty.")])]
    before = copy.deepcopy(data)
    packet = build_evidence_context(data)
    assert has_isolated_records(data)
    assert packet["anchor_required_for_session_ids"] == ["existing-container"]
    assert [unit["relationship"] for unit in packet["examples"]] == ["isolated_record"] * 2
    assert packet["examples"][1]["recorded_query"] == "My own history is empty."
    assert all(unit["sequence_semantics"] == "continuity_not_asserted" for unit in packet["examples"])
    assert all(unit["request_observation"]["original_session_independence"] == "unknown"
               for unit in packet["examples"])
    assert packet["coverage"]["source_session_ids"] == ["existing-container"]
    assert packet["coverage"]["included_count"] == 2
    assert data == before


def test_metadata_only_projection_binds_exact_same_content_without_repeating_body():
    data = [source([recorded_turn()])]
    full = build_evidence_context(data)
    metadata = build_evidence_context(data, include_content=False)
    assert metadata["coverage"]["context_sha256"] == full["coverage"]["context_sha256"]
    assert metadata["examples"][0]["content_sha256"] == full["examples"][0]["content_sha256"]
    assert metadata["examples"][0]["content_utf8_bytes"] == full["examples"][0]["content_utf8_bytes"]
    assert "recorded_query" not in metadata["examples"][0]
    assert "recorded_messages" not in metadata["examples"][0]
    assert "Hello, who is this?" not in json.dumps(metadata)


def test_conflicting_machine_score_and_explanation_are_preserved_without_relabeling():
    unit = build_evidence_context([source([recorded_turn()])])["examples"][0]
    feedback = unit["source_feedback"][0]
    assert feedback["origin"] == "declared_external_model_evaluation"
    assert feedback["structured_payload"] == {
        "score": 10, "explanation": "Final score is zero", "criteria": {"relevance": "failed"},
    }
    assert feedback["independently_verified"] is False
    assert feedback["raw_text"] == unit["recorded_messages"][-1]["content"]
    assert feedback["source_path"] == "messages[1]"


def test_real_multiturn_session_and_user_correction_are_not_reclassified_as_imported_records():
    turns = [recorded_turn("first", isolated=False), recorded_turn("second", isolated=False)]
    turns[-1]["messages"][-1]["content"] = 'Please correct the answer. {"score": 10}'
    data = [source(turns)]
    packet = build_evidence_context(data)
    assert not has_isolated_records(data)
    assert packet["anchor_required_for_session_ids"] == []
    assert [unit["trace_id"] for unit in packet["examples"]] == ["first", "second"]
    assert all(unit["relationship"] == "recorded_session_turn" for unit in packet["examples"])
    assert packet["examples"][1]["source_feedback"][0]["origin"] == "recorded_user_message"
    assert packet["examples"][1]["source_feedback"][0]["structured_payload"] is None


@pytest.mark.parametrize("detector", [has_isolated_records, build_evidence_context])
def test_conflicting_reconstruction_views_fail_instead_of_choosing_a_convenient_history(detector):
    turn = recorded_turn()
    turn["request_context"]["generation_requests"][0]["input"]["reconstruction"]["constructed_import_batch"] = False
    with pytest.raises(EvidenceContextError, match="Conflicting recorded reconstruction"):
        detector([source([turn])])


def test_only_target_evidence_is_presented_and_an_explicit_empty_target_never_uses_context():
    context, target = recorded_turn("old"), recorded_turn("new")
    data = source([context, target])
    data["provenance"].update(target_trace_refs=[{"trace_id": "new"}], context_trace_refs=[{"trace_id": "old"}])
    packet = build_evidence_context([data])
    assert [unit["evidence_ref"] for unit in packet["examples"]] == ["new"]
    assert packet["coverage"]["expected_count"] == packet["coverage"]["included_count"] == 1
    assert build_evidence_context([{**source([context]), "_evidence_turns": []}])["examples"] == []
    data["_evidence_turns"] = []
    with pytest.raises(EvidenceContextError, match="target Trace is missing"):
        build_evidence_context([data])


def test_repeated_projection_is_not_additional_evidence_but_conflicting_copies_fail():
    turn = recorded_turn()
    data = source([turn, copy.deepcopy(turn)])
    packet = build_evidence_context([data])
    assert packet["coverage"]["expected_count"] == packet["coverage"]["included_count"] == 1
    assert packet["coverage"]["duplicate_evidence_refs"] == ["trace-a"]
    data["turns"][-1]["response_text"] = "A contradictory recorded answer"
    with pytest.raises(EvidenceContextError, match="Conflicting copies"):
        build_evidence_context([data])


def test_more_than_fifty_records_are_present_and_oversize_evidence_does_not_silently_clip():
    data = [source([recorded_turn(f"trace-{index}", query="Recorded complete query " + str(index))
                    for index in range(52)])]
    packet = build_evidence_context(data)
    assert packet["coverage"]["expected_count"] == packet["coverage"]["included_count"] == 52
    assert packet["examples"][-1]["recorded_query"] == "Recorded complete query 51"
    assert packet["coverage"]["omitted_evidence_refs"] == packet["coverage"]["truncated_fields"] == []
    with pytest.raises(EvidenceContextError, match="no fields were truncated"):
        build_evidence_context(data, max_bytes=1_000)


def test_tags_and_id_names_alone_do_not_make_a_session_an_independent_import():
    turn = recorded_turn()
    turn.pop("request_context")
    packet = build_evidence_context([source([turn])])
    assert not has_isolated_records([source([turn])])
    assert packet["examples"][0]["relationship"] == "recorded_session_turn"
    assert packet["examples"][0]["source_feedback"][0]["origin"] == "recorded_user_message"


def test_old_context_cannot_change_the_target_session_synthesis_path():
    old, target = recorded_turn("old-context"), recorded_turn("real-target", isolated=False)
    # Even conflicting metadata in Context is not new target Evidence.
    old["request_context"]["generation_requests"][0]["input"]["reconstruction"]["constructed_import_batch"] = False
    data = source([old, target])
    data["provenance"].update(target_trace_refs=[{"trace_id": "real-target"}],
                              context_trace_refs=[{"trace_id": "old-context"}])
    assert not has_isolated_records([data])
    packet = build_evidence_context([data])
    assert [unit["trace_id"] for unit in packet["examples"]] == ["real-target"]
    assert packet["anchor_required_for_session_ids"] == []


def test_no_batch_declaration_keeps_the_legacy_skill_path_without_interpreting_other_observations():
    turn = recorded_turn(isolated=False)
    reconstruction = turn["request_context"]["generation_requests"][0]["input"]["reconstruction"]
    reconstruction["actual_original_request_observed"] = True
    assert not has_isolated_records([source([turn])])


def context_source(*, target_id="selected", context_id="comparison", session_id="selected-container"):
    target, context = recorded_turn(target_id), recorded_turn(context_id, query="A distinct recorded comparison.")
    context["response_text"] = "The original acceptable answer."
    context["messages"][0]["content"] = context["response_text"]
    context["messages"][-1]["content"] = 'model_eval_feedback: {"score": 100, "judgment": "PASS"}'
    data = source([target])
    data["session_id"] = session_id

    def ref(trace_id):
        return {"source_session_key": "recorded-source", "trace_id": trace_id,
                "revision": "recorded-revision-" + trace_id, "context_revision": "recorded-context-" + trace_id}

    data["provenance"].update(
        schema_version="session-source.v1", identity_verified=True, trace_identity_verified=True,
        target_trace_refs=[ref(target_id)], trace_revisions=[ref(target_id)], context_trace_refs=[ref(context_id)],
    )
    data["analysis_context"] = {
        "context_only": True, "turns": [copy.deepcopy(target), context],
        "context_trace_refs": [ref(context_id)],
    }
    return data


def test_verified_comparison_is_preserved_as_context_without_becoming_target_or_anchor():
    data = context_source()
    before = copy.deepcopy(data)
    full = build_evidence_context([data])
    metadata = build_evidence_context([data], include_content=False)
    assert full["coverage"]["included_count"] == full["coverage"]["expected_count"] == 1
    assert full["coverage"]["context_count"] == 1
    assert full["anchor_required_for_session_ids"] == ["selected-container"]
    assert full["coverage"]["source_session_ids"] == ["selected-container"]
    assert [unit["trace_id"] for unit in full["examples"]] == ["selected"]
    comparison = full["context_examples"][0]
    assert comparison["usage"] == "context_only"
    assert comparison["trace_id"] == "comparison"
    assert comparison["source_session_key"] == "recorded-source"
    assert "evidence_ref" not in comparison
    assert comparison["context_trace_ref"] == data["provenance"]["context_trace_refs"][0]
    assert comparison["recorded_query"] == before["analysis_context"]["turns"][1]["prompt_text"]
    assert comparison["recorded_messages"] == before["analysis_context"]["turns"][1]["messages"]
    assert comparison["recorded_turn"] == before["analysis_context"]["turns"][1]
    assert comparison["content_sha256"]["recorded_turn"]
    assert comparison["source_feedback"][0]["structured_payload"] == {"score": 100, "judgment": "PASS"}
    assert metadata["context_examples"] == full["context_examples"]
    assert metadata["coverage"]["context_sha256"] == full["coverage"]["context_sha256"]
    assert "recorded_query" not in metadata["examples"][0]
    assert data == before


def test_global_target_precedence_avoids_context_copies_and_keeps_packet_unchanged():
    first = context_source()
    second = context_source(target_id="comparison", context_id="selected", session_id="other-container")
    # Each selected record's full Context copy is the same original Trace body.
    second["turns"][0] = copy.deepcopy(first["analysis_context"]["turns"][1])
    second["analysis_context"]["turns"][0] = copy.deepcopy(second["turns"][0])
    second["analysis_context"]["turns"][1] = copy.deepcopy(first["turns"][0])
    data = [first, second]
    without_context = [{k: copy.deepcopy(v) for k, v in session.items() if k != "analysis_context"}
                       for session in data]
    packet = build_evidence_context(data)
    assert packet == build_evidence_context(without_context)
    assert "context_examples" not in packet
    assert "context_count" not in packet["coverage"]
    assert "context_trace_refs" not in packet["coverage"]
    first["analysis_context"]["turns"][1]["response_text"] = "A changed Context copy of the selected target"
    with pytest.raises(EvidenceContextError, match="Conflicting target and Context copies"):
        build_evidence_context(data)


def test_same_context_trace_across_carriers_is_counted_once_and_conflicts_fail():
    first = context_source()
    second = context_source(target_id="other-target", session_id="other-container")
    packet = build_evidence_context([first, second])
    assert packet["coverage"]["included_count"] == 2
    assert packet["coverage"]["context_count"] == 1
    assert packet["context_examples"][0]["carrier_session_ids"] == ["selected-container", "other-container"]
    second["analysis_context"]["turns"][1]["prompt_text"] = "A different original query for the same Trace"
    with pytest.raises(EvidenceContextError, match="Conflicting copies of recorded Context"):
        build_evidence_context([first, second])


def test_conflicting_declared_context_revisions_fail_even_when_body_matches():
    first = context_source()
    second = context_source(target_id="other-target", session_id="other-container")
    second["provenance"]["context_trace_refs"][0]["revision"] = "different-recorded-revision"
    second["analysis_context"]["context_trace_refs"][0]["revision"] = "different-recorded-revision"
    with pytest.raises(EvidenceContextError, match="Conflicting recorded Context Trace references"):
        build_evidence_context([first, second])


@pytest.mark.parametrize("missing", ["identity", "trace_identity", "source_declaration", "snapshot_declaration",
                                     "context_only", "turn_declaration", "revision"])
def test_unattested_or_undeclared_old_material_is_not_promoted_to_context(missing):
    data = context_source()
    if missing == "identity":
        data["provenance"]["identity_verified"] = False
    elif missing == "trace_identity":
        data["provenance"]["trace_identity_verified"] = False
    elif missing == "source_declaration":
        data["provenance"]["context_trace_refs"] = []
    elif missing == "snapshot_declaration":
        data["analysis_context"]["context_trace_refs"] = []
    elif missing == "context_only":
        data["analysis_context"]["context_only"] = False
    elif missing == "revision":
        data["provenance"]["context_trace_refs"][0].pop("revision")
    else:
        context = data["analysis_context"]["turns"][1]
        context["request_context"]["trace_input"]["reconstruction"]["constructed_import_batch"] = False
        generation = context["request_context"]["generation_requests"][0]["input"]["reconstruction"]
        generation["constructed_import_batch"] = False
    packet = build_evidence_context([data])
    assert "context_examples" not in packet
    assert "context_count" not in packet["coverage"]
    assert packet["coverage"]["included_count"] == 1


def test_old_isolated_context_does_not_activate_ordinary_skill_target_path():
    data = context_source()
    data["turns"][0] = recorded_turn("selected", isolated=False)
    # Conflicting declarations in old Context remain irrelevant to a normal Skill.
    data["analysis_context"]["turns"][1]["request_context"]["trace_input"]["reconstruction"]["batch_id"] = "conflicting"
    assert not has_isolated_records([data])
    packet = build_evidence_context([data])
    assert "context_examples" not in packet
    assert packet["anchor_required_for_session_ids"] == []


def test_context_reference_must_match_snapshot_and_missing_body_is_not_silently_omitted():
    data = context_source()
    data["analysis_context"]["context_trace_refs"][0]["context_revision"] = "different-prefix"
    with pytest.raises(EvidenceContextError, match="Conflicting recorded Context Trace references"):
        build_evidence_context([data])
    data = context_source()
    data["analysis_context"]["turns"] = []
    with pytest.raises(EvidenceContextError, match="Declared Context Trace is missing"):
        build_evidence_context([data])


def test_context_conflicts_include_unprojected_generation_observations_and_target_metrics():
    first = context_source()
    second = context_source(target_id="other-target", session_id="other-container")
    second["analysis_context"]["turns"][1]["request_context"]["generation_requests"][0]["model"] = "another-model"
    with pytest.raises(EvidenceContextError, match="Conflicting copies of recorded Context"):
        build_evidence_context([first, second])
    data = context_source()
    data["analysis_context"]["turns"][1]["metrics"] = {"total_tokens": 20}
    selected = context_source(target_id="comparison", context_id="selected", session_id="other-container")
    selected["turns"][0] = copy.deepcopy(data["analysis_context"]["turns"][1])
    selected["turns"][0]["metrics"] = {"total_tokens": 10}
    selected["analysis_context"]["turns"][1] = copy.deepcopy(data["turns"][0])
    with pytest.raises(EvidenceContextError, match="Conflicting target and Context copies"):
        build_evidence_context([data, selected])


def test_context_copy_does_not_conflict_with_split_numbering_or_platform_prm_annotation():
    first = context_source()
    second = context_source(target_id="comparison", context_id="selected", session_id="other-container")
    second["turns"][0] = copy.deepcopy(first["analysis_context"]["turns"][1])
    second["turns"][0].update(turn_num=1, prm_score=0.8)
    second["analysis_context"]["turns"][1] = copy.deepcopy(first["turns"][0])
    packet = build_evidence_context([first, second])
    assert "context_examples" not in packet
    assert packet["coverage"]["included_count"] == 2
