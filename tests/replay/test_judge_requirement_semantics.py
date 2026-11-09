"""Judge instruction and transport contracts, without model semantic claims."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from team_replay import judging
from teamEvolver import llm


@pytest.fixture
def sdk_capture(monkeypatch):
    state = SimpleNamespace(calls=[], result=None)
    monkeypatch.setattr(llm, "_current_tenant_id", lambda: "requirement-semantics-" + uuid.uuid4().hex)

    class FakeOpenAI:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def create(self, **body):
            state.calls.append(copy.deepcopy(body))
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(state.result, ensure_ascii=False)),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            )

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    yield state
    llm._reset_dispatchers_for_testing()


async def dispatch(state, checklist, raw, *, override=None):
    state.result = copy.deepcopy(raw)
    harness = {"model": "fake-model", "base_url": "http://provider.invalid/v1", "api_key": "fake-key"}
    if override is not None:
        harness["system_prompt"] = override
    interactions = [{"prompt": "仅回复一条简短台词", "response": "收到。"}]
    messages = [{"role": "assistant", "content": "收到。"}]
    frozen = copy.deepcopy(checklist)
    result = await asyncio.to_thread(
        judging.judge_checklist, harness=harness, checklist=checklist,
        interactions=interactions, messages=messages, artifacts=[],
    )
    assert checklist == frozen
    assert len(state.calls) == 1
    body = state.calls[0]
    schema = copy.deepcopy(judging.JUDGE_SCHEMA)
    schema["properties"]["items"].update(minItems=len(checklist), maxItems=len(checklist))
    schema["properties"]["items"]["items"]["properties"]["id"]["enum"] = [c["id"] for c in checklist]
    assert body["messages"] == [
        {"role": "system", "content": (override or judging.JUDGE_SYSTEM) + "\nJSON Schema: " + json.dumps(schema)},
        {"role": "user", "content": json.dumps({
            "checklist": frozen,
            "interactions": [{"user": item["prompt"], "agent": item["response"]} for item in interactions],
            "messages": messages, "artifacts": [],
        }, ensure_ascii=False)},
    ]
    assert body["response_format"] == {"type": "json_object"}
    return result, body


def judgment(checks):
    return {"items": checks, "all_satisfied": all(item["satisfied"] for item in checks),
            "positive_observations": []}


@pytest.mark.asyncio
async def test_default_judge_sends_requirement_truth_contract_without_rewriting_checklist(sdk_capture):
    checks = [
        {"id": "absence", "kind": "output", "text": "禁止泄露口令。", "polarity": "must_not"},
        {"id": "conditional", "kind": "output", "text": "若用户要求文件，须提供文件。"},
        {"id": "unknown", "kind": "trajectory", "text": "不得使用未授权工具。", "polarity": "must_not"},
    ]
    raw = judgment([
        {"id": "absence", "satisfied": True, "evidence": "完整公开消息没有泄露口令。"},
        {"id": "conditional", "satisfied": True, "evidence": "用户没有提出文件请求，分支未触发。"},
        {"id": "unknown", "satisfied": False, "evidence": "工具轨迹未提供，不能确认是否发生过调用。"},
    ])
    report, body = await dispatch(sdk_capture, checks, raw)
    system = body["messages"][0]["content"]
    # This verifies the dispatched convention, not that a real model follows it.
    assert "satisfied=true 表示完整满足该条原标准" in system
    assert "完整公开观测范围足以确认被禁止行为未发生" in system
    assert "条件明确未触发时" in system
    assert "不能把未知当作未发生" in system
    assert "satisfied 必须与 evidence 的完整含义一致" in system
    assert report["satisfied_ids"] == ["absence", "conditional"]
    assert report["unmet_ids"] == ["unknown"]
    assert report["attempt_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["公开消息包含被禁止的信息。", "公开消息未出现被禁止的信息。"])
async def test_valid_negative_is_not_reinterpreted_or_resampled_by_semantics_guidance(sdk_capture, evidence):
    checks = [{"id": "prohibition", "kind": "output", "text": "禁止泄露口令。", "polarity": "must_not"}]
    raw = judgment([{"id": "prohibition", "satisfied": False, "evidence": evidence}])
    report, _ = await dispatch(sdk_capture, checks, raw)
    assert report["judge"] == "model"
    assert report["items"][0]["satisfied"] is False
    assert report["items"][0]["evidence"] == evidence
    assert report["all_satisfied"] is False
    assert report["attempt_count"] == 1
    # Even a contradictory valid result is not silently flipped by word matching.


@pytest.mark.asyncio
async def test_custom_system_override_retains_existing_precedence_and_json_contract(sdk_capture):
    checks = [{"id": "unchanged", "kind": "output", "text": "保留原始要求。"}]
    raw = judgment([{"id": "unchanged", "satisfied": False, "evidence": "缺少要求的交付。"}])
    custom = "自定义裁判；只使用这条系统指令。"
    report, body = await dispatch(sdk_capture, checks, raw, override=custom)
    assert body["messages"][0]["content"].startswith(custom + "\nJSON Schema: ")
    assert judging.JUDGE_SYSTEM not in body["messages"][0]["content"]
    assert report["judge"] == "model" and report["all_satisfied"] is False
    assert report["attempt_count"] == 1
