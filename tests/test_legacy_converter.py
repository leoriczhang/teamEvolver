from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations.legacy_converter import (
    LegacyConverterSource,
    compile_converter,
    conversion_to_turn,
    inspect_converter,
)
from teamEvolver.integrations import legacy_parser
from teamEvolver.integrations.langfuse_pull import pull_sessions, preview_sessions

DEFAULT_CODE = (
    "from core.langfuse_client import langfuse_to_template\ndef convert(raw):\n    return langfuse_to_template(raw)\n"
)
OLD_ROOT = Path(__file__).resolve().parents[2] / "inc-aiagent-core-skill-opt"
RAW = {
    "trace": {
        "id": "trace-1",
        "sessionId": "cid-session",
        "name": "agent-call",
        "input": "question",
        "output": "answer",
        "timestamp": "2026-09-01T00:00:00Z",
    },
    "observations": [
        {
            "id": "tool-1",
            "name": "tool: execute_skill",
            "type": "SPAN",
            "startTime": "2026-09-01T00:00:01Z",
            "input": {"skill_name": "demo"},
            "output": {"content": "failed", "isError": True},
        }
    ],
}


def test_default_converter_preserves_contract():
    converted = compile_converter(DEFAULT_CODE).convert(RAW)
    assert converted == legacy_parser.langfuse_to_template(RAW)
    turn = conversion_to_turn(converted, {**RAW["trace"], "observations": RAW["observations"]}, {})
    assert turn["prompt_text"] == "question"
    assert turn["response_text"] == "answer"
    assert turn["legacy_conversions"] == converted["conversions"]


@pytest.mark.parametrize("source", sorted((OLD_ROOT / "converters").glob("*.py")), ids=lambda p: p.stem)
def test_every_supplied_converter_compiles_and_converts(source):
    code = source.read_text(encoding="utf-8")
    assert inspect_converter(code)["status"] == "compatible"
    result = compile_converter(code).convert(RAW)
    assert isinstance(result["conversions"], list)


@pytest.mark.skipif(not OLD_ROOT.exists(), reason="original customer source not included")
def test_vendored_parser_matches_original():
    spec = importlib.util.spec_from_file_location("original_parser", OLD_ROOT / "core/langfuse_client.py")
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    for observations in (
        [],
        RAW["observations"],
        [
            {"id": "summary", "name": "agent turn summary", "input": "q"},
            {"id": "loop", "name": "agent loop 1", "parentObservationId": "summary"},
            {
                "id": "llm",
                "name": "llm",
                "type": "GENERATION",
                "parentObservationId": "loop",
                "output": {"content": [{"type": "text", "text": "a"}]},
            },
        ],
    ):
        raw = {**RAW, "observations": observations}
        assert legacy_parser.langfuse_to_template(raw) == original.langfuse_to_template(raw)


def test_imports_do_not_pollute_process_core_namespace():
    import sys

    before = sys.modules.get("core")
    compile_converter(DEFAULT_CODE)
    assert sys.modules.get("core") is before
    bad = inspect_converter("from core.engine import PROJECT_NAME\ndef convert(raw): return raw")
    assert bad["status"] == "blocked"
    with pytest.raises(ValueError):
        compile_converter("def convert(raw):\n    !invalid")


def test_a2a_converter_keeps_real_empty_reply():
    source = OLD_ROOT / "converters/cmpgdm3ls01cgxa06aj2u6kqi.py"
    if not source.exists():
        pytest.skip("customer source not included")
    raw = {
        "trace": {"id": "a2a", "output": "must not be used"},
        "observations": [
            {"name": "a2a.send", "type": "SPAN", "input": {"messages": [{"role": "user", "content": "real question"}]}},
            {
                "name": "llm",
                "type": "GENERATION",
                "output": {"content": [{"type": "text", "text": "# memory private internal text"}]},
            },
        ],
    }
    conv = compile_converter(source.read_text()).convert(raw)
    turn = conversion_to_turn(conv, raw["trace"], {"response_text": "bad default"})
    assert turn["prompt_text"] == "real question"
    assert turn["response_text"] == ""


class FakeClient:
    def iter_traces(self, **kwargs):
        return [RAW["trace"]]

    def get_trace(self, tid):
        return {**RAW["trace"], "observations": RAW["observations"]}

    def close(self):
        pass


def test_legacy_pull_uses_same_filtered_ids_as_preview(monkeypatch):
    monkeypatch.setattr(LegacyConverterSource, "_client", lambda self: FakeClient())
    config = TeamEvolverConfig(
        langfuse_enabled=True, datasource_type="skillopt", datasource_legacy_converter_code=DEFAULT_CODE
    )
    preview = preview_sessions(config, max_sessions=10)
    ingested = []

    async def ingest(session):
        ingested.append(session)
        return {"status": "queued", "queued": True}

    result = asyncio.run(pull_sessions(config, ingest, max_sessions=10))
    assert result["counts"]["queued"] == 1
    assert preview["sessions"][0]["session_id"] == ingested[0]["session_id"]
    assert ingested[0]["turns"][0]["legacy_conversions"]


def test_broken_converter_does_not_fall_back_to_native(monkeypatch):
    monkeypatch.setattr(LegacyConverterSource, "_client", lambda self: FakeClient())
    config = TeamEvolverConfig(
        langfuse_enabled=True,
        datasource_type="skillopt",
        datasource_legacy_converter_code="def convert(raw): raise ValueError('broken converter')",
    )

    async def ingest(session):
        pytest.fail("invalid conversion reached ingest")

    result = asyncio.run(pull_sessions(config, ingest, max_sessions=10))
    assert result["counts"]["error"] == 1


def test_json_body_limit_stops_reading_early(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from teamEvolver.proxy.routes import _read_limited_json_body

    monkeypatch.setenv("TEAMEVOLVER_MAX_SESSION_BODY_BYTES", "1024")
    reads = []

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b"x" * 700, "more_body": True}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read_limited_json_body(request))
    assert exc.value.status_code == 413
    assert len(reads) == 2


def test_tool_evidence_and_converter_revision_affect_dedup():
    from copy import deepcopy
    from teamEvolver.session_store import SessionStore
    from teamEvolver.storage import InMemoryObjectStore

    store = SessionStore(InMemoryObjectStore())
    session = {
        "session_id": "same",
        "legacy_converter": {"sha256": "v1"},
        "turns": [{"prompt_text": "q", "response_text": "a", "tool_results": [{"content": "old"}]}],
    }
    store.save_queued(session)
    assert store.duplicate_of_processed(session)
    changed = deepcopy(session)
    changed["turns"][0]["tool_results"][0]["content"] = "new error"
    assert not store.duplicate_of_processed(changed)
    changed = deepcopy(session)
    changed["legacy_converter"]["sha256"] = "v2"
    assert not store.duplicate_of_processed(changed)


def test_consumed_archive_preserves_upstream_lineage(tmp_path):
    import json
    from teamEvolver.evolve import EvolveServer, EvolveServerConfig
    from teamEvolver.session_store import SessionStore

    server = EvolveServer(EvolveServerConfig(), mock=True, mock_root=str(tmp_path))
    session = {
        "session_id": "lineage",
        "langfuse": {"trace_ids": ["trace-1"]},
        "legacy_converter": {"sha256": "v1"},
        "turns": [
            {
                "trace_id": "trace-1",
                "prompt_text": "q",
                "response_text": "a",
                "legacy_conversions": [{"role": "user", "content": []}],
            }
        ],
    }
    server._archive_sessions([session])
    archived = json.loads(server._bucket.get_object("session_archive/lineage.json").read())
    assert archived["langfuse"]["trace_ids"] == ["trace-1"]
    assert archived["turns"][0]["trace_id"] == "trace-1"
    assert archived["turns"][0]["legacy_conversions"]
    assert SessionStore(server._bucket).duplicate_of_processed(session)


def test_multiple_calls_keep_observation_ids_without_double_counting():
    trace = {
        "id": "multi",
        "observations": [
            {"id": "a", "name": "tool: alpha"},
            {"id": "b", "name": "tool: beta"},
        ],
    }
    conv = {
        "conversions": [
            {"role": "gpt", "content": [{"type": "toolCall", "name": "alpha"}, {"type": "toolCall", "name": "beta"}]},
            {"role": "tool_result", "observationId": "a", "content": [{"type": "text", "value": "A"}]},
            {"role": "tool_result", "observationId": "b", "content": [{"type": "text", "value": "B"}]},
        ]
    }
    turn = conversion_to_turn(conv, trace, {})
    assert [call["id"] for call in turn["tool_calls"]] == ["a", "b"]
    assert turn["metrics"]["tool_call_count"] == 2
