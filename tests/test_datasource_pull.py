from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from session_ingestion.adapters import _runtime
from session_ingestion.pull import service


class FakeAdapter:
    def __init__(self, session_ids):
        self.session_ids = list(session_ids)
        self.fetch_calls = []
        self.closed = False
        self.retry_count = 2

    def list_session_ids(self, _filters, *, max_sessions):
        return self.session_ids[:max_sessions]

    def preview_sessions(self, _filters, *, max_sessions):
        return [
            {"session_id": session_id, "title": session_id}
            for session_id in self.session_ids[:max_sessions]
        ]

    def fetch_session(self, session_id):
        self.fetch_calls.append(session_id)
        return {"id": session_id}, []

    def convert_session(self, raw, _traces):
        return {
            "session_id": raw["id"],
            "turns": [{"prompt_text": "question", "response_text": "answer"}],
        }

    def close(self):
        self.closed = True


def _descriptor(**overrides):
    value = {
        "configured": True,
        "enabled": True,
        "file": "test.py",
        "revision": "revision",
        "supported_filters": ["session_id"],
        "required_filters": [],
        "max_sessions": 100,
        "exclude_session_id_patterns": ["origin-*", "rollout-*"],
    }
    value.update(overrides)
    return value


def test_adapter_metadata_validates_exclusion_patterns() -> None:
    source = """
SOURCE = {
    "supported_filters": [],
    "required_filters": [],
    "exclude_session_id_patterns": ["origin-*"],
}
def build_adapter():
    pass
"""
    metadata = _runtime.validate_content(source, "test.py")
    assert metadata["exclude_session_id_patterns"] == ["origin-*"]

    invalid = source.replace('["origin-*"]', '"origin-*"')
    with pytest.raises(
        _runtime.AdapterError,
        match="exclude_session_id_patterns",
    ):
        _runtime.validate_content(invalid, "test.py")


def test_pull_filters_before_fetch_and_preserves_original_total(monkeypatch) -> None:
    adapter = FakeAdapter(["origin-a", "business-1", "rollout-b"])
    descriptor = _descriptor()
    monkeypatch.setattr(service, "describe", lambda *_args: descriptor)
    monkeypatch.setattr(service, "load", lambda *_args, **_kwargs: adapter)

    async def ingest(_session):
        return {"status": "queued", "queued": True}

    result = asyncio.run(
        service.pull_sessions(
            SimpleNamespace(),
            SimpleNamespace(tenant_id="tenant-a"),
            ingest,
            {"max_sessions": 10},
        )
    )

    assert result["total"] == 3
    assert result["counts"]["filtered"] == 2
    assert result["counts"]["queued"] == 1
    assert result["retry_count"] == 2
    assert adapter.fetch_calls == ["business-1"]
    assert result["results"][0] == {
        "session_id": "origin-a",
        "status": "filtered",
        "reason": "excluded_session_id_pattern:origin-*",
    }
    assert adapter.closed is True


def test_explicit_session_id_bypasses_exclusion(monkeypatch) -> None:
    adapter = FakeAdapter(["origin-debug"])
    descriptor = _descriptor()
    monkeypatch.setattr(service, "describe", lambda *_args: descriptor)
    monkeypatch.setattr(service, "load", lambda *_args, **_kwargs: adapter)

    async def ingest(_session):
        return {"status": "queued"}

    result = asyncio.run(
        service.pull_sessions(
            SimpleNamespace(),
            SimpleNamespace(tenant_id="tenant-a"),
            ingest,
            {"session_id": "origin-debug"},
        )
    )

    assert result["counts"]["filtered"] == 0
    assert adapter.fetch_calls == ["origin-debug"]


def test_preview_uses_the_same_exclusion_rules(monkeypatch) -> None:
    adapter = FakeAdapter(["origin-a", "business-1", "rollout-b"])
    descriptor = _descriptor()
    monkeypatch.setattr(_runtime, "describe", lambda *_args: descriptor)
    monkeypatch.setattr(_runtime, "load", lambda *_args, **_kwargs: adapter)

    result = _runtime.preview(SimpleNamespace(), SimpleNamespace(), {})

    assert result["total"] == 3
    assert result["count"] == 1
    assert result["counts"] == {"included": 1, "filtered": 2}
    assert result["sessions"] == [
        {"session_id": "business-1", "title": "business-1"}
    ]
