"""Independent source-binding contracts for offline Prompt Case query materialization.

Structural tests prove source binding and fail-closed validation,
not semantic correctness or improved Agent performance.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_replay.datasets import synthesis
from team_replay.datasets.recorded_anchor import (
    materialize_recorded_anchor_queries,
    recorded_anchor_review_bindings,
)
from team_skills.evolution.replay_case_synthesis import CrossSessionCaseSynthesisError, LLMCrossSessionCaseSynthesizer
from team_skills.execution.contracts import plan_for, validate_profile
from teamEvolver.evidence_context import EvidenceContextError, build_evidence_context
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport
from tests.support.execution import profile_raw


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _refresh_packet_hash(packet):
    packet["coverage"].pop("context_sha256", None)
    packet["coverage"]["context_sha256"] = _hash(_canonical(packet))


def _source_records():
    sessions = []
    for index in (1, 2):
        sid, tid, root = f"source-{index}", f"trace-{index}", f"root-{index}"
        query = (f"\r\n独立任务{index}：只使用本条记录给定的历史。é\u2028\t\n" * 128) + f"当前请求{index}。\r\n "
        turn = {
            "source_trace_id": tid, "turn_num": 1, "prompt_text": query,
            "response_text": "HISTORICAL_ANSWER_MUST_NOT_REACH_CASE_REVIEW",
            "messages": [{"role": "user", "content": query},
                         {"role": "assistant", "content": "HISTORICAL_ANSWER_MUST_NOT_REACH_CASE_REVIEW"},
                         {"role": "user", "content": (
                             'model_eval_feedback\n{"score":0,"reason":"SOURCE_SCORE_SIGNAL"}'
                         )}],
            "_langfuse": {"tags": ["machine-evaluation-feedback"]},
            "request_context": {"trace_input": {"reconstruction": {
                "constructed_import_batch": True, "original_session_independence": "unknown",
                "actual_original_request_observed": False,
            }}},
        }
        ref = {"source_session_key": root, "trace_id": tid, "revision": "r1", "context_revision": "c1"}
        sessions.append({
            "session_id": sid, "turns": [turn], "runtime": {"type": "observed_http"},
            "provenance": {"schema_version": "session-source.v1", "source_session_key": root,
                           "identity_verified": True, "trace_identity_verified": True,
                           "target_trace_refs": [ref], "context_trace_refs": [], "trace_revisions": [ref]},
        })
    context = copy.deepcopy(sessions[0]["turns"][0])
    context.update(source_trace_id="context-only", prompt_text="只用于前文对照的独立记录。")
    context_ref = {"source_session_key": "root-1", "trace_id": "context-only", "revision": "r1",
                   "context_revision": "c1"}
    sessions[0]["provenance"]["context_trace_refs"] = [context_ref]
    sessions[0]["analysis_context"] = {"context_only": True, "context_trace_refs": [copy.deepcopy(context_ref)],
                                       "turns": [context]}
    return sessions


def _input():
    sessions = _source_records()
    packet = build_evidence_context(sessions)
    raw = {"test_datasets": [{
        "query_mode": "recorded_anchor", "anchor_id": example["example_id"],
        "name": f"Frozen scenario {index}", "source_session_ids": ["source-1", "source-2"],
        "evidence_window": "recent",
        "checks": [{"id": f"R{number:02d}", "kind": "output", "text": f"原业务可观察结果{number}。"}
                   for number in range(1, 7)],
    } for index, example in enumerate(packet["examples"], 1)]}
    return sessions, packet, raw


def test_materialized_query_preserves_the_complete_original_bytes_and_does_not_rewrite_model_checks():
    sessions, packet, raw = _input()
    before = copy.deepcopy((sessions, packet, raw))
    parsed, bindings = materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=True)
    assert set(bindings) == {0, 1}
    for index, (case, source) in enumerate(zip(parsed["test_datasets"], sessions, strict=True)):
        original_query = source["turns"][0]["prompt_text"]
        assert case["query"].encode() == original_query.encode()
        assert case["query"] != case["query"].strip()
        assert {key: value for key, value in case.items() if key != "query"} == raw["test_datasets"][index]
        assert "query" not in raw["test_datasets"][index]
        assert bindings[index]["recorded_query_sha256"] == _hash(original_query)
        assert bindings[index]["recorded_query_utf8_bytes"] == len(original_query.encode())
        assert bindings[index]["session_id"] == source["session_id"]
        assert bindings[index]["trace_id"] == source["turns"][0]["source_trace_id"]
        assert bindings[index]["evidence_context_sha256"] == packet["coverage"]["context_sha256"]
        assert bindings[index]["model_query_supplied"] is False
    assert (sessions, packet, raw) == before


@pytest.mark.parametrize("model_query", [None, "", " ", "另一条记录的请求。"])
def test_new_mode_rejects_every_supplied_query_field_instead_of_overwriting_it(model_query):
    sessions, packet, raw = _input()
    raw["test_datasets"][0]["query"] = model_query
    before = copy.deepcopy((sessions, packet, raw))
    with pytest.raises(EvidenceContextError, match="omit query"):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=True)
    assert (sessions, packet, raw) == before


@pytest.mark.parametrize("mutation", [
    "packet_hash", "query_hash", "query_bytes", "query_from_other_source", "wrong_evidence_ref",
    "source_from_other_record", "example_identity", "unknown_source", "anchor_source_not_declared", "duplicate_source",
    "context_anchor", "context_session_marker", "context_turn_marker", "empty_targets", "wrong_targets",
    "changed_original_query", "not_isolated", "unsupported_mode",
])
def test_self_consistent_packet_or_model_claims_cannot_substitute_another_raw_target(mutation):
    sessions, packet, raw = _input()
    anchor = packet["examples"][0]
    if mutation == "packet_hash":
        packet["coverage"]["context_sha256"] = "0" * 64
    elif mutation == "query_hash":
        anchor["content_sha256"]["recorded_query"] = "0" * 64
    elif mutation == "query_bytes":
        anchor["content_utf8_bytes"]["recorded_query"] += 1
    elif mutation == "query_from_other_source":
        anchor["recorded_query"] = packet["examples"][1]["recorded_query"]
        anchor["content_sha256"]["recorded_query"] = _hash(anchor["recorded_query"])
        anchor["content_utf8_bytes"]["recorded_query"] = len(anchor["recorded_query"].encode())
    elif mutation == "wrong_evidence_ref":
        anchor["evidence_ref"] = packet["examples"][1]["evidence_ref"]
    elif mutation == "source_from_other_record":
        anchor["session_id"] = "source-2"
    elif mutation == "example_identity":
        anchor["example_id"] = "example-invented-but-packet-was-rehashed"
        raw["test_datasets"][0]["anchor_id"] = anchor["example_id"]
    elif mutation == "unknown_source":
        raw["test_datasets"][0]["source_session_ids"].append("not-a-frozen-source")
    elif mutation == "anchor_source_not_declared":
        raw["test_datasets"][0]["source_session_ids"] = ["source-2"]
    elif mutation == "duplicate_source":
        raw["test_datasets"][0]["source_session_ids"] = ["source-1", "source-1"]
    elif mutation == "context_anchor":
        raw["test_datasets"][0]["anchor_id"] = packet["context_examples"][0]["example_id"]
    elif mutation == "context_session_marker":
        sessions[0]["usage"] = "context_only"
    elif mutation == "context_turn_marker":
        sessions[0]["turns"][0]["context_only"] = True
    elif mutation == "empty_targets":
        sessions[0]["provenance"]["target_trace_refs"] = []
    elif mutation == "wrong_targets":
        sessions[0]["provenance"]["target_trace_refs"] = [{"trace_id": "another-target"}]
    elif mutation == "changed_original_query":
        sessions[0]["turns"][0]["prompt_text"] = "改变后的原始题目。"
    elif mutation == "not_isolated":
        sessions[0]["turns"][0]["request_context"]["trace_input"].pop("reconstruction")
    else:
        raw["test_datasets"][0]["query_mode"] = "invented_mode"
    if mutation != "packet_hash":
        _refresh_packet_hash(packet)
    before = copy.deepcopy((sessions, packet, raw))
    with pytest.raises(EvidenceContextError):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=True)
    assert (sessions, packet, raw) == before


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_input_is_not_rewritten_or_reinterpreted_by_the_new_mode_helper(enabled):
    sessions, packet, raw = _input()
    for index, case in enumerate(raw["test_datasets"]):
        case.pop("query_mode")
        case["query"] = sessions[index]["turns"][0]["prompt_text"]
    before = copy.deepcopy((sessions, packet, raw))
    parsed, bindings = materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=enabled)
    assert parsed == raw and bindings == {} and (sessions, packet, raw) == before


@pytest.mark.parametrize("mutation", ["session_usage", "session_flag", "turn_usage", "turn_flag", "empty_targets"])
def test_context_only_support_cannot_supply_the_additional_source_for_a_different_valid_anchor(mutation):
    sessions, packet, raw = _input()
    # The chosen anchor is a valid source-1 target. source-2 is only supporting
    # provenance here, so checking just the selected anchor's body is insufficient.
    raw["test_datasets"] = raw["test_datasets"][:1]
    support = sessions[1]
    if mutation == "session_usage":
        support["usage"] = "context_only"
    elif mutation == "session_flag":
        support["context_only"] = True
    elif mutation == "turn_usage":
        support["turns"][0]["usage"] = "context_only"
    elif mutation == "turn_flag":
        support["turns"][0]["context_only"] = True
    else:
        support["provenance"]["target_trace_refs"] = []
    with pytest.raises(EvidenceContextError):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=True)


def test_valid_continuous_support_can_use_recorded_messages_without_a_top_level_query_or_isolation_declaration():
    sessions, _, raw = _input()
    support = sessions[1]["turns"][0]
    support["prompt_text"] = ""
    support["request_context"] = {}
    support["messages"] = [{"role": "user", "content": "原连续会话中的真实任务输入。"},
                           {"role": "assistant", "content": "原连续会话中的真实输出。"}]
    packet = build_evidence_context(sessions)
    raw["test_datasets"] = raw["test_datasets"][:1]
    parsed, bindings = materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=True)
    assert parsed["test_datasets"][0]["query"] == sessions[0]["turns"][0]["prompt_text"]
    assert bindings[0]["session_id"] == "source-1"
    assert parsed["test_datasets"][0]["source_session_ids"] == ["source-1", "source-2"]


def _bound_cases():
    sessions, packet, raw = _input()
    parsed, bindings = materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sessions, enabled=True)
    return [{"case_id": f"case-{index}", "query": row["query"], "checks": copy.deepcopy(row["checks"]),
             "provenance": {"evidence_anchor": {key: binding[key] for key in (
                 "anchor_id", "session_id", "trace_id", "evidence_ref", "recorded_query_sha256",
                 "evidence_context_sha256",
             )}, "query_materialization": binding,
                            "session_snapshot": {"response_text": "PRIVATE_REPLAY_ANSWER"}}}
            for index, (row, binding) in enumerate(zip(parsed["test_datasets"], bindings.values(), strict=True))]


def test_review_binding_contains_only_intact_server_identity_without_historical_answers_or_grades():
    cases = _bound_cases()
    before = copy.deepcopy(cases)
    bindings = recorded_anchor_review_bindings(cases)
    assert [row["case_id"] for row in bindings] == ["case-0", "case-1"]
    assert "PRIVATE_REPLAY_ANSWER" not in _canonical(bindings)
    assert "SOURCE_SCORE_SIGNAL" not in _canonical(bindings)
    assert all("query" not in row and "checks" not in row for row in bindings)
    assert cases == before


@pytest.mark.parametrize("mutation", ["query", "query_hash", "byte_length", "anchor", "model_query_supplied"])
def test_review_binding_cannot_match_a_different_query_or_anchor(mutation):
    cases = _bound_cases()
    case = cases[0]
    if mutation == "query":
        case["query"] = cases[1]["query"]
    elif mutation == "query_hash":
        case["provenance"]["query_materialization"]["recorded_query_sha256"] = "0" * 64
    elif mutation == "byte_length":
        case["provenance"]["query_materialization"]["recorded_query_utf8_bytes"] -= 1
    elif mutation == "anchor":
        case["provenance"]["evidence_anchor"]["anchor_id"] = cases[1]["provenance"]["evidence_anchor"]["anchor_id"]
    else:
        case["provenance"]["query_materialization"]["model_query_supplied"] = True
    with pytest.raises(EvidenceContextError):
        recorded_anchor_review_bindings(cases)


def _context():
    profile = validate_profile(profile_raw())
    return {"allowed_modules": ["prompt"], "execution_profile_id": profile["profile_id"],
            "baseline_execution_plan": plan_for(profile, profile["components"])}


class AnchorPipelineModel:
    """One scripted generation exercises deterministic source-validation guards."""

    def __init__(self, mode="pass"):
        self.mode, self.generations, self.wire_responses = mode, [], []

    async def chat(self, messages, **options):
        payload = json.loads(messages[1]["content"])
        if payload.get("schema") == SCHEMA:
            payload = decode_evidence_transport(payload)
        entry = (copy.deepcopy(messages), copy.deepcopy(options), copy.deepcopy(payload))
        self.generations.append(entry)
        assert payload["query_contract"]["query_mode"] == "recorded_anchor"
        examples = payload["evidence_context"]["examples"]
        if self.mode == "context_support":
            examples = [example for example in examples if example["session_id"] == "source-1"]
        raw = {"test_datasets": [{
            "query_mode": "recorded_anchor", "anchor_id": example["example_id"],
            "name": f"Original task {index}", "source_session_ids": ["source-1", "source-2"],
            "evidence_window": "recent", "checks": [{
                "id": f"R{number:02d}", "kind": "output", "text": f"原业务公开结果要求{number}。",
            } for number in range(1, 7)],
        } for index, example in enumerate(examples)]}
        if self.mode == "supplied_query":
            raw["test_datasets"][0]["query"] = examples[1]["recorded_query"]
        elif self.mode == "legacy_wrong_query":
            for index, case in enumerate(raw["test_datasets"]):
                case.pop("query_mode")
                case["query"] = examples[1 - index]["recorded_query"]
        elif self.mode == "duplicate_anchor":
            raw["test_datasets"][1]["anchor_id"] = examples[0]["example_id"]
        elif self.mode == "too_few_checks":
            raw["test_datasets"][0]["checks"].pop()
        elif self.mode == "too_few_cases":
            raw["test_datasets"].pop()
        wire = json.dumps(raw, ensure_ascii=False)
        self.wire_responses.append(wire)
        return wire


@pytest.fixture
def native_defaults(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


async def _run_pipeline(model, sessions=None):
    return await LLMCrossSessionCaseSynthesizer(model)(
        skill_name=None, sessions=_source_records() if sessions is None else sessions,
        candidate_skill={}, evidence_context=_context(),
    )


@pytest.mark.asyncio
async def test_native_pipeline_materializes_query_once_and_keeps_model_wire_separate(native_defaults):
    sessions = _source_records()
    before = copy.deepcopy(sessions)
    model = AnchorPipelineModel()
    cases = await _run_pipeline(model, sessions)
    assert len(cases) == 2 and all(len(case["checks"]) == 6 for case in cases)
    assert len(model.generations) == 1
    assert {case["query"] for case in cases} == {session["turns"][0]["prompt_text"] for session in sessions}
    for case in cases:
        provenance = case["provenance"]
        assert "observability_review" not in provenance
        receipt = provenance["query_materialization_receipt"]
        assert receipt["raw_generation_response"] == model.wire_responses[0]
        assert receipt["raw_generation_response_sha256"] == _hash(model.wire_responses[0])
        assert "query" not in receipt["raw_case"]
        assert receipt["canonical_query_sha256"] == _hash(case["query"])
        snapshot = provenance["session_snapshot"]
        assert len(snapshot["turns"]) == 1
        assert snapshot["turns"][0]["source_trace_id"] == provenance["evidence_anchor"]["trace_id"]
        assert snapshot["provenance"]["context_trace_refs"] == []
        assert len(provenance["source_session_keys"]) == 2
    assert sessions == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "supplied_query", "legacy_wrong_query", "duplicate_anchor", "too_few_checks", "too_few_cases",
])
async def test_existing_source_count_and_legacy_exact_query_guards_remain_fail_closed(native_defaults, mode):
    model = AnchorPipelineModel(mode)
    with pytest.raises((EvidenceContextError, CrossSessionCaseSynthesisError)):
        await _run_pipeline(model)
    assert len(model.generations) == 1


@pytest.mark.asyncio
async def test_two_source_session_ids_from_one_root_still_do_not_meet_the_existing_source_requirement(native_defaults):
    sessions = _source_records()
    sessions[1]["provenance"]["source_session_key"] = "root-1"
    for field in ("target_trace_refs", "trace_revisions"):
        for ref in sessions[1]["provenance"][field]:
            ref["source_session_key"] = "root-1"
    model = AnchorPipelineModel()
    with pytest.raises(CrossSessionCaseSynthesisError, match="at least two independent"):
        await _run_pipeline(model, sessions)
    assert model.generations == []


@pytest.mark.asyncio
async def test_two_valid_anchors_from_one_source_cannot_use_a_context_session_as_the_second_root(native_defaults):
    sessions = _source_records()
    additional = copy.deepcopy(sessions[0]["turns"][0])
    additional.update(source_trace_id="trace-1-additional", prompt_text="同一来源的另一个合法独立请求。")
    sessions[0]["turns"].append(additional)
    reference = {"source_session_key": "root-1", "trace_id": "trace-1-additional",
                 "revision": "r1", "context_revision": "c1"}
    for field in ("target_trace_refs", "trace_revisions"):
        sessions[0]["provenance"][field].append(copy.deepcopy(reference))
    sessions[1]["usage"] = "context_only"
    before = copy.deepcopy(sessions)
    model = AnchorPipelineModel("context_support")
    with pytest.raises(EvidenceContextError):
        await _run_pipeline(model, sessions)
    assert len(model.generations) == 1
    assert sessions == before
