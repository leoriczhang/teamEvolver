"""Semantic Session split contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from session_ingestion.split import split_session


class _Client:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.factory_calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        assert "total_turns" in messages[-1]["content"]
        assert kwargs["trace_name"] == "session_ingestion.split_session"
        return self.reply

    async def factory(self):
        self.factory_calls += 1
        return self


@pytest.mark.asyncio
async def test_semantic_split_preserves_trace_evidence_and_segment_identity():
    base_id = "s" * 200
    session = {
        "session_id": base_id,
        "business_time": "2026-10-01T12:00:00+00:00",
        "turns": [
            {
                "prompt_text": "topic one",
                "tool_calls": [{"id": "c1"}],
                "injected_skills": ["catalog"],
                "used_skills": ["skill-a"],
            },
            {
                "prompt_text": "topic one follow-up",
                "tool_results": [{"tool_call_id": "c1"}],
                "injected_skills": ["catalog"],
                "used_skills": ["skill-a"],
            },
            {
                "prompt_text": "topic two",
                "injected_skills": ["catalog"],
                "used_skills": ["skill-b"],
            },
        ],
    }
    client = _Client(
        '{"segments":[{"start":1,"end":2,"topic":"one"},'
        '{"start":3,"end":3,"topic":"two"}]}'
    )

    segments = await split_session(
        SimpleNamespace(session_split_enabled=True),
        session,
        client_factory=client.factory,
    )

    assert client.calls == 1
    assert client.factory_calls == 1
    assert [len(segment["turns"]) for segment in segments] == [2, 1]
    assert segments[0]["session_id"].endswith("_s001-002")
    assert segments[1]["session_id"].endswith("_s003-003")
    assert all(len(segment["session_id"]) == 200 for segment in segments)
    assert segments[0]["business_time"] == session["business_time"]
    assert segments[0]["injected_skills"] == ["catalog"]
    assert segments[0]["used_skills"] == ["skill-a"]
    assert segments[0]["turns"][0]["tool_calls"] == [{"id": "c1"}]
    assert segments[1]["used_skills"] == ["skill-b"]
    assert segments[0]["metadata"]["split_from_session"] == base_id


@pytest.mark.asyncio
async def test_split_segment_is_not_split_recursively():
    session = {
        "session_id": "s1_s001-002",
        "turns": [{"prompt_text": "already split"}],
        "metadata": {"split_from_session": "s1"},
    }
    client = _Client('{"segments":[{"start":1,"end":1,"topic":"unused"}]}')

    segments = await split_session(
        SimpleNamespace(session_split_enabled=True),
        session,
        client_factory=client.factory,
    )

    assert segments == [session]
    assert client.calls == 0
    assert client.factory_calls == 0


@pytest.mark.asyncio
async def test_missing_split_model_fails_open_to_original_session():
    session = {
        "session_id": "s1",
        "turns": [{"prompt_text": "one"}, {"prompt_text": "two"}],
    }

    async def unavailable():
        raise RuntimeError("model is not configured")

    segments = await split_session(
        SimpleNamespace(session_split_enabled=True),
        session,
        client_factory=unavailable,
    )

    assert segments == [session]
