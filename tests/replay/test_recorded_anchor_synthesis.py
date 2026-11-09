"""Complete source-anchor materialization, without a real model or Agent."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_replay.datasets import synthesis
from team_replay.datasets.schema import normalize_case
from team_skills.evolution.replay_case_synthesis import LLMCrossSessionCaseSynthesizer
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport


def sources():
    output = []
    for index in (1, 2):
        query = f" \r\n  原始问题{index}，e\u0301和é不是相同字节。\r\n当前消息：周二能见面吗？\t\r\n"
        ref = {"source_session_key": f"root-{index}", "trace_id": f"trace-{index}",
               "revision": "v1", "context_revision": "c1"}
        output.append({"session_id": f"source-{index}", "provenance": {
            "schema_version": "session-source.v1", "source_session_key": f"root-{index}",
            "identity_verified": True, "trace_identity_verified": True,
            "target_trace_refs": [ref], "trace_revisions": [ref], "context_trace_refs": [],
        }, "turns": [{"trace_id": f"trace-{index}", "turn_num": 1, "prompt_text": query,
                      "response_text": "PRIVATE_OLD_SOURCE_ANSWER", "messages": [
                          {"role": "user", "content": query},
                          {"role": "assistant", "content": "PRIVATE_OLD_SOURCE_ANSWER"},
                          {"role": "user", "content": "PRIVATE_SOURCE_GRADE"},
                      ], "request_context": {"trace_input": {"reconstruction": {
                          "constructed_import_batch": True, "actual_original_request_observed": False,
                          "original_session_independence": "unknown",
                      }}}}]})
    return output


def context():
    return {"allowed_modules": ["prompt"], "execution_profile_id": "customer",
            "baseline_execution_plan": {"binding": {"runtime_type": "observed_http"},
                "components": [{"component_id": "main", "kind": "agent_prompt", "slot": "system", "order": 0,
                                "content": "Reply as the customer. Do not repeat previous customer wording."}],
                "environment": {"request": {"tools": []}}},
            "working_execution_plan": {"PRIVATE_CANDIDATE": "Do not send to the Case reviewer"},
            "active_candidate_feedback": {"answer": "PRIVATE_REPLAY_ANSWER"}}


def body(messages):
    value = json.loads(messages[1]["content"])
    return decode_evidence_transport(value) if value.get("schema") == SCHEMA else value


class Model:
    def __init__(self, check=None):
        self.check = check
        self.generations, self.responses = [], []

    async def chat(self, messages, **options):
        payload = body(messages)
        self.generations.append(copy.deepcopy(payload))
        assert payload["query_contract"]["query_mode"] == "recorded_anchor"
        anchors = payload["evidence_context"]["examples"]
        cases = []
        for index, anchor in enumerate(anchors):
            checks = [{"id": f"R{i:02}", "kind": "output", "text": f"Original requirement {i}"}
                      for i in range(1, 8)]
            if self.check is not None:
                checks[0] = copy.deepcopy(self.check)
            cases.append({"query_mode": "recorded_anchor", "anchor_id": anchor["example_id"],
                          "name": f"Source task {index}", "checks": checks,
                          "source_session_ids": ["source-1", "source-2"], "evidence_window": "recent"})
        raw = "\n" + json.dumps({"test_datasets": cases}, ensure_ascii=False, indent=2) + "\n"
        self.responses.append(raw)
        return raw


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


@pytest.mark.asyncio
async def test_single_synthesis_preserves_exact_query_id_hash_and_original_wire_receipt():
    evidence, model = sources(), Model()
    before = copy.deepcopy(evidence)
    cases = await LLMCrossSessionCaseSynthesizer(model)(skill_name=None, sessions=evidence, candidate_skill={},
                                                       evidence_context=context())
    assert evidence == before and len(cases) == 2
    assert len(model.generations) == 1
    for index, case in enumerate(cases):
        query = evidence[index]["turns"][0]["prompt_text"]
        expected_hash = hashlib.sha256(query.encode()).hexdigest()
        assert case["query"].encode() == query.encode() == normalize_case(case)["query"].encode()
        assert case["case_id"] == synthesis._dataset_id(
            skill_name=None, query=query, anchor_id=case["provenance"]["evidence_anchor"]["anchor_id"],
            source_session_ids=["source-1", "source-2"],
        )
        binding = case["provenance"]["query_materialization"]
        assert binding["recorded_query_sha256"] == expected_hash
        receipt = case["provenance"]["query_materialization_receipt"]
        assert receipt["raw_generation_response"] == model.responses[0]
        assert receipt["raw_generation_response_sha256"] == hashlib.sha256(model.responses[0].encode()).hexdigest()
        assert "query" not in receipt["raw_case"] and receipt["canonical_query_sha256"] == expected_hash
        assert case["checks"] == receipt["raw_case"]["checks"]
        assert "observability_review" not in case["provenance"]


@pytest.mark.asyncio
async def test_schema_valid_check_is_not_rejudged_or_repaired_by_an_additional_model():
    check = {"id": "R01", "kind": "trajectory", "text": "内部逐条比对后才回复，不公开自检。"}
    model = Model(check)
    cases = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name=None, sessions=sources(), candidate_skill={}, evidence_context=context())
    assert len(model.generations) == 1
    assert all(case["checks"][0] == check for case in cases)
    assert all("observability_review" not in case["provenance"] for case in cases)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_bound,offline", [(True, True), (True, False), (False, True), (False, False)])
async def test_background_clause_is_limited_to_new_recorded_anchor_prompts(prompt_bound, offline):
    evidence = sources()
    if not offline:
        for session in evidence:
            session["turns"][0]["request_context"]["trace_input"].pop("reconstruction")
    new_mode = prompt_bound and offline
    clause = "背景事实用于保持角色和情境一致，不自动成为每轮必须显式复述的输出项"
    calls = []

    class Inspector:
        async def chat(self, messages, **options):
            payload = body(messages)
            calls.append(options["trace_name"])
            assert (clause in messages[0]["content"]) is new_mode
            anchors = (payload.get("evidence_context") or {}).get("examples") or []
            cases = []
            for index in range(2):
                case = {"name": f"Background fixture {index}", "source_session_ids": ["source-1", "source-2"],
                        "checks": [{"id": f"R{i:02}", "kind": "output", "text": f"Original requirement {i}"}
                                   for i in range(1, 8)]}
                if new_mode:
                    case.update(query_mode="recorded_anchor", anchor_id=anchors[index]["example_id"])
                else:
                    case["query"] = anchors[index]["recorded_query"] if anchors else f"Recorded task {index}"
                    if offline:
                        case["anchor_id"] = anchors[index]["example_id"]
                cases.append(case)
            return json.dumps({"test_datasets": cases}, ensure_ascii=False)

    await LLMCrossSessionCaseSynthesizer(Inspector())(
        skill_name=None if prompt_bound else "demo", sessions=evidence, candidate_skill={},
        evidence_context=context() if prompt_bound else {"allowed_modules": ["skill"]},
    )
    assert len(calls) == 1
