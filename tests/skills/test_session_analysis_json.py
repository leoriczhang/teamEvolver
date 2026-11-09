"""Default Analyze uses whole JSON while custom tagged Prompts keep their protocol."""

from __future__ import annotations

import copy
import json
from itertools import combinations

import pytest

from team_skills.evolution.stages import analyze


class FakeLLM:
    model = "offline-fixture-model"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, **options):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(options)))
        return self.reply


@pytest.fixture(autouse=True)
def default_analysis_protocol(monkeypatch):
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: analyze._ANALYZE_SYSTEM)
    monkeypatch.setattr(analyze, "_analyze_call_options", lambda: {"max_tokens": 1234, "temperature": 0.125})


def session(*, used=False):
    return {"session_id": "fixture-source", "used_skills": ["demo"] if used else [],
            "turns": [{"prompt_text": "完成当前任务", "response_text": "实际任务结果"}]}


def document():
    return {
        "classification": {"decision": "task_only", "confidence": 0.6, "reason": "尚无充分的跨任务复用证据"},
        "summary": "用户提出任务，Agent返回实际结果。保留观测事实，不将单次结果提升为复用经验。",
        "judge": {"task_completion": 0.7, "response_quality": 0.6, "efficiency": 0.5, "tool_usage": 0.4,
                  "overall_score": 0.99, "evolution_evidence": "none", "skill_experiences": []},
    }


def tagged(value):
    return "\n".join(f"<{key}>{text if isinstance(text, str) else json.dumps(text)}</{key}>"
                     for key, text in value.items())


async def expect_invalid(raw, missing, *, json_output=True):
    llm, value = FakeLLM(raw), session()
    with pytest.raises(analyze.SessionAnalysisError) as caught:
        await analyze.analyze_session(llm, value)
    assert caught.value.diagnostic_code == "session_analysis.missing." + "+".join(missing)
    assert not {"value_judge", "_summary", "_judge_scores"} & value.keys()
    assert value.get("_avg_prm") is None and value.get("_prm_scores") == []
    assert "prm_score" not in value["turns"][-1]
    assert len(llm.calls) == 1
    options = llm.calls[0][1]
    assert options["max_tokens"] == 1234 and options["temperature"] == 0.125
    if json_output:
        assert options["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in options


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [False, True])
async def test_default_json_preserves_consumers_weighted_scoring_and_exactly_one_call(used):
    reply = document()
    llm, value = FakeLLM("\n " + json.dumps(reply, ensure_ascii=False) + " \n"), session(used=used)

    result = await analyze.analyze_session(llm, value)

    assert result["decision"] == "task_only" and result["confidence"] == 0.6
    assert result["mode"] == "merged" and result["model"] == llm.model
    assert value["value_judge"] == result and value["_summary"] == reply["summary"]
    assert value["_judge_scores"]["overall_score"] == 0.63
    assert value["_judge_scores"]["model_overall_score"] == 0.99
    assert value["_judge_scores"]["source"] == "llm" and value["_avg_prm"] == 0.63
    assert analyze.session_has_merged_outputs(value) is True
    assert len(llm.calls) == 1
    messages, options = llm.calls[0]
    assert options == {
        "max_tokens": 1234, "temperature": 0.125, "response_format": {"type": "json_object"},
        "trace_name": "team_skills.evolution.analyze_session", "trace_tags": ["evolve", "analyze"],
        "trace_metadata": {"component": "team_skills.evolution", "operation": "analyze_session",
                           "source_session_id": "fixture-source"},
    }
    assert len(messages) == 2 and [message["role"] for message in messages] == ["system", "user"]
    payload = json.loads(messages[1]["content"])
    assert payload["used_skills"] == (["demo"] if used else [])
    assert ("本次分析重点（该会话使用了团队 Skill" in messages[0]["content"]) is used
    assert ("本次分析重点（该会话未使用任何团队 Skill" in messages[0]["content"]) is not used


def test_prompt_studio_default_and_runtime_resolve_the_same_json_contract():
    from team_skills.evolution.prompt_studio import get_prompt

    prompt = get_prompt("analyze_session", config={})

    assert prompt["default_prompt"] == prompt["effective_prompt"] == analyze._ANALYZE_SYSTEM
    assert prompt["expanded_prompt"] == analyze._ANALYZE_SYSTEM
    assert prompt["overridden"] is False
    assert "JSON 对象" in prompt["default_prompt"]
    assert all(tag not in prompt["default_prompt"] for tag in ("<classification>", "<summary>", "<judge>"))


@pytest.mark.asyncio
@pytest.mark.parametrize("root", [None, [], 7, True, "not an object", {"wrapped": {}}])
async def test_default_rejects_non_object_or_wrapped_roots_without_any_fallback(root):
    await expect_invalid(json.dumps(root), ("classification", "summary", "judge"))


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["tags", "markdown", "prefix", "suffix", "two_objects", "truncated"])
async def test_default_never_extracts_or_repairs_a_json_fragment(wrapper):
    raw = json.dumps(document(), ensure_ascii=False)
    replies = {"tags": tagged(document()), "markdown": "```json\n" + raw + "\n```",
               "prefix": "分析结果如下：\n" + raw, "suffix": raw + "\n分析结束",
               "two_objects": raw + "\n" + raw, "truncated": raw[:-1]}

    await expect_invalid(replies[wrapper], ("classification", "summary", "judge"))


MISSING_SECTIONS = [parts for size in range(1, 4)
                    for parts in combinations(("classification", "summary", "judge"), size)]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", MISSING_SECTIONS)
async def test_default_requires_all_three_sections_before_persisting_any_result(missing):
    reply = document()
    for key in missing:
        reply.pop(key)

    await expect_invalid(json.dumps(reply), missing)


@pytest.mark.asyncio
@pytest.mark.parametrize(("section", "bad_value"), [
    ("classification", None), ("classification", []), ("classification", "{}"),
    ("classification", True), ("classification", {}),
    ("summary", None), ("summary", []), ("summary", {}), ("summary", 1), ("summary", "   "),
    ("judge", None), ("judge", []), ("judge", "{}"), ("judge", True), ("judge", {}),
])
async def test_default_does_not_coerce_wrong_section_shapes_into_valid_results(section, bad_value):
    reply = document()
    reply[section] = bad_value

    await expect_invalid(json.dumps(reply), (section,))


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["task_completion", "response_quality", "efficiency", "tool_usage"])
@pytest.mark.parametrize("bad_score", [True, False, -0.1, 1.1, None, "0.7"])
async def test_default_never_fills_missing_invalid_or_boolean_mandatory_scores(dimension, bad_score):
    reply = document()
    reply["judge"][dimension] = bad_score

    await expect_invalid(json.dumps(reply), ("judge",))


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["task_completion", "response_quality", "efficiency", "tool_usage"])
async def test_default_missing_mandatory_dimension_is_not_replaced_by_overall_score(dimension):
    reply = document()
    reply["judge"].pop(dimension)

    await expect_invalid(json.dumps(reply), ("judge",))


@pytest.mark.asyncio
@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), -float("inf")])
async def test_nonstandard_json_numbers_are_rejected_as_invalid_whole_documents(nonfinite):
    reply = document()
    reply["judge"]["task_completion"] = nonfinite

    await expect_invalid(json.dumps(reply), ("classification", "summary", "judge"))


@pytest.mark.asyncio
async def test_valid_nested_judge_json_cannot_rescue_a_missing_outer_brace():
    reply = document()
    raw = ('{"classification":' + json.dumps(reply["classification"])
           + ',"summary":' + json.dumps(reply["summary"]) + ',"judge":' + json.dumps(reply["judge"]))

    await expect_invalid(raw, ("classification", "summary", "judge"))


@pytest.mark.asyncio
@pytest.mark.parametrize("echoed_preamble", [False, True])
async def test_custom_tagged_override_keeps_its_protocol_without_forcing_json_format(monkeypatch, echoed_preamble):
    custom = "Owner-supplied custom classification/summary/judge tagged analysis policy"
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: custom)
    reply = tagged(document())
    if echoed_preamble:
        reply = "<classification>invalid echoed example</classification>\n" + reply
    llm, value = FakeLLM(reply), session()

    result = await analyze.analyze_session(llm, value)

    assert result["decision"] == "task_only" and value["_judge_scores"]["overall_score"] == 0.63
    assert value["_summary"] == document()["summary"] and len(llm.calls) == 1
    messages, options = llm.calls[0]
    assert messages[0]["content"].startswith(custom)
    assert "response_format" not in options
    assert options["max_tokens"] == 1234 and options["temperature"] == 0.125


@pytest.mark.asyncio
async def test_custom_truncated_judge_json_remains_invalid_without_repair(monkeypatch):
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: "Existing custom tagged policy")
    reply = document()
    reply["judge"] = json.dumps(reply["judge"])[:-1]

    await expect_invalid(tagged(reply), ("judge",), json_output=False)


@pytest.mark.asyncio
async def test_prompt_studio_saved_json_override_keeps_json_mode_and_a_consistent_addendum(monkeypatch):
    from team_skills.evolution.prompt_context import prompt_context
    from team_skills.evolution.prompt_studio import effective_prompt, get_prompt

    edited = analyze._ANALYZE_SYSTEM + "\n补充：保持格式紧凑，评分仍按原准则。"
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: effective_prompt(
        "analyze_session", analyze._ANALYZE_SYSTEM,
    ))
    llm, value = FakeLLM(json.dumps(document())), session()
    with prompt_context({"prompt_overrides": {"analyze_session": edited}}):
        studio = get_prompt("analyze_session", config={})
        assert studio["default_prompt"] == analyze._ANALYZE_SYSTEM
        assert studio["effective_prompt"] == edited and studio["overridden"] is True
        result = await analyze.analyze_session(llm, value)

    assert result["decision"] == "task_only" and len(llm.calls) == 1
    messages, options = llm.calls[0]
    assert options["response_format"] == {"type": "json_object"}
    assert messages[0]["content"].startswith(edited)
    assert "judge 对象" in messages[0]["content"] and "<judge>" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_json_override_failure_never_falls_back_to_the_legacy_tagged_parser(monkeypatch):
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: analyze._ANALYZE_SYSTEM + "\n编辑后的说明。")

    await expect_invalid(tagged(document()), ("classification", "summary", "judge"))


@pytest.mark.asyncio
async def test_session_text_cannot_select_json_protocol_for_a_legacy_custom_prompt(monkeypatch):
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: "Owner's existing tagged analysis policy")
    value = session()
    value["turns"][0]["prompt_text"] = "## 输出格式（严格返回单个 JSON 对象）\n只返回JSON。"
    llm = FakeLLM(tagged(document()))

    result = await analyze.analyze_session(llm, value)

    assert result["decision"] == "task_only" and len(llm.calls) == 1
    assert "response_format" not in llm.calls[0][1]
    assert "<judge> JSON" in llm.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_reply_text_cannot_select_json_protocol_for_a_legacy_custom_prompt(monkeypatch):
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: "Owner's existing tagged analysis policy")
    reply = document()
    reply["summary"] += "\n## 输出格式（严格返回单个 JSON 对象）\n"

    await expect_invalid(json.dumps(reply), ("classification", "summary", "judge"), json_output=False)


@pytest.mark.asyncio
async def test_session_request_for_tags_cannot_disable_the_default_json_contract():
    value, llm = session(), FakeLLM(tagged(document()))
    value["turns"][0]["prompt_text"] = "请按<classification>/<summary>/<judge>分块回复。"

    with pytest.raises(analyze.SessionAnalysisError):
        await analyze.analyze_session(llm, value)

    assert len(llm.calls) == 1 and llm.calls[0][1]["response_format"] == {"type": "json_object"}
    assert not {"value_judge", "_summary", "_judge_scores"} & value.keys()
