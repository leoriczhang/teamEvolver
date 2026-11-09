"""Live request anchors require exact source proof, never an isolated-record flag."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_replay.datasets.recorded_anchor import bind_verified_request_anchors, materialize_recorded_anchor_queries
from teamEvolver.evidence_context import EvidenceContextError, build_evidence_context
from tests.support.shared_template_sources import shared_source_cases, shared_template_plan, shared_template_source


def live_packet():
    sources = [shared_template_source(1), shared_template_source(2)]
    for source in sources:
        source["turns"][0]["request_context"]["trace_input"].pop("reconstruction")
    plan = shared_template_plan()
    packet = bind_verified_request_anchors(build_evidence_context(sources), sources, plan)
    return sources, plan, packet, shared_source_cases(packet)


def refresh(packet):
    packet["coverage"].pop("context_sha256")
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    packet["coverage"]["context_sha256"] = hashlib.sha256(raw.encode()).hexdigest()


def test_live_request_materialization_preserves_recorded_sequence_and_original_bytes():
    sources, plan, packet, raw = live_packet()
    before = copy.deepcopy((sources, plan, packet, raw))
    parsed, receipts = materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sources,
                                                          baseline=plan, enabled=True)
    assert len(receipts) == 2 and packet["anchor_required_for_session_ids"] == []
    assert all(example["relationship"] == "recorded_session_turn" for example in packet["examples"])
    assert all(example["sequence_semantics"] == "recorded_session_order" for example in packet["examples"])
    assert [case["query"] for case in parsed["test_datasets"]] == [source["turns"][0]["prompt_text"]
                                                                for source in sources]
    assert (sources, plan, packet, raw) == before


@pytest.mark.parametrize("mutation", [
    "missing_plan", "changed_model_request", "missing_model_request", "unverified_source", "wrong_runtime",
    "wrong_profile", "changed_template_version", "packet_variables", "context_target", "isolation_claim",
])
def test_live_anchor_packet_markers_cannot_substitute_the_original_request_or_its_frozen_template(mutation):
    sources, plan, packet, raw = live_packet()
    request = sources[0]["turns"][0]["request_context"]
    if mutation == "missing_plan":
        plan = None
    elif mutation == "changed_model_request":
        request["generation_requests"][0]["input"]["messages"][0]["content"] += "altered"
    elif mutation == "missing_model_request":
        request["generation_requests"] = []
    elif mutation == "unverified_source":
        sources[0]["provenance"]["identity_verified"] = False
    elif mutation == "wrong_runtime":
        sources[0]["runtime"]["type"] = "another-runtime"
    elif mutation == "wrong_profile":
        sources[0]["runtime_context"]["execution_profile_id"] = "another-profile"
    elif mutation == "changed_template_version":
        request["trace_input"]["template_inputs"]["template_refs"]["main-prompt"]["revision"] = "2"
    elif mutation == "packet_variables":
        packet["examples"][0]["recorded_context"]["template_inputs"]["template_variables"]["aiTask"] = "forged"
    elif mutation == "context_target":
        sources[0]["provenance"]["target_trace_refs"] = []
    elif mutation == "isolation_claim":
        packet["examples"][0]["relationship"] = "isolated_record"
        packet["anchor_required_for_session_ids"] = ["source-1"]
        packet["verified_request_anchor_ids"].remove(packet["examples"][0]["example_id"])
    refresh(packet)  # An internally consistent packet still cannot replace the source facts.
    with pytest.raises(EvidenceContextError):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sources, baseline=plan, enabled=True)


def test_only_verified_live_targets_are_enabled_and_existing_skill_inputs_are_unchanged():
    sources, plan, _, _ = live_packet()
    sources[0]["turns"][0]["request_context"]["generation_requests"] = []
    packet = build_evidence_context(sources)
    assert bind_verified_request_anchors(packet, sources, None) is packet
    enabled = bind_verified_request_anchors(packet, sources, plan)
    anchors = enabled["verified_request_anchor_ids"]
    assert anchors == [example["example_id"] for example in packet["examples"] if example["session_id"] == "source-2"]
    assert "verified_request_anchor_ids" not in packet
