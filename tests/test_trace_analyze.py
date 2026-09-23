"""Tests for mandatory trace-level analysis."""

from __future__ import annotations

import asyncio
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
        return json.dumps({
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
        })


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
    session = _bad_session([
        _turn("t1", "帮我查数据"),
        _turn("t2", "继续"),
        _turn("t3", "改一下"),
    ])
    reply = json.dumps({
        "traces": [
            {"trace_id": "t1", "turn_num": 1, "is_badcase": False,
             "problem_type": "other", "problem_description": ""},
            {"trace_id": "t2", "turn_num": 2, "is_badcase": True,
             "problem_type": "tool_error", "problem_description": "exec 调用报错 boom"},
            {"trace_id": "t3", "turn_num": 3, "is_badcase": True,
             "problem_type": "skill_misselect", "problem_description": "路由到了错误技能"},
        ]
    })
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
    reply = json.dumps({
        "traces": [
            {"trace_id": "t1", "turn_num": 1, "is_badcase": True,
             "problem_type": "made_up_type", "problem_description": "有问题"},
        ]
    })
    results = asyncio.run(ta.analyze_session_traces(_FakeClient(reply), session))
    assert results[0]["problem_type"] == "other"
    assert results[0]["is_badcase"] is True  # description present keeps it flagged


# --------------------------------------------------------------------------- #
# model failures and incomplete responses                                      #
# --------------------------------------------------------------------------- #

def test_missing_model_fails_without_dispatching_results():
    session = _bad_session([
        _turn("t1", "run it", tool_errors=[{"tool_name": "exec", "content": "boom"}]),
        _turn("t2", "ok"),
    ])
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

    session = _bad_session([
        _turn("t1", "run", tool_results=[{"tool_name": "exec", "has_error": True,
                                          "error_type": "E", "content": "failed"}]),
    ])
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
        '{"traces":[{"trace_id":"t1","turn_num":1,"is_badcase":true,'
        '"problem_type":"other","problem_description":""}]}',
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
    reply = json.dumps({"traces": [{
        "trace_id": "t1", "turn_num": 1, "is_badcase": False,
        "problem_type": "other", "problem_description": "",
    }]})
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


def test_empty_session_yields_no_results():
    assert asyncio.run(ta.analyze_session_traces(None, {"session_id": "x", "turns": []})) == []


# --------------------------------------------------------------------------- #
# hook dispatch (one call per trace result)                                    #
# --------------------------------------------------------------------------- #

def test_hook_fires_once_per_trace_result():
    session = _bad_session([
        _turn("t1", "a", tool_errors=[{"tool_name": "exec", "content": "boom"}]),
        _turn("t2", "b"),
        _turn("t3", "c"),
    ])
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
    results = asyncio.run(
        ta.analyze_and_dispatch_traces(_AllGoodClient(), session, fire_hook=False)
    )
    assert results and not seen


# --------------------------------------------------------------------------- #
# env-configured hook                                                          #
# --------------------------------------------------------------------------- #

def test_env_hook_loaded_and_invoked(monkeypatch):
    monkeypatch.setenv(
        ta._ENV_HOOK_ENV, "team_skills.evolution.stages.trace_analyze:_test_env_sink"
    )
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

@pytest.mark.parametrize(
    ("overall", "evidence", "decision", "expected_status"),
    [
        (0.2, "defect", "skipped", "skipped"),
        (0.9, "none", "valuable", "queued"),
    ],
)
def test_ingest_fires_trace_hook_for_every_session(
    monkeypatch,
    overall,
    evidence,
    decision,
    expected_status,
):
    """Both good and bad sessions trigger per-trace analysis and hooks."""
    from types import SimpleNamespace

    import session_ingestion.service as service_mod

    class _FakeStore:
        @classmethod
        def from_config(cls, config, tenant_id=None):
            return cls()

        def duplicate_of_processed(self, session) -> bool:
            return False

        def save_queued(self, session) -> str:
            return "key"

        def save_skipped(self, session) -> None:
            return None

    class _StubClassifier:
        client = _FakeClient(json.dumps({"traces": [{
            "trace_id": "t1",
            "turn_num": 1,
            "is_badcase": evidence == "defect",
            "problem_type": "tool_error" if evidence == "defect" else "other",
            "problem_description": "exec 调用报错 boom" if evidence == "defect" else "",
        }]}))

        @classmethod
        def from_config(cls, config):
            return cls()

        async def classify(self, session) -> dict:
            session["_judge_scores"] = {
                "overall_score": overall,
                "evolution_evidence": evidence,
                "evidence_reason": "工具报错" if evidence == "defect" else "",
            }
            return {
                "decision": decision,
                "confidence": 0.9,
                "reason": evidence,
                "mode": "stub",
            }

    monkeypatch.setattr(service_mod, "SessionStore", _FakeStore)
    monkeypatch.setattr(service_mod, "SessionValueClassifier", _StubClassifier)

    async def _stub_session_analysis(session, classifier):
        session["_summary"] = "stub analysis"
        session["_judge_scores"] = {
            "overall_score": overall,
            "evolution_evidence": evidence,
            "evidence_reason": "工具报错" if evidence == "defect" else "",
        }
        return {
            "decision": decision,
            "confidence": 0.9,
            "reason": evidence,
            "mode": "merged",
        }

    monkeypatch.setattr(service_mod, "_classify_session", _stub_session_analysis)
    # Skip the semantic split LLM call.
    monkeypatch.setattr(
        "session_ingestion.split._split_client", lambda config: None
    )

    received: list[dict] = []
    ta.register_trace_analysis_hook(received.append)

    owner = SimpleNamespace(
        config=SimpleNamespace(
            session_split_enabled=True,
            llm_api_key="", llm_api_base="", llm_model_id="", model_name="",
        ),
        _session_judge_queue=None,
    )
    owner._schedule_evolve_trigger = lambda: True

    session = {
        "session_id": "sess-bad",
        "turns": [
            _turn(
                "t1",
                "run",
                tool_errors=(
                    [{"tool_name": "exec", "content": "boom"}]
                    if evidence == "defect"
                    else []
                ),
            )
        ],
        "user_alias": "u1",
    }
    outcome = asyncio.run(service_mod.ingest(owner, session))

    assert outcome["status"] == expected_status
    assert received, "trace hook should fire for every session at ingest"
    assert received[0]["trace_id"] == "t1"
    assert received[0]["problem_type"] == (
        "tool_error" if evidence == "defect" else "other"
    )
