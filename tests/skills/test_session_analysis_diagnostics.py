"""Mandatory analysis exposes only fixed structural failure codes, never source/provider text."""

from __future__ import annotations

import copy
import json
from itertools import combinations

import pytest

from session_ingestion.diagnostics import (
    ANALYSIS_SECTIONS,
    ingestion_error_code,
    missing_analysis_diagnostic,
    safe_analysis_diagnostic,
)
from team_skills.evolution.stages import analyze

PRIVATE = "PRIVATE-source-and-provider-key"


class FakeLLM:
    model = "offline-model"

    def __init__(self, reply="", error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def chat(self, messages, **options):
        self.calls.append((messages, options))
        if self.error:
            raise self.error
        return self.reply


def session():
    return {"session_id": PRIVATE, "turns": [{"prompt_text": PRIVATE, "response_text": "Observed result"}]}


def response(missing=(), *, invalid=False):
    values = {
        "classification": json.dumps({"decision": "task_only", "confidence": 0.6, "reason": "任务尚无复用证据"}),
        "summary": "用户提出任务，Agent返回实际结果。证据仅支持当前任务，不提升为复用知识。",
        "judge": json.dumps(
            {
                "task_completion": 0.7,
                "response_quality": 0.6,
                "efficiency": 0.5,
                "tool_usage": 0.4,
                "evolution_evidence": "none",
                "skill_experiences": [],
            }
        ),
    }
    if invalid:
        for name in missing:
            values[name] = "   " if name == "summary" else PRIVATE
    return "\n".join(f"<{name}>{value}</{name}>" for name, value in values.items() if invalid or name not in missing)


@pytest.fixture(autouse=True)
def fixed_analysis_options(monkeypatch):
    monkeypatch.setattr(analyze, "_analyze_call_options", lambda: {"max_tokens": 1234, "temperature": 0.125})
    monkeypatch.setattr(analyze, "_effective_analyze_system", lambda: "Original fixed analysis policy")


@pytest.mark.asyncio
async def test_no_llm_is_explicit_and_does_not_modify_input_or_call_a_substitute():
    value = session()
    before = copy.deepcopy(value)
    with pytest.raises(analyze.SessionAnalysisError) as error:
        await analyze.analyze_session(None, value)
    assert error.value.diagnostic_code == "session_analysis.llm_unconfigured"
    assert PRIVATE not in str(error.value) and value == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "TimeoutError",
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "AuthenticationError",
        "LLMOverloadedError",
        "RuntimeError",
        PRIVATE,
    ],
)
async def test_provider_type_is_closed_and_original_error_text_is_not_propagated(kind):
    llm = FakeLLM(error=type(kind, (RuntimeError,), {})(PRIVATE))
    value = session()
    with pytest.raises(analyze.SessionAnalysisError) as error:
        await analyze.analyze_session(llm, value)
    expected = kind if kind != PRIVATE else "OtherError"
    assert error.value.diagnostic_code == "session_analysis.provider." + expected
    assert ingestion_error_code(error.value) == error.value.diagnostic_code
    assert PRIVATE not in str(error.value) and PRIVATE not in error.value.diagnostic_code
    assert error.value.__cause__ is None and error.value.__suppress_context__ is True
    assert len(llm.calls) == 1
    assert not {"_summary", "_judge_scores", "value_judge"} & value.keys()


MISSING = [parts for size in range(1, 4) for parts in combinations(ANALYSIS_SECTIONS, size)]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", MISSING)
@pytest.mark.parametrize("invalid", [False, True])
async def test_all_missing_or_invalid_section_combinations_remain_failed_without_partial_outputs(missing, invalid):
    value = session()
    llm = FakeLLM(response(missing, invalid=invalid))
    with pytest.raises(analyze.SessionAnalysisError) as error:
        await analyze.analyze_session(llm, value)
    assert error.value.diagnostic_code == "session_analysis.missing." + "+".join(missing)
    assert PRIVATE not in str(error.value) and PRIVATE not in error.value.diagnostic_code
    assert len(llm.calls) == 1 and llm.calls[0][1]["max_tokens"] == 1234
    assert llm.calls[0][1]["temperature"] == 0.125
    assert not {"_summary", "_judge_scores", "value_judge"} & value.keys()


@pytest.mark.asyncio
async def test_complete_response_preserves_original_classification_weighted_scores_and_call_options():
    value, llm = session(), FakeLLM(response())
    result = await analyze.analyze_session(llm, value)
    assert result["decision"] == "task_only" and result["confidence"] == 0.6
    assert value["value_judge"] == result and value["_judge_scores"]["source"] == "llm"
    assert value["_judge_scores"]["overall_score"] == 0.63
    assert len(llm.calls) == 1 and llm.calls[0][1]["temperature"] == 0.125


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [],
        17,
        PRIVATE,
        "session_analysis.missing." + PRIVATE,
        "session_analysis.provider." + PRIVATE,
        "x" * 10000,
    ],
)
def test_untrusted_diagnostic_attribute_cannot_escape_closed_contract(value):
    error = RuntimeError(PRIVATE)
    error.diagnostic_code = value
    assert safe_analysis_diagnostic(value) == ""
    assert ingestion_error_code(error) == "RuntimeError"
    assert analyze.SessionAnalysisError("Legacy message", diagnostic_code=value).diagnostic_code == ""


def test_throwing_attribute_keeps_legacy_type_and_unknown_missing_labels_are_not_serialized():
    class BrokenAttribute(RuntimeError):
        @property
        def diagnostic_code(self):
            raise ValueError(PRIVATE)

    assert ingestion_error_code(BrokenAttribute(PRIVATE)) == "BrokenAttribute"
    assert missing_analysis_diagnostic([PRIVATE]) == ""
    assert (
        missing_analysis_diagnostic([PRIVATE, "judge", "classification"])
        == "session_analysis.missing.classification+judge"
    )
