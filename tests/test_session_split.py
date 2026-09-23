"""Tests for semantic session topic splitting (session_ingestion/split.py)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from session_ingestion import split as split_mod
from session_ingestion.service import _aggregate_split_outcome
from session_ingestion.split import _coerce_boundaries, split_session


def _config(**overrides) -> SimpleNamespace:
    values = {
        "session_split_enabled": True,
        "llm_api_key": "",
        "llm_api_base": "",
        "llm_model_id": "",
        "model_name": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _turn(prompt: str, ts: str = "", **extra) -> dict:
    turn = {
        "prompt_text": prompt,
        "response_text": f"reply to {prompt}",
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": f"reply to {prompt}"},
        ],
        "tool_calls": [],
        "metrics": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }
    if ts:
        turn["timestamp"] = ts
    turn.update(extra)
    return turn


def _session(turns: list[dict], session_id: str = "sess-1") -> dict:
    return {
        "session_id": session_id,
        "turns": turns,
        "title": "原始标题",
        "user_alias": "alice",
        "source": "langfuse",
        "metadata": {"langfuse_session_id": "raw:1"},
        "force_reprocess": True,
        "reprocess_reason": "test reingest",
    }


class _FakeClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.reply


class _TraceClient:
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        return json.dumps({"traces": [
            {
                "trace_id": trace["trace_id"],
                "turn_num": trace["turn_num"],
                "is_badcase": False,
                "problem_type": "other",
                "problem_description": "",
            }
            for trace in payload["traces"]
        ]})


def _client_for(segments: list[dict]) -> _FakeClient:
    return _FakeClient(json.dumps({"segments": segments}))


def _patch_client(monkeypatch, client) -> None:
    monkeypatch.setattr(split_mod, "_split_client", lambda config: client)


def test_no_model_passes_through() -> None:
    session = _session([_turn(f"q{i}") for i in range(5)])
    assert asyncio.run(split_session(_config(), session)) == [session]


def test_env_toggle_disables_split(monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_SESSION_SPLIT", "0")
    session = _session([_turn(f"q{i}") for i in range(10)])
    assert asyncio.run(split_session(_config(), session)) == [session]


def test_config_disable(monkeypatch) -> None:
    monkeypatch.delenv("TEAMEVOLVER_SESSION_SPLIT", raising=False)
    session = _session([_turn(f"q{i}") for i in range(10)])
    assert asyncio.run(split_session(_config(session_split_enabled=False), session)) == [session]


def test_already_split_session_not_resplit() -> None:
    session = _session([_turn(f"q{i}") for i in range(10)])
    session["metadata"]["split_from_session"] = "orig"
    assert asyncio.run(split_session(_config(), session)) == [session]


def test_candidate_audit_not_split() -> None:
    session = _session([_turn(f"q{i}") for i in range(10)])
    session["runtime_context"] = {"candidate_job_id": "job-1", "candidate_sha256": "abc"}
    assert asyncio.run(split_session(_config(), session)) == [session]


def test_empty_turns_not_split() -> None:
    session = _session([])
    assert asyncio.run(split_session(_config(), session)) == [session]


def test_small_session_split_semantically(monkeypatch) -> None:
    """No turn-count gating: even a 2-turn session splits when topics differ."""
    client = _client_for(
        [
            {"start": 1, "end": 1, "topic": "查天气"},
            {"start": 2, "end": 2, "topic": "改周报"},
        ]
    )
    _patch_client(monkeypatch, client)
    turns = [_turn("今天天气怎么样"), _turn("帮我改一下周报")]
    segments = asyncio.run(split_session(_config(), _session(turns)))

    assert client.calls, "boundary LLM call should happen for every session"
    assert [s["session_id"] for s in segments] == [
        "sess-1_s001-001",
        "sess-1_s002-002",
    ]
    assert [s["metadata"]["split_segment"]["topic"] for s in segments] == [
        "查天气",
        "改周报",
    ]
    assert all(s["metadata"]["split_segment"]["strategy"] == "semantic" for s in segments)


def test_single_topic_reply_keeps_session_whole(monkeypatch) -> None:
    client = _client_for([{"start": 1, "end": 6, "topic": "同一个任务"}])
    _patch_client(monkeypatch, client)
    turns = [_turn(f"q{i}") for i in range(6)]
    segments = asyncio.run(split_session(_config(), _session(turns)))
    assert segments == [_session(turns)]
    assert len(client.calls) == 1


def test_segment_construction(monkeypatch) -> None:
    client = _client_for(
        [
            {"start": 1, "end": 3, "topic": "任务一"},
            {"start": 4, "end": 6, "topic": "任务二"},
        ]
    )
    _patch_client(monkeypatch, client)
    turns = [
        _turn("任务一 a", ts="2026-09-16T10:00:00+00:00"),
        _turn("任务一 b"),
        _turn("任务一 c", used_skills=["skill-x"]),
        _turn("任务二 a", ts="2026-09-16T12:00:00+00:00"),
        _turn("任务二 b"),
        _turn("任务二 c"),
    ]
    segments = asyncio.run(split_session(_config(), _session(turns)))

    assert len(segments) == 2
    first, second = segments
    assert first["metadata"]["split_from_session"] == "sess-1"
    assert first["metadata"]["split_segment"]["turn_range"] == [1, 3]
    assert first["metrics"]["interaction_turns"] == 3
    assert first["metrics"]["total_tokens"] == 45
    assert first["used_skills"] == ["skill-x"]
    assert first["user_alias"] == "alice"
    assert first["force_reprocess"] is True
    assert first["title"].startswith("任务一")
    assert second["messages"][0]["content"] == "任务二 a"
    # segments carry a fresh timestamp from their first turn
    assert second["timestamp"].startswith("2026-09-16T12:00:00")


def test_coerce_boundaries_clips_and_fills() -> None:
    raw = json.dumps(
        {
            "segments": [
                {"start": 2, "end": 5, "topic": "A"},
                {"start": 4, "end": 8, "topic": "B"},  # overlap clipped
            ]
        }
    )
    # gap 1..1 absorbed before the first segment; overlap 4..5 clipped so B
    # starts after A ends; 9..10 absorbed into the last segment
    assert _coerce_boundaries(raw, 10) == [
        (1, 1, ""),
        (2, 5, "A"),
        (6, 10, "B"),
    ]


def test_coerce_boundaries_rejects_garbage() -> None:
    assert _coerce_boundaries("not json at all", 10) == []
    assert _coerce_boundaries(json.dumps({"segments": []}), 10) == []
    assert _coerce_boundaries(json.dumps({"segments": [{"start": "x"}]}), 10) == []


def test_llm_failure_leaves_session_unsplit(monkeypatch) -> None:
    class _BrokenClient:
        async def chat(self, messages, **kwargs) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(split_mod, "_split_client", lambda config: _BrokenClient())
    session = _session([_turn(f"q{i}") for i in range(6)])
    assert asyncio.run(split_session(_config(), session)) == [session]


def test_aggregate_split_outcome() -> None:
    outcome = _aggregate_split_outcome(
        "orig",
        [
            {"status": "skipped", "session_id": "orig_s001-030", "queued": False},
            {
                "status": "queued",
                "session_id": "orig_s031-060",
                "queued": True,
                "value_judge": {"decision": "valuable"},
            },
            {"status": "error", "session_id": "orig_s061-090", "queued": False},
        ],
    )
    assert outcome["status"] == "queued"
    assert outcome["queued"] is True
    assert outcome["split"] is True
    assert outcome["session_id"] == "orig"
    assert outcome["value_judge"] == {"decision": "valuable"}
    assert len(outcome["segments"]) == 3


def test_ingest_splits_and_queues_each_segment(monkeypatch) -> None:
    """Service-level: a semantically mixed session is split and each part classified."""
    import session_ingestion.service as service_mod

    class _FakeStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @classmethod
        def from_config(cls, config, tenant_id=None):
            return cls()

        async def duplicate_of_processed(self, session) -> bool:
            return False

        def save_queued(self, session) -> str:
            return f"key:{session['session_id']}"

        def save_skipped(self, session) -> None:
            return None

    class _StubClassifier:
        client = _TraceClient()

        @classmethod
        def from_config(cls, config):
            return cls()

        async def classify(self, session) -> dict:
            return {
                "decision": "valuable",
                "confidence": 0.9,
                "reason": "stub",
                "mode": "stub",
            }

    monkeypatch.setattr(service_mod, "SessionStore", _FakeStore)
    monkeypatch.setattr(service_mod, "SessionValueClassifier", _StubClassifier)

    async def _stub_session_analysis(session, classifier):
        session["_summary"] = "stub analysis"
        session["_judge_scores"] = {
            "overall_score": 0.9,
            "evolution_evidence": "none",
        }
        return {
            "decision": "valuable",
            "confidence": 0.9,
            "reason": "stub",
            "mode": "merged",
        }

    monkeypatch.setattr(service_mod, "_classify_session", _stub_session_analysis)
    monkeypatch.setattr(
        split_mod,
        "_split_client",
        lambda config: _client_for(
            [
                {"start": 1, "end": 7, "topic": "话题A"},
                {"start": 8, "end": 12, "topic": "话题B"},
            ]
        ),
    )

    owner = SimpleNamespace(
        config=_config(),
        _session_judge_queue=None,
    )
    owner._schedule_evolve_trigger = lambda: True

    invalidated: list[tuple] = []
    session = _session([_turn(f"q{i}") for i in range(12)], session_id="big-sess")
    outcome = asyncio.run(
        service_mod.ingest(owner, session, invalidate_cache=lambda *keys: invalidated.append(keys))
    )

    assert outcome["status"] == "queued"
    assert outcome["split"] is True
    assert outcome["queued"] is True
    assert [seg["status"] for seg in outcome["segments"]] == ["queued", "queued"]
    assert all(seg["session_id"].startswith("big-sess_s") for seg in outcome["segments"])
    assert invalidated, "invalidate_cache should fire per ingested segment"


def test_ingest_unsplit_path_unchanged(monkeypatch) -> None:
    """Without a split client, sessions keep the exact single-session ingest behavior."""
    import session_ingestion.service as service_mod

    class _FakeStore:
        @classmethod
        def from_config(cls, config, tenant_id=None):
            return cls()

        async def duplicate_of_processed(self, session) -> bool:
            return False

        def save_queued(self, session) -> str:
            return "key"

        def save_skipped(self, session) -> None:
            return None

    class _StubClassifier:
        client = _TraceClient()

        @classmethod
        def from_config(cls, config):
            return cls()

        async def classify(self, session) -> dict:
            return {"decision": "valuable", "confidence": 0.9, "reason": "stub", "mode": "stub"}

    monkeypatch.setattr(service_mod, "SessionStore", _FakeStore)
    monkeypatch.setattr(service_mod, "SessionValueClassifier", _StubClassifier)

    async def _stub_session_analysis(session, classifier):
        session["_summary"] = "stub analysis"
        session["_judge_scores"] = {
            "overall_score": 0.9,
            "evolution_evidence": "none",
        }
        return {
            "decision": "valuable",
            "confidence": 0.9,
            "reason": "stub",
            "mode": "merged",
        }

    monkeypatch.setattr(service_mod, "_classify_session", _stub_session_analysis)

    owner = SimpleNamespace(
        config=_config(),
        _session_judge_queue=None,
    )
    owner._schedule_evolve_trigger = lambda: True

    session = _session([_turn(f"q{i}") for i in range(4)])
    outcome = asyncio.run(service_mod.ingest(owner, session))
    assert outcome["status"] == "queued"
    assert "split" not in outcome
