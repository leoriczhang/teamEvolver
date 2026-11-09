"""Tests for mandatory trace-level analysis."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_skills.evolution.stages import trace_analyze as ta
from teamEvolver.llm import LLMOverloadedError


class _FakeClient:
    """Minimal AsyncLLMClient stand-in returning a canned reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.reply


class _AllGoodClient:
    """Return one explicit non-badcase decision for every supplied trace."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "traces": [
                    {
                        "trace_id": trace["trace_id"],
                        "turn_num": trace["turn_num"],
                        "is_badcase": False,
                        "problem_type": "other",
                        "problem_description": "",
                    }
                    for trace in payload["traces"]
                ]
            }
        )


def _turn(trace_id: str, prompt: str, response: str = "", **extra) -> dict:
    turn = {
        "trace_id": trace_id,
        "prompt_text": prompt,
        "response_text": response or f"reply to {prompt}",
        "tool_calls": [],
        "tool_results": [],
        "tool_errors": [],
    }
    turn.update(extra)
    return turn


def _bad_session(turns: list[dict], *, overall: float = 0.2, evidence: str = "defect") -> dict:
    return {
        "session_id": "sess-bad",
        "turns": turns,
        "user_alias": "42749155",
        "meta": {"user_id": "42749155", "trace_id": "biz-trace-1"},
        "ingested_at": "2026-09-18T00:00:00+00:00",
        "_judge_scores": {
            "overall_score": overall,
            "evolution_evidence": evidence,
            "evidence_reason": "路由到了错误的技能",
            "rationale": "整体失败",
        },
        "_summary": "会话失败：工具报错且被用户纠正。",
    }


@pytest.fixture(autouse=True)
def _isolate_hooks(monkeypatch):
    """Each test starts with no in-process hooks and no env hook."""
    ta.clear_trace_analysis_hooks()
    monkeypatch.delenv(ta._ENV_HOOK_ENV, raising=False)
    ta._reset_env_hook_cache()
    yield
    ta.clear_trace_analysis_hooks()
    ta._reset_env_hook_cache()


# --------------------------------------------------------------------------- #
# problem-type vocabulary                                                      #
# --------------------------------------------------------------------------- #


def test_normalize_problem_type_maps_unknown_to_other():
    assert ta.normalize_problem_type("tool_error") == "tool_error"
    assert ta.normalize_problem_type("Tool-Error") == "tool_error"
    assert ta.normalize_problem_type("skill gap") == "skill_gap"
    assert ta.normalize_problem_type("nonsense") == "other"
    assert ta.normalize_problem_type("") == "other"
    assert ta.normalize_problem_type(None) == "other"


# --------------------------------------------------------------------------- #
# LLM localization                                                             #
# --------------------------------------------------------------------------- #


def test_llm_localizes_problem_traces():
    session = _bad_session(
        [
            _turn("t1", "帮我查数据"),
            _turn("t2", "继续"),
            _turn("t3", "改一下"),
        ]
    )
    reply = json.dumps(
        {
            "traces": [
                {
                    "trace_id": "t1",
                    "turn_num": 1,
                    "is_badcase": False,
                    "problem_type": "other",
                    "problem_description": "",
                },
                {
                    "trace_id": "t2",
                    "turn_num": 2,
                    "is_badcase": True,
                    "problem_type": "tool_error",
                    "problem_description": "exec 调用报错 boom",
                },
                {
                    "trace_id": "t3",
                    "turn_num": 3,
                    "is_badcase": True,
                    "problem_type": "skill_misselect",
                    "problem_description": "路由到了错误技能",
                },
            ]
        }
    )
    client = _FakeClient(reply)
    results = asyncio.run(ta.analyze_session_traces(client, session))

    assert client.calls, "LLM should be called"
    assert len(results) == 3  # one row per trace
    by_trace = {r["trace_id"]: r for r in results}

    assert by_trace["t1"]["is_badcase"] is False
    assert by_trace["t1"]["problem_type"] == "other"
    assert by_trace["t1"]["problem_description"] == ""

    assert by_trace["t2"]["is_badcase"] is True
    assert by_trace["t2"]["problem_type"] == "tool_error"
    assert by_trace["t2"]["problem_type_label"] == "工具使用错误"
    assert "boom" in by_trace["t2"]["problem_description"]

    assert by_trace["t3"]["problem_type"] == "skill_misselect"
    assert all(r["source"] == "llm" for r in results)
    # identity + timestamps threaded through
    assert all(r["user_id"] == "42749155" for r in results)
    assert all(r["session_trace_id"] == "biz-trace-1" for r in results)
    assert all(r["ingested_at"] == "2026-09-18T00:00:00+00:00" for r in results)


def test_llm_off_vocabulary_type_normalizes_to_other():
    session = _bad_session([_turn("t1", "q")])
    reply = json.dumps(
        {
            "traces": [
                {
                    "trace_id": "t1",
                    "turn_num": 1,
                    "is_badcase": True,
                    "problem_type": "made_up_type",
                    "problem_description": "有问题",
                },
            ]
        }
    )
    results = asyncio.run(ta.analyze_session_traces(_FakeClient(reply), session))
    assert results[0]["problem_type"] == "other"
    assert results[0]["is_badcase"] is True  # description present keeps it flagged


# --------------------------------------------------------------------------- #
# model failures and incomplete responses                                      #
# --------------------------------------------------------------------------- #


def test_missing_model_fails_without_dispatching_results():
    session = _bad_session(
        [
            _turn("t1", "run it", tool_errors=[{"tool_name": "exec", "content": "boom"}]),
            _turn("t2", "ok"),
        ]
    )
    received = []
    ta.register_trace_analysis_hook(received.append)
    with pytest.raises(RuntimeError, match="Trace Analyze"):
        asyncio.run(ta.analyze_and_dispatch_traces(None, session))
    assert received == []


@pytest.mark.parametrize("error", [RuntimeError("boom"), LLMOverloadedError("queue full")])
def test_model_failure_never_produces_rule_based_results(error):
    class _BrokenClient:
        async def chat(self, messages, **kwargs) -> str:
            raise error

    session = _bad_session(
        [
            _turn(
                "t1",
                "run",
                tool_results=[{"tool_name": "exec", "has_error": True, "error_type": "E", "content": "failed"}],
            ),
        ]
    )
    received = []
    ta.register_trace_analysis_hook(received.append)
    with pytest.raises(RuntimeError):
        asyncio.run(ta.analyze_and_dispatch_traces(_BrokenClient(), session))
    assert received == []


def test_model_all_normal_verdict_is_not_overridden_by_session_score():
    session = _bad_session([_turn("t1", "q"), _turn("t2", "q2")])
    results = asyncio.run(ta.analyze_session_traces(_AllGoodClient(), session))
    assert all(row["source"] == "llm" and row["is_badcase"] is False for row in results)
    assert all(row["problem_description"] == "" for row in results)


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        '{"traces":[]}',
        '{"traces":[{"trace_id":"t1","turn_num":1}]}',
        '{"traces":[{"trace_id":"t1","turn_num":1,"is_badcase":"false"}]}',
        '{"traces":[{"trace_id":"t1","turn_num":1,"is_badcase":true,"problem_type":"other","problem_description":""}]}',
        '{"traces":[{"trace_id":"unknown","turn_num":1,"is_badcase":false,'
        '"problem_type":"other","problem_description":""}]}',
    ],
)
def test_invalid_model_reply_cannot_become_a_normal_result(reply):
    session = _bad_session([_turn("t1", "q")])
    received = []
    ta.register_trace_analysis_hook(received.append)
    with pytest.raises(RuntimeError, match="Trace Analyze"):
        asyncio.run(ta.analyze_and_dispatch_traces(_FakeClient(reply), session))
    assert received == []


def test_missing_trace_in_model_reply_fails_whole_session():
    session = _bad_session([_turn("t1", "q1"), _turn("t2", "q2")])
    reply = json.dumps(
        {
            "traces": [
                {
                    "trace_id": "t1",
                    "turn_num": 1,
                    "is_badcase": False,
                    "problem_type": "other",
                    "problem_description": "",
                }
            ]
        }
    )
    received = []
    ta.register_trace_analysis_hook(received.append)
    with pytest.raises(RuntimeError, match="Trace Analyze"):
        asyncio.run(ta.analyze_and_dispatch_traces(_FakeClient(reply), session))
    assert received == []


def test_late_batch_failure_does_not_dispatch_partial_results():
    class _LateFailureClient(_AllGoodClient):
        async def chat(self, messages, **kwargs):
            if self.calls:
                raise RuntimeError("second batch failed")
            return await super().chat(messages, **kwargs)

    session = _bad_session([_turn(f"t{idx}", "q") for idx in range(61)])
    received = []
    ta.register_trace_analysis_hook(received.append)
    with pytest.raises(RuntimeError):
        asyncio.run(ta.analyze_and_dispatch_traces(_LateFailureClient(), session))
    assert received == []


# --------------------------------------------------------------------------- #
# gating                                                                       #
# --------------------------------------------------------------------------- #


def test_good_session_is_still_analyzed():
    good = _bad_session([_turn("t1", "q")], overall=0.9, evidence="none")
    client = _AllGoodClient()
    results = asyncio.run(ta.analyze_and_dispatch_traces(client, good))
    assert len(client.calls) == 1
    assert len(results) == 1
    assert results[0]["trace_id"] == "t1"


def test_legacy_env_toggle_cannot_disable_analysis(monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_TRACE_BADCASE_ANALYSIS", "0")
    session = _bad_session([_turn("t1", "q")])
    client = _AllGoodClient()
    assert len(asyncio.run(ta.analyze_and_dispatch_traces(client, session))) == 1
    assert len(client.calls) == 1


def test_every_trace_is_analyzed_when_session_exceeds_batch_size():
    session = _bad_session([_turn(f"t{idx}", f"q{idx}") for idx in range(1, 62)])
    client = _AllGoodClient()

    results = asyncio.run(ta.analyze_and_dispatch_traces(client, session))

    assert len(results) == 61
    assert [row["trace_id"] for row in results] == [f"t{idx}" for idx in range(1, 62)]
    assert all(row["source"] == "llm" for row in results)
    assert len(client.calls) == 2


def _incremental_session(*, count=2, targets=(2,)):
    """Original source numbering remains visible in full context after filtering."""
    turns = [_turn(f"display-{i}", f"Question {i}", source_trace_id=f"upstream-{i}", turn_num=i)
             for i in range(1, count + 1)]
    session = _bad_session([copy.deepcopy(turns[i - 1]) for i in targets])
    session["analysis_context"] = {"turns": copy.deepcopy(turns), "context_only": True}
    session["provenance"] = {"identity_verified": True, "trace_identity_verified": True,
                             "target_trace_refs": [{"trace_id": f"upstream-{i}"} for i in targets],
                             "context_trace_refs": [{"trace_id": f"upstream-{i}"} for i in range(1, count + 1)
                                                    if i not in targets]}
    return session


def _normal_verdict(identity, number):
    return {"trace_id": identity, "turn_num": number, "is_badcase": False,
            "problem_type": "other", "problem_description": ""}


def test_incremental_target_keeps_second_source_position_and_complete_context_without_analyzing_old_trace():
    session = _incremental_session()
    original = copy.deepcopy(session)
    client = _FakeClient(json.dumps({"traces": [_normal_verdict("upstream-2", 2)]}))
    result = asyncio.run(ta.analyze_session_traces(client, session))
    payload = json.loads(client.calls[0]["messages"][-1]["content"])
    assert payload["total_traces"] == 1
    assert [(t["trace_id"], t["turn_num"]) for t in payload["traces"]] == [("upstream-2", 2)]
    assert payload["analysis_context"] == original["analysis_context"]
    assert [(t["trace_id"], t["turn_num"]) for t in result] == [("upstream-2", 2)]
    assert session["turns"] == original["turns"]


@pytest.mark.parametrize("returned", [
    [_normal_verdict("upstream-1", 1)],  # A context-only Trace is not an analysis target.
    [_normal_verdict("unknown", 2)],
    [_normal_verdict("upstream-2", 1)],  # Renumbering the correct ID is still rejected.
    [_normal_verdict("upstream-2", 2), _normal_verdict("upstream-2", 2)],
    [_normal_verdict("upstream-1", 1), _normal_verdict("upstream-2", 2)],
])
def test_incremental_response_still_requires_exact_target_identity_coverage_and_number(returned):
    with pytest.raises(ta.TraceAnalysisError):
        asyncio.run(ta.analyze_session_traces(_FakeClient(json.dumps({"traces": returned})), _incremental_session()))


def test_sparse_targets_keep_full_source_positions_across_trace_batches(monkeypatch):
    monkeypatch.setattr(ta, "_TRACE_BATCH_SIZE", 1)
    client = _AllGoodClient()
    session = _incremental_session(count=5, targets=(2, 5))
    results = asyncio.run(ta.analyze_session_traces(client, session))
    assert [(r["trace_id"], r["turn_num"]) for r in results] == [("upstream-2", 2), ("upstream-5", 5)]
    assert len(client.calls) == 2
    assert [json.loads(c["messages"][-1]["content"])["traces"][0]["turn_num"] for c in client.calls] == [2, 5]


def test_duplicate_target_reply_is_rejected_even_when_response_count_matches():
    session = _incremental_session(count=4, targets=(2, 4))
    duplicated = [_normal_verdict("upstream-2", 2), _normal_verdict("upstream-2", 2)]
    with pytest.raises(ta.TraceAnalysisError, match="duplicate trace/turn"):
        asyncio.run(ta.analyze_session_traces(_FakeClient(json.dumps({"traces": duplicated})), session))


@pytest.mark.parametrize("mutation", ["missing_target", "duplicate_identity"])
def test_incomplete_or_ambiguous_verified_context_is_rejected_before_calling_model(mutation):
    session = _incremental_session()
    if mutation == "missing_target":
        session["analysis_context"]["turns"].pop()
    else:
        session["analysis_context"]["turns"].append(copy.deepcopy(session["analysis_context"]["turns"][0]))
    client = _AllGoodClient()
    with pytest.raises(ta.TraceAnalysisError):
        asyncio.run(ta.analyze_session_traces(client, session))
    assert client.calls == []


def test_split_segment_retains_full_source_position_and_legacy_partial_batch_retains_existing_numbering():
    # A second-topic segment can contain only one turn but retain the full source Context.
    split = _incremental_session(count=4, targets=(4,))
    split["metadata"] = {"split_from_session": "root", "split_segment": {"turn_range": [4, 4]}}
    assert ta.build_trace_payload(split)["traces"][0]["turn_num"] == 4
    legacy = _bad_session([_turn("old-one", "one"), _turn("old-two", "two")])
    legacy["turns"][1]["turn_num"] = 99  # Legacy numbering has always followed the accepted turn sequence.
    payload = ta.build_trace_payload(legacy, turns=legacy["turns"][1:], start_turn_num=2)
    assert [(r["trace_id"], r["turn_num"]) for r in payload["traces"]] == [("old-two", 2)]
    unverified = _incremental_session()
    unverified["provenance"]["identity_verified"] = False
    assert ta.build_trace_payload(unverified)["traces"][0]["turn_num"] == 1


def test_empty_session_yields_no_results():
    assert asyncio.run(ta.analyze_session_traces(None, {"session_id": "x", "turns": []})) == []


# --------------------------------------------------------------------------- #
# hook dispatch (one call per trace result)                                    #
# --------------------------------------------------------------------------- #


def test_hook_fires_once_per_trace_result():
    session = _bad_session(
        [
            _turn("t1", "a", tool_errors=[{"tool_name": "exec", "content": "boom"}]),
            _turn("t2", "b"),
            _turn("t3", "c"),
        ]
    )
    received: list[dict] = []
    ta.register_trace_analysis_hook(received.append)

    results = asyncio.run(ta.analyze_and_dispatch_traces(_AllGoodClient(), session))
    # one hook call per produced trace result (3 traces -> 3 calls)
    assert len(received) == len(results) == 3
    assert [r["trace_id"] for r in received] == ["t1", "t2", "t3"]
    # every dispatched result carries the required trace-level fields
    for row in received:
        assert set(row) >= {"session_id", "trace_id", "problem_type", "problem_description"}


def test_hook_failure_is_swallowed():
    session = _bad_session([_turn("t1", "a")])

    def _boom(_result):
        raise RuntimeError("hook down")

    ok: list[dict] = []
    ta.register_trace_analysis_hook(_boom)
    ta.register_trace_analysis_hook(ok.append)  # still runs despite the first

    results = asyncio.run(ta.analyze_and_dispatch_traces(_AllGoodClient(), session))
    assert results, "analysis result returned despite a failing hook"
    assert len(ok) == len(results)


def test_duplicate_registration_ignored():
    session = _bad_session([_turn("t1", "a")])
    seen: list[dict] = []
    ta.register_trace_analysis_hook(seen.append)
    ta.register_trace_analysis_hook(seen.append)  # dedup
    asyncio.run(ta.analyze_and_dispatch_traces(_AllGoodClient(), session))
    assert len(seen) == 1  # exactly one trace, hook registered once


def test_no_hook_when_fire_hook_false():
    session = _bad_session([_turn("t1", "a")])
    seen: list[dict] = []
    ta.register_trace_analysis_hook(seen.append)
    results = asyncio.run(ta.analyze_and_dispatch_traces(_AllGoodClient(), session, fire_hook=False))
    assert results and not seen


# --------------------------------------------------------------------------- #
# env-configured hook                                                          #
# --------------------------------------------------------------------------- #


def test_env_hook_loaded_and_invoked(monkeypatch):
    monkeypatch.setenv(ta._ENV_HOOK_ENV, "team_skills.evolution.stages.trace_analyze:_test_env_sink")
    ta._reset_env_hook_cache()
    ta._TEST_ENV_SINK.clear()
    session = _bad_session([_turn("t1", "a")])
    results = asyncio.run(ta.analyze_and_dispatch_traces(_AllGoodClient(), session))
    assert len(ta._TEST_ENV_SINK) == len(results) == 1


def test_bad_env_hook_spec_is_ignored(monkeypatch):
    monkeypatch.setenv(ta._ENV_HOOK_ENV, "not-a-valid-spec")
    ta._reset_env_hook_cache()
    session = _bad_session([_turn("t1", "a")])
    # must not raise; analysis still returns results
    results = asyncio.run(ta.analyze_and_dispatch_traces(_AllGoodClient(), session))
    assert results


# --------------------------------------------------------------------------- #
# ingest wiring (service._ingest_one fires trace analysis for every session)  #
# --------------------------------------------------------------------------- #
