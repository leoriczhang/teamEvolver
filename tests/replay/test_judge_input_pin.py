"""Request audit pins match real SDK-bound inputs, without asserting model quality."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from team_replay import judging
from teamEvolver import llm
from teamEvolver.llm_usage import collect_llm_usage

HARNESS = {
    "model": "fake-pin-model", "base_url": "http://provider.invalid/v1", "api_key": "private-fake-key",
    "max_tokens": 321,
}


@pytest.fixture
def pinned_sdk(monkeypatch):
    state = SimpleNamespace(calls=[], response=None)
    monkeypatch.setattr(llm, "_current_tenant_id", lambda: "judge-pin-" + uuid.uuid4().hex)

    class FakeOpenAI:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def create(self, **body):
            state.calls.append(copy.deepcopy(body))
            answer = state.response(body) if callable(state.response) else state.response
            if isinstance(answer, BaseException):
                raise answer
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=answer), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=5, total_tokens=7),
            )

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    yield state
    llm._reset_dispatchers_for_testing()


def response(checks, satisfied=False):
    return json.dumps({
        "items": [{"id": check["id"], "satisfied": satisfied, "evidence": "实际输出对应的短证据"}
                  for check in checks],
        "all_satisfied": satisfied, "positive_observations": [],
    }, ensure_ascii=False)


async def run_judge(checks, *, harness=None, interactions=None, messages=None):
    return await asyncio.to_thread(
        judging.judge_checklist, harness=copy.deepcopy(harness or HARNESS), checklist=checks,
        interactions=interactions or [], messages=messages or [], artifacts=[],
    )


def expected_pin_from_sdk(body, effective_system):
    # Independently parse the actual fake-SDK call; do not call the production pin helper.
    system_message = body["messages"][0]["content"]
    prefix = effective_system + "\nJSON Schema: "
    assert system_message.startswith(prefix)
    schema_text = system_message[len(prefix):]
    json.loads(schema_text)
    checklist = json.loads(body["messages"][1]["content"])["checklist"]
    return {
        "version": "checklist-judge-input-pin-v1", "scope": "intended_request",
        "effective_system_sha256": hashlib.sha256(effective_system.encode()).hexdigest(),
        "json_schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest(),
        "system_message_sha256": hashlib.sha256(system_message.encode()).hexdigest(),
        "checklist_sha256": hashlib.sha256(json.dumps(checklist, ensure_ascii=False).encode()).hexdigest(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [None, "私有自定义裁判，不附默认规则。"])
async def test_pin_matches_effective_system_dynamic_schema_and_original_sdk_checklist(pinned_sdk, custom):
    checks = [
        {"id": "甲", "kind": "output", "text": "不复述历史任一内容，尤其是两个例子。", "polarity": "must_not"},
        {"id": "乙", "kind": "trajectory", "text": "保留独立子句与条件。"},
    ]
    original = copy.deepcopy(checks)
    pinned_sdk.response = response(checks)
    harness = {**HARNESS, **({"system_prompt": custom} if custom is not None else {})}
    report = await run_judge(checks, harness=harness)
    assert len(pinned_sdk.calls) == report["attempt_count"] == 1
    body = pinned_sdk.calls[0]
    effective = custom or judging.JUDGE_SYSTEM
    expected = expected_pin_from_sdk(body, effective)
    assert report["format_diagnostic"]["request_pin"] == expected
    assert report["format_attempts"][0]["format_diagnostic"]["request_pin"] == expected
    assert checks == original == json.loads(body["messages"][1]["content"])["checklist"]
    schema = json.loads(body["messages"][0]["content"].split("\nJSON Schema: ", 1)[1])
    assert schema["properties"]["items"]["minItems"] == schema["properties"]["items"]["maxItems"] == 2
    assert schema["properties"]["items"]["items"]["properties"]["id"]["enum"] == ["甲", "乙"]
    assert body == {
        "model": HARNESS["model"], "messages": [
            {"role": "system", "content": effective + "\nJSON Schema: " + json.dumps(schema)},
            {"role": "user", "content": json.dumps({
                "checklist": original, "interactions": [], "messages": [], "artifacts": [],
            }, ensure_ascii=False)},
        ], "temperature": 0.0, "max_tokens": 321, "response_format": {"type": "json_object"},
    }
    assert report["all_satisfied"] is False
    for private in [HARNESS["api_key"], HARNESS["base_url"], "私有自定义裁判", checks[0]["text"]]:
        assert private not in json.dumps(expected, ensure_ascii=False)
    if custom:
        assert judging.JUDGE_SYSTEM not in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_changed_check_text_and_ids_change_only_corresponding_pins(pinned_sdk):
    variants = [
        [{"id": "one", "kind": "output", "text": "原始中文要求。"}],
        [{"id": "one", "kind": "output", "text": "另一条中文要求。"}],
        [{"id": "two", "kind": "output", "text": "另一条中文要求。"}],
    ]
    pins = []
    for checks in variants:
        pinned_sdk.response = response(checks)
        report = await run_judge(checks)
        pin = report["format_diagnostic"]["request_pin"]
        assert pin == expected_pin_from_sdk(pinned_sdk.calls[-1], judging.JUDGE_SYSTEM)
        pins.append(pin)
    assert len({pin["effective_system_sha256"] for pin in pins}) == 1
    assert pins[0]["json_schema_sha256"] == pins[1]["json_schema_sha256"]
    assert pins[1]["json_schema_sha256"] != pins[2]["json_schema_sha256"]
    assert len({pin["checklist_sha256"] for pin in pins}) == 3


@pytest.mark.asyncio
async def test_format_retry_keeps_identical_pin_and_request_then_stops_on_valid_negative(pinned_sdk):
    checks = [{"id": "one", "kind": "output", "text": "完整原标准。"}]
    answers = iter(["{invalid", response(checks)])
    pinned_sdk.response = lambda _: next(answers)
    report = await run_judge(checks)
    assert len(pinned_sdk.calls) == report["attempt_count"] == 2
    assert pinned_sdk.calls[0] == pinned_sdk.calls[1]
    expected = expected_pin_from_sdk(pinned_sdk.calls[0], judging.JUDGE_SYSTEM)
    assert [entry["format_diagnostic"]["request_pin"] for entry in report["format_attempts"]] == [expected, expected]
    assert report["format_attempts"][0]["format_diagnostic"]["schema_error"] == "invalid_json"
    assert report["judge"] == "model" and report["all_satisfied"] is False


@pytest.mark.asyncio
async def test_provider_failure_pin_never_claims_successful_dispatch_or_serializes_private_error(pinned_sdk):
    checks = [{"id": "one", "text": "私有原标准"}]
    pinned_sdk.response = RuntimeError("private-provider-error")
    report = await run_judge(checks)
    assert report["judge"] == "unavailable" and report["attempt_count"] == len(pinned_sdk.calls) == 1
    diagnostic = report["format_diagnostic"]
    assert diagnostic == {
        "schema_error": "judge_completion_unavailable",
        "request_pin": expected_pin_from_sdk(pinned_sdk.calls[0], judging.JUDGE_SYSTEM),
    }
    assert diagnostic["request_pin"]["scope"] == "intended_request"
    assert "private-provider-error" not in json.dumps(report)


@pytest.mark.asyncio
async def test_budget_failure_before_sdk_dispatch_retains_only_intended_pin(pinned_sdk):
    checks = [{"id": "one", "kind": "output", "text": "原始要求。"}]
    pinned_sdk.response = "{}"
    with collect_llm_usage("pre-exhausted-judge-pin", budget_tokens=7) as meter:
        await asyncio.to_thread(
            judging.complete_json, harness=HARNESS, system="consume fake budget", schema={"type": "object"},
            payload={}, temperature=0.0,
        )
        report = await run_judge(checks)
    assert len(pinned_sdk.calls) == meter.snapshot()["attempt_count"] == 1
    assert report["judge"] == "unavailable" and report["attempt_count"] == 1
    pin = report["format_diagnostic"]["request_pin"]
    assert pin["scope"] == "intended_request"
    assert pin["effective_system_sha256"] == hashlib.sha256(judging.JUDGE_SYSTEM.encode()).hexdigest()
    assert pin["checklist_sha256"] == hashlib.sha256(json.dumps(checks, ensure_ascii=False).encode()).hexdigest()
    assert "dispatched" not in pin and "response_sha256" not in report["format_diagnostic"]


@pytest.mark.asyncio
async def test_invalid_checklist_ids_have_no_dispatch_or_invented_dynamic_schema_pin(pinned_sdk):
    report = await run_judge([{"id": "same", "text": "one"}, {"id": "same", "text": "two"}])
    assert pinned_sdk.calls == []
    assert report["format_diagnostic"] == {"schema_error": "invalid_checklist_ids"}


@pytest.mark.asyncio
@pytest.mark.parametrize("satisfied", [False, True])
async def test_scope_guidance_is_generic_and_does_not_auto_rescore_valid_judgments(pinned_sdk, satisfied):
    checks = [{"id": "scope", "kind": "output", "text": "不得复述任一历史消息，尤其是后两例。"}]
    pinned_sdk.response = response(checks, satisfied=satisfied)
    interactions = [{"prompt": "C: 先前的消息\nC: 后面的例子", "response": "先前的消息"}]
    report = await run_judge(checks, interactions=interactions)
    system = pinned_sdk.calls[0]["messages"][0]["content"]
    assert "完整适用范围和全部独立子句" in system
    assert "通常不是穷尽清单" in system
    assert "完整相关历史" in system
    assert "简短 evidence 不等于只核举例" in system
    assert "不输出内部推理过程" in system
    assert report["all_satisfied"] is satisfied
    assert report["items"][0]["satisfied"] is satisfied
    assert report["attempt_count"] == len(pinned_sdk.calls) == 1
    # This test protects transport/finality only; the fake result is not semantic validation.
