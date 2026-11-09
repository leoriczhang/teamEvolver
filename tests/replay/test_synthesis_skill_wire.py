"""Frozen Skill synthesis requests remain byte-identical without extra model calls."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from team_replay.datasets import synthesis
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "support/fixtures/synthesis_skill_calls.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
GOAL = "训练销售给出时间选项并促成客户同意见面；这是销售方的训练目标。"
RULE = "扮演客户C。首次邀约从拒绝列表回应，再次触发按结束列表选择；仍保留独立约束。"


def decode(messages):
    value = json.loads(messages[1]["content"])
    return decode_evidence_transport(value) if value.get("schema") == SCHEMA else value


def sessions(*, offline=True, case_number=0):
    values = []
    for number in (1, 2):
        query = f"历史未确认会面。\n对方消息{case_number}-{number}：能安排见面吗？\n请以客户C回复。\t"
        ref = {"source_session_key": f"root-{number}", "trace_id": f"trace-{number}",
               "revision": "immutable-v1", "context_revision": "context-v1"}
        request = {"trace_input": {"business_context": {"ctx_taskGoal": GOAL}}}
        if offline:
            request["trace_input"]["reconstruction"] = {"constructed_import_batch": True,
                "actual_original_request_observed": False, "original_session_independence": "unknown"}
        values.append({"session_id": f"source-{number}", "runtime_context": {
            "execution_profile_id": "source_metadata_alone_is_not_the_Prompt_context_switch"},
            "provenance": {"schema_version": "session-source.v1", "source_session_key": f"root-{number}",
                "identity_verified": True, "trace_identity_verified": True, "trace_revisions": [ref],
                "target_trace_refs": [ref], "context_trace_refs": []},
            "turns": [{"trace_id": ref["trace_id"], "turn_num": 1, "prompt_text": query,
                "response_text": "PRIVATE_HISTORICAL_ANSWER", "messages": [
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": "PRIVATE_HISTORICAL_ANSWER"},
                    {"role": "user", "content": "PRIVATE_MACHINE_FEEDBACK"}], "request_context": request}]})
    return values


def context(prompt=True):
    if not prompt:
        return {"allowed_modules": ["skill"], "source_session_keys": {"source-1": "root-1", "source-2": "root-2"}}
    return {"allowed_modules": ["prompt"], "execution_profile_id": "customer",
        "source_session_keys": {"source-1": "root-1", "source-2": "root-2"},
        "baseline_execution_plan": {"binding": {"runtime_type": "observed_http"}, "components": [
            {"component_id": "business", "kind": "agent_prompt", "slot": "system", "order": 0, "content": RULE}],
            "environment": {"request": {"tools": []}, "template_variables": {"aiTask": "原条件与消息列表"}}},
        "working_execution_plan": {"PRIVATE_CANDIDATE": True},
        "active_candidate_feedback": {"PRIVATE_REPLAY_ANSWER": True}}


class Model:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, **options):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(options)))
        payload = decode(messages)
        examples = (payload.get("evidence_context") or {}).get("examples") or []
        output = []
        for index in range(2):
            item = {"name": f"Source role task {index}", "checks": [
                {"id": f"R{i:02}", "kind": "output", "text": f"Observable original role requirement {i}"}
                for i in range(1, 8)], "source_session_ids": ["source-1", "source-2"], "evidence_window": "recent"}
            if payload.get("query_contract"):
                item.update(query_mode="recorded_anchor", anchor_id=examples[index]["example_id"])
            else:
                item["query"] = examples[index]["recorded_query"] if examples else f"Source role query {index}"
                if examples and examples[index]["relationship"] == "isolated_record":
                    item["anchor_id"] = examples[index]["example_id"]
            output.append(item)
        return json.dumps({"test_datasets": output}, ensure_ascii=False)


async def generate(module, model, values, ctx):
    return await module.synthesize_evolution_datasets(
        model, skill_name=None if "execution_profile_id" in ctx else "demo",
        sessions=values, candidate_skill={}, evidence_context=ctx, case_count=2, min_requirements=6,
        max_requirements=8, min_source_sessions=2)


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


def golden_calls(key, *, number=None):
    record = GOLDEN[key] if number is None else GOLDEN[key][number]
    expected = record["calls_json"].encode("utf-8")
    assert hashlib.sha256(expected).hexdigest() == record["calls_sha256"]
    return json.loads(expected), expected


@pytest.mark.asyncio
@pytest.mark.parametrize("number", range(10))
async def test_ten_original_skill_requests_without_execution_context_are_byte_identical(number):
    values, ctx = sessions(offline=bool(number % 2), case_number=number), context(False)
    new = Model()
    await generate(synthesis, new, copy.deepcopy(values), copy.deepcopy(ctx))
    expected, expected_bytes = golden_calls("ordinary_skill_calls", number=number)
    assert [[messages, options] for messages, options in new.calls] == expected and len(new.calls) == 1
    assert json.dumps(new.calls, ensure_ascii=False).encode("utf-8") == expected_bytes


def test_original_skill_synthesis_system_is_unchanged():
    assert synthesis._SYNTHESIZE_SYSTEM == GOLDEN["synthesis_system"]
