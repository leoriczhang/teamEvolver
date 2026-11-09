"""Preserve recorded task boundaries when synthesizing offline Prompt Cases."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_replay.datasets import synthesis
from team_skills.evolution.replay_case_synthesis import CrossSessionCaseSynthesisError, LLMCrossSessionCaseSynthesizer
from teamEvolver.evidence_context import EvidenceContextError, build_evidence_context
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport


@pytest.fixture
def source_sessions():
    output = []
    for number in (1, 2):
        query = f"以下是本轮之前的对话历史：\n（暂无历史对话）\n当前 HWP 消息：\n周六活动{number}能参加吗？"
        feedback = 'model_eval_feedback\n{"综合得分":10,"得分说明":"没有回应当前邀约"}'
        reconstruction = {
            "constructed_import_batch": True,
            "original_session_independence": "unknown",
            "actual_original_request_observed": False,
        }
        output.append({
            "session_id": f"s{number}",
            "_summary": "An analytical summary, not a replacement for recorded history",
            "_trajectory": "An analytical trajectory",
            "turns": [{
                "trace_id": f"t{number}", "turn_num": 1,
                "prompt_text": query, "response_text": "你好，请问哪位？",
                "messages": [
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": "你好，请问哪位？"},
                    {"role": "user", "content": feedback},
                ],
                "_langfuse": {"tags": ["machine-evaluation-feedback"]},
                "request_context": {
                    "trace_input": {
                        "business_context": {"ctx_chatHistory": "", "ctx_aiTask": "首次邀约拒绝，再次触发选择结束消息"},
                        "reconstruction": copy.deepcopy(reconstruction),
                    },
                    "generation_requests": [{"input": {"reconstruction": copy.deepcopy(reconstruction)}}],
                },
            }],
        })
    return output


@pytest.fixture(autouse=True)
def native_stage_defaults(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


def ordinary_sessions(source_sessions):
    values = copy.deepcopy(source_sessions)
    for session in values:
        request = session["turns"][0]["request_context"]
        request["trace_input"].pop("reconstruction")
        request.pop("generation_requests")
    return values


def with_context_only_positive(source_sessions):
    values = copy.deepcopy(source_sessions)
    for session in values:
        sid, tid = session["session_id"], session["turns"][0]["trace_id"]
        ref = {"source_session_key": sid, "trace_id": tid, "revision": "v1", "context_revision": "c1"}
        session["provenance"] = {
            "schema_version": "session-source.v1", "source_session_key": sid,
            "identity_verified": True, "trace_identity_verified": True,
            "target_trace_refs": [ref], "context_trace_refs": [], "trace_revisions": [ref],
        }
    positive = copy.deepcopy(values[0]["turns"][0])
    positive.update(
        trace_id="context-pass", prompt_text="当前邀请我周六参加活动。", response_text="周六我有安排，先看看时间。",
    )
    positive["messages"] = [
        {"role": "user", "content": positive["prompt_text"]},
        {"role": "assistant", "content": positive["response_text"]},
        {"role": "user", "content": 'model_eval_feedback\n{"综合得分":100,"得分说明":"原始正常回应"}'},
    ]
    context_ref = {"source_session_key": "s1", "trace_id": "context-pass", "revision": "v1", "context_revision": "c1"}
    values[0]["provenance"]["context_trace_refs"] = [context_ref]
    values[0]["analysis_context"] = {
        "context_only": True, "context_trace_refs": [copy.deepcopy(context_ref)],
        "turns": [copy.deepcopy(values[0]["turns"][0]), positive],
    }
    return values


class Model:
    def __init__(self, mode="valid"):
        self.mode = mode
        self.calls = 0
        self.system = ""
        self.payload = {}
        self.wire_payload = {}

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.system = messages[0]["content"]
        self.wire_payload = json.loads(messages[1]["content"])
        self.payload = (decode_evidence_transport(self.wire_payload)
                        if self.wire_payload.get("schema") == SCHEMA else self.wire_payload)
        if self.mode == "provider_error":
            raise RuntimeError("Temporary model error")
        if self.mode == "no_cases":
            return '{"test_datasets":[]}'
        if self.mode == "invalid_json":
            return "This is not a JSON Case result"
        evidence = self.payload.get("evidence_context") or {}
        examples = evidence.get("examples") or []
        cases = []
        for index in range(2):
            anchor = examples[0 if self.mode == "same_anchor" else index] if examples else None
            case = {
                "name": f"Shared failure pattern {index}",
                "query": anchor["recorded_query"] if anchor else f"Representative task {index}",
                "source_session_ids": ["s1", "s2"],
                "checks": [{"id": f"R{number}", "kind": "output", "text": f"Grounded requirement {number}"}
                           for number in range(7)],
            }
            if anchor and anchor["relationship"] == "isolated_record":
                case["anchor_id"] = anchor["example_id"]
            if self.mode == "invented_history":
                case["query"] += "\nC：我们之前已经约好了周日晚上电话聊。"
            elif self.mode == "missing_anchor":
                case.pop("anchor_id", None)
            elif self.mode == "wrong_anchor":
                case["anchor_id"] = "unknown-record"
            elif self.mode == "wrong_source" and index == 1:
                case["source_session_ids"] = ["s1", "other"]
            elif self.mode == "padded_source_without_anchor":
                case["source_session_ids"] = [" s1 ", " s2 "]
                case.pop("anchor_id", None)
                case["query"] = "A fabricated conversation not present in the evidence"
            elif self.mode == "padded_wrong_source" and index == 1:
                case["source_session_ids"] = [" s1 ", " other "]
            elif self.mode == "padded_source_valid":
                case["source_session_ids"] = [" s1 ", " s2 "]
            elif self.mode == "context_anchor":
                context = evidence["context_examples"][0]
                case.update(anchor_id=context["example_id"], query=context["recorded_query"])
            elif self.mode == "invalid_checks":
                case["checks"] = case["checks"][:3]
            cases.append(case)
        return json.dumps({"test_datasets": cases}, ensure_ascii=False)


async def synthesize(model, sessions, context=None):
    return await synthesis.synthesize_evolution_datasets(
        model, skill_name="demo", sessions=sessions, candidate_skill={},
        evidence_context=context, case_count=2, min_requirements=6, max_requirements=8, min_source_sessions=2,
    )


@pytest.mark.asyncio
async def test_default_skill_synthesis_keeps_original_compact_contract(source_sessions, monkeypatch):
    sessions = ordinary_sessions(source_sessions)
    model = Model()
    # The additional Evidence budget must not constrain the original Skill path.
    monkeypatch.setattr(synthesis, "_MAX_SYNTHESIS_EVIDENCE_BYTES", 1)
    cases = await synthesize(model, sessions)
    assert len(cases) == 2
    assert "evidence_context" not in model.payload
    assert model.system == synthesis.render_synthesis_prompt(
        synthesis._SYNTHESIZE_SYSTEM, case_count=2, min_requirements=6,
        max_requirements=8, min_source_sessions=2,
    )
    assert model.payload["sessions"] == [synthesis._session_payload(s) for s in sessions]
    assert all("evidence_anchor" not in c["provenance"] for c in cases)


@pytest.mark.asyncio
async def test_offline_cases_bind_exact_query_without_losing_feedback_or_task_context(source_sessions):
    before = copy.deepcopy(source_sessions)
    model = Model()
    cases = await synthesize(model, source_sessions)
    packet = model.payload["evidence_context"]
    assert packet == build_evidence_context(source_sessions)
    assert len(cases) == 2 and all(len(case["checks"]) == 7 for case in cases)
    assert "candidate_skill" not in model.payload
    for example, case in zip(packet["examples"], cases, strict=True):
        assert case["query"] == example["recorded_query"]
        assert example["recorded_context"]["business_context"]["ctx_chatHistory"] == ""
        assert example["recorded_context"]["business_context"]["ctx_aiTask"] == "首次邀约拒绝，再次触发选择结束消息"
        assert example["source_feedback"][0]["structured_payload"]["综合得分"] == 10
        assert example["source_feedback"][0]["independently_verified"] is False
        anchor = case["provenance"]["evidence_anchor"]
        assert anchor["anchor_id"] == example["example_id"]
        assert anchor["trace_id"] == example["trace_id"]
        assert anchor["evidence_ref"] == example["evidence_ref"]
        assert anchor["session_id"] in case["provenance"]["session_ids"]
        assert anchor["recorded_query_sha256"] == hashlib.sha256(case["query"].encode()).hexdigest()
        assert anchor["evidence_context_sha256"] == packet["coverage"]["context_sha256"]
    assert source_sessions == before
    assert model.calls == 1
    assert "context_examples" not in packet
    assert model.system == synthesis.render_synthesis_prompt(
        synthesis._SYNTHESIZE_SYSTEM, case_count=2, min_requirements=6,
        max_requirements=8, min_source_sessions=2,
    ) + synthesis._PROMPT_EVIDENCE_SYSTEM


@pytest.mark.asyncio
async def test_verified_positive_context_is_preserved_without_becoming_target_evidence(source_sessions):
    sources = with_context_only_positive(source_sessions)
    before = copy.deepcopy(sources)
    original_packet = build_evidence_context(sources)
    model = Model()
    cases = await synthesize(model, sources)
    packet = model.payload["evidence_context"]
    assert packet == original_packet
    assert len(packet["examples"]) == packet["coverage"]["included_count"] == 2
    assert packet["coverage"]["source_session_ids"] == ["s1", "s2"]
    assert packet["anchor_required_for_session_ids"] == ["s1", "s2"]
    context = packet["context_examples"][0]
    assert context["usage"] == "context_only" and "evidence_ref" not in context
    assert context["source_feedback"][0]["structured_payload"]["综合得分"] == 100
    assert synthesis._CONTEXT_EXAMPLES_SYSTEM in model.system
    assert len(cases) == 2 and all(len(c["checks"]) == 7 for c in cases)
    assert all(c["provenance"]["evidence_anchor"]["anchor_id"] != context["example_id"] for c in cases)
    assert sources == before


@pytest.mark.asyncio
async def test_context_only_positive_cannot_be_selected_as_a_case_anchor(source_sessions):
    with pytest.raises(EvidenceContextError, match="valid anchor"):
        await synthesize(Model("context_anchor"), with_context_only_positive(source_sessions))


@pytest.mark.asyncio
async def test_prompt_bound_recorded_sessions_keep_continuity_and_do_not_require_offline_anchor(source_sessions):
    model = Model()
    cases = await synthesize(model, ordinary_sessions(source_sessions), {"allowed_modules": ["prompt"]})
    packet = model.payload["evidence_context"]
    assert packet["anchor_required_for_session_ids"] == []
    assert all(e["relationship"] == "recorded_session_turn" for e in packet["examples"])
    assert all("evidence_anchor" not in c["provenance"] for c in cases)
    assert len(cases) == 2 and model.calls == 1
    assert synthesis._PROMPT_CASE_SYSTEM in model.system


@pytest.mark.asyncio
async def test_evidence_guidance_explains_existing_independent_root_requirement(source_sessions):
    model = Model()
    roots = {"s1": "recorded-root-a", "s2": "recorded-root-b"}
    await synthesize(model, source_sessions, {"allowed_modules": ["prompt"], "source_session_keys": roots})
    assert synthesis._SOURCE_ROOTS_SYSTEM.replace("{min_source_sessions}", "2") in model.system
    assert model.payload["team_sop_evidence"]["context"]["source_session_keys"] == roots
    assert len(model.payload["evidence_context"]["examples"]) == 2


@pytest.mark.asyncio
async def test_ordinary_skill_root_mapping_does_not_change_its_existing_prompt(source_sessions):
    model = Model()
    await synthesize(model, ordinary_sessions(source_sessions), {"source_session_keys": {"s1": "r1", "s2": "r2"}})
    assert model.system == synthesis.render_synthesis_prompt(
        synthesis._SYNTHESIZE_SYSTEM, case_count=2, min_requirements=6,
        max_requirements=8, min_source_sessions=2,
    )


@pytest.mark.asyncio
async def test_two_selected_sibling_segments_still_fail_when_other_roots_are_available(source_sessions):
    sources = copy.deepcopy(source_sessions)
    sources[0]["metadata"] = {"split_from_session": "same-upstream-session"}
    sources[1]["metadata"] = {"split_from_session": "same-upstream-session"}
    third = copy.deepcopy(sources[1])
    third["session_id"] = "s3"
    third["metadata"] = {"split_from_session": "another-upstream-session"}
    third["turns"][0]["trace_id"] = "t3"
    sources.append(third)
    before = copy.deepcopy(sources)
    model = Model()  # Deliberately returns s1+s2 despite the available distinct s3 root.
    with pytest.raises(CrossSessionCaseSynthesisError, match="independent"):
        await LLMCrossSessionCaseSynthesizer(model)(
            skill_name=None, sessions=sources, candidate_skill={}, evidence_context={"allowed_modules": ["prompt"]},
        )
    assert model.calls == 1
    root_map = model.payload["team_sop_evidence"]["context"]["source_session_keys"]
    assert root_map["s1"] == root_map["s2"] != root_map["s3"]
    assert sources == before


@pytest.mark.asyncio
async def test_online_prompt_without_grounded_requirements_cannot_invent_fallback_checks(source_sessions):
    model = Model("invalid_json")
    with pytest.raises(CrossSessionCaseSynthesisError, match="exactly two"):
        await LLMCrossSessionCaseSynthesizer(model)(
            skill_name=None, sessions=ordinary_sessions(source_sessions), candidate_skill={},
            evidence_context={"execution_profile_id": "profile"},
        )
    assert model.calls == 1


@pytest.mark.asyncio
async def test_ordinary_skill_keeps_its_existing_grounded_fallback(source_sessions):
    sessions = ordinary_sessions(source_sessions)
    for session in sessions:
        session["turns"][0]["prompt_text"] += "\n### 要求\n" + "\n".join(
            f"{number}. Recorded requirement {number}" for number in range(6)
        )
    model = Model("invalid_json")
    cases = await synthesize(model, sessions)
    assert len(cases) == 2
    assert all(case["provenance"]["synthesis_mode"] == "grounded_fallback" for case in cases)
    assert model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "invented_history", "missing_anchor", "wrong_anchor", "wrong_source",
    "invalid_checks", "no_cases", "provider_error", "same_anchor",
    "padded_source_without_anchor", "padded_wrong_source",
])
async def test_invalid_offline_generation_cannot_be_replaced_by_fallback(source_sessions, mode):
    model = Model(mode)
    with pytest.raises(EvidenceContextError):
        await synthesize(model, source_sessions)
    assert model.calls == 1


@pytest.mark.asyncio
async def test_normalized_source_ids_still_require_the_matching_record_anchor(source_sessions):
    cases = await synthesize(Model("padded_source_valid"), source_sessions)
    assert all(case["provenance"]["session_ids"] == ["s1", "s2"] for case in cases)
    assert [case["provenance"]["evidence_anchor"]["session_id"] for case in cases] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_normalization_preserves_the_verified_anchor_query_bytes(source_sessions):
    query = " \n 这是带空白的原始问题：周六能参加吗？ \n"
    source_sessions[0]["turns"][0]["prompt_text"] = query
    cases = await synthesize(Model(), source_sessions)
    assert cases[0]["query"].encode() == query.encode()
    query_hash = cases[0]["provenance"]["evidence_anchor"]["recorded_query_sha256"]
    assert query_hash == hashlib.sha256(query.encode()).hexdigest()


@pytest.mark.asyncio
async def test_combined_system_and_evidence_budget_fails_before_dispatch(source_sessions, monkeypatch):
    packet_bytes = len(json.dumps(build_evidence_context(source_sessions), ensure_ascii=False).encode())
    monkeypatch.setattr(synthesis, "_MAX_SYNTHESIS_EVIDENCE_BYTES", packet_bytes + 128)
    model = Model()
    with pytest.raises(EvidenceContextError, match="system and evidence payload"):
        await synthesize(model, source_sessions)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_repeated_system_messages_are_shared_without_removing_recorded_evidence(source_sessions):
    system = "Recorded original System instructions. " * 16_000
    for source in source_sessions:
        source["turns"][0]["messages"].insert(0, {"role": "system", "content": system})
    packet = build_evidence_context(source_sessions, max_bytes=8 * 1024 * 1024)
    assert len(json.dumps(packet, ensure_ascii=False).encode()) > 786_432
    model = Model()
    cases = await synthesize(model, source_sessions)
    assert model.calls == 1 and len(cases) == 2
    assert model.wire_payload["schema"] == SCHEMA
    assert model.payload["evidence_context"] == packet
    wire_bytes = len(json.dumps(model.wire_payload, ensure_ascii=False, separators=(",", ":")).encode())
    assert wire_bytes + len(model.system.encode()) <= 786_432
    assert all(e["recorded_messages"][0]["content"] == system for e in packet["examples"])


@pytest.mark.asyncio
async def test_reserves_system_budget_before_lossless_sharing(source_sessions, monkeypatch):
    system = "Original System context. " * 15_000
    for source in source_sessions:
        source["turns"][0]["messages"].insert(0, {"role": "system", "content": system})
    preview = Model()
    await synthesize(preview, source_sessions)
    assert preview.wire_payload.get("schema") != SCHEMA
    raw_body_bytes = len(json.dumps(preview.payload, ensure_ascii=False).encode())
    maximum = raw_body_bytes + len(preview.system.encode()) // 2
    assert raw_body_bytes < maximum < raw_body_bytes + len(preview.system.encode())
    assert maximum <= 786_432
    monkeypatch.setattr(synthesis, "_MAX_SYNTHESIS_EVIDENCE_BYTES", maximum)
    model = Model()
    cases = await synthesize(model, source_sessions)
    assert model.calls == 1 and len(cases) == 2
    assert model.wire_payload["schema"] == SCHEMA
    assert model.payload == preview.payload
    wire_bytes = len(json.dumps(model.wire_payload, ensure_ascii=False, separators=(",", ":")).encode())
    assert wire_bytes + len(model.system.encode()) <= maximum


@pytest.mark.asyncio
async def test_evidence_that_cannot_fit_losslessly_fails_before_model_dispatch(source_sessions):
    for number, source in enumerate(source_sessions):
        query = str(number) * 500_000
        source["turns"][0]["prompt_text"] = query
        source["turns"][0]["messages"][0]["content"] = query
    model = Model()
    with pytest.raises(EvidenceContextError, match="system and evidence payload"):
        await synthesize(model, source_sessions)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_conflicting_record_declarations_are_not_silently_reinterpreted(source_sessions):
    source_sessions[0]["turns"][0]["request_context"]["generation_requests"][0]["input"]["reconstruction"][
        "constructed_import_batch"
    ] = False
    model = Model()
    with pytest.raises(EvidenceContextError, match="Conflicting"):
        await synthesize(model, source_sessions)
    assert model.calls == 0
