"""Tests for the Langfuse session ingestion integration.

Covers:
- pure Langfuse trace/session -> teamEvolver session conversion,
- LangfuseClient query-parameter construction, metadata filter JSON, pagination,
  and session-id resolution routing (traces endpoint vs sessions endpoint),
- default-filter + override merging, and
- the pull_sessions orchestration (convert + ingest + summary counts).

The HTTP layer is exercised by intercepting LangfuseClient._get so no network
or live Langfuse deployment is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations import langfuse_pull
from teamEvolver.integrations.langfuse_client import (
    LangfuseClient,
    LangfuseError,
    SessionFilters,
)
from teamEvolver.integrations.langfuse_convert import (
    convert_langfuse_session,
    convert_trace_to_turn,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #
def _trace(trace_id: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
    base = {
        "id": trace_id,
        "sessionId": session_id,
        "timestamp": kwargs.pop("timestamp", "2026-01-01T00:00:00Z"),
        "name": kwargs.pop("name", "agent-run"),
        "userId": kwargs.pop("userId", "user-1"),
        "environment": kwargs.pop("environment", "production"),
        "tags": kwargs.pop("tags", ["agent"]),
        "release": kwargs.pop("release", "v1"),
        "version": kwargs.pop("version", "1.0"),
        "input": kwargs.pop("input", None),
        "output": kwargs.pop("output", None),
        "observations": kwargs.pop("observations", []),
    }
    base.update(kwargs)
    return base


def _generation(**kwargs: Any) -> dict[str, Any]:
    obs = {
        "id": kwargs.pop("id", "obs-1"),
        "type": "GENERATION",
        "startTime": kwargs.pop("startTime", "2026-01-01T00:00:01Z"),
        "model": kwargs.pop("model", "gpt-4o"),
        "usageDetails": kwargs.pop("usageDetails", {"input": 100, "output": 40, "total": 140}),
        "input": kwargs.pop("input", None),
        "output": kwargs.pop("output", None),
        "level": kwargs.pop("level", "DEFAULT"),
    }
    obs.update(kwargs)
    return obs


class _FakeClient(LangfuseClient):
    """LangfuseClient whose HTTP layer is replaced by canned responses."""

    def __init__(self, responses: dict[str, Any]) -> None:
        # Skip the real __init__ credential checks; set the minimal attributes.
        self._base_url = "https://fake/api/public"
        self._auth = ("pk", "sk")
        self._timeout = 5.0
        self._page_limit = 50
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, dict(params or {})))
        for key, value in self._responses.items():
            if path == key or path.startswith(key):
                return value(path, params or {}) if callable(value) else value
        return {"data": [], "meta": {"totalPages": 1}}


# --------------------------------------------------------------------------- #
# Converter                                                                    #
# --------------------------------------------------------------------------- #
def test_convert_trace_to_turn_extracts_text_and_tokens() -> None:
    trace = _trace(
        "t1",
        "s1",
        input={"messages": [{"role": "user", "content": "帮我整理接口流程"}]},
        output="这是整理后的可复用步骤",
        observations=[
            _generation(usageDetails={"input": 120, "output": 30, "total": 150}),
        ],
    )
    turn = convert_trace_to_turn(trace, 1)

    assert turn["turn_num"] == 1
    assert turn["prompt_text"] == "帮我整理接口流程"
    assert turn["response_text"] == "这是整理后的可复用步骤"
    assert turn["metrics"]["input_tokens"] == 120
    assert turn["metrics"]["output_tokens"] == 30
    assert turn["metrics"]["total_tokens"] == 150
    assert turn["metrics"]["api_call_count"] == 1


def test_convert_trace_to_turn_captures_tool_calls_and_results() -> None:
    trace = _trace(
        "t1",
        "s1",
        input={"role": "user", "content": "call the tool"},
        output={
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "terminal", "arguments": {"cmd": "ls"}}}
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "ok"},
            ]
        },
    )
    turn = convert_trace_to_turn(trace, 1)

    assert turn["metrics"]["tool_call_count"] == 1
    assert turn["tool_calls"][0]["function"]["name"] == "terminal"
    # Arguments dicts are serialized to strings for wire-compat with the hub.
    assert turn["tool_calls"][0]["function"]["arguments"] == '{"cmd": "ls"}'
    assert turn["tool_results"][0]["tool_name"] == "terminal"
    assert turn["tool_results"][0]["has_error"] is False


def test_convert_trace_counts_error_and_span_tools() -> None:
    trace = _trace(
        "t1",
        "s1",
        input="do work",
        observations=[
            {"id": "o1", "type": "SPAN", "name": "search", "startTime": "2026-01-01T00:00:01Z"},
            {"id": "o2", "type": "SPAN", "name": "write", "startTime": "2026-01-01T00:00:02Z"},
        ],
    )
    turn = convert_trace_to_turn(trace, 1)
    # No message tool_calls -> non-generation spans are counted as tool activity.
    assert turn["metrics"]["tool_call_count"] == 2


def test_convert_langfuse_session_aggregates_turns_and_attributes() -> None:
    session = {"id": "s1", "createdAt": "2026-01-01T00:00:00Z", "projectId": "proj-1"}
    traces = [
        _trace(
            "t2",
            "s1",
            timestamp="2026-01-01T00:05:00Z",
            input="second turn",
            observations=[_generation(usageDetails={"input": 10, "output": 5, "total": 15})],
        ),
        _trace(
            "t1",
            "s1",
            timestamp="2026-01-01T00:00:00Z",
            input="first turn",
            tags=["agent", "eval"],
            observations=[_generation(usageDetails={"input": 20, "output": 5, "total": 25})],
        ),
    ]
    converted = convert_langfuse_session(session, traces)

    assert converted["session_id"] == "s1"
    assert converted["source"] == "langfuse"
    # Turns are ordered chronologically by trace timestamp.
    assert [t["trace_id"] for t in converted["turns"]] == ["t1", "t2"]
    assert converted["metrics"]["interaction_turns"] == 2
    assert converted["metrics"]["total_tokens"] == 40
    assert converted["metrics"]["input_tokens"] == 30
    assert converted["langfuse"]["project_id"] == "proj-1"
    assert converted["langfuse"]["environment"] == "production"
    assert converted["langfuse"]["user_id"] == "user-1"
    assert set(converted["langfuse"]["tags"]) == {"agent", "eval"}
    assert converted["user_alias"] == "user-1"
    assert converted["model"] == "gpt-4o"


# --------------------------------------------------------------------------- #
# Client: filters, pagination, routing                                         #
# --------------------------------------------------------------------------- #
def test_metadata_filter_json_is_typed_by_value() -> None:
    import json

    raw = LangfuseClient._build_metadata_filter(
        {"customer_tier": "enterprise", "retries": 3, "flagged": True}
    )
    conditions = {c["key"]: c for c in json.loads(raw)}
    assert conditions["customer_tier"]["type"] == "stringObject"
    assert conditions["retries"]["type"] == "numberObject"
    assert conditions["flagged"]["type"] == "booleanObject"
    assert all(c["column"] == "metadata" for c in conditions.values())


def test_iter_sessions_sends_time_and_environment_params() -> None:
    client = _FakeClient(
        {
            "/sessions": {
                "data": [{"id": "s1"}, {"id": "s2"}],
                "meta": {"totalPages": 1, "limit": 50},
            }
        }
    )
    sessions = client.iter_sessions(
        from_timestamp="2026-01-01T00:00:00Z",
        environment=["production", "staging"],
    )
    assert [s["id"] for s in sessions] == ["s1", "s2"]
    path, params = client.calls[0]
    assert path == "/sessions"
    assert params["fromTimestamp"] == "2026-01-01T00:00:00Z"
    assert params["environment"] == ["production", "staging"]


def test_iter_traces_sends_attribute_filters_and_metadata_filter() -> None:
    client = _FakeClient(
        {"/traces": {"data": [{"id": "t1", "sessionId": "s1"}], "meta": {"totalPages": 1}}}
    )
    client.iter_traces(
        user_id="user-1",
        tags=["agent"],
        release="v2",
        version="1.2",
        name="agent-run",
        environment=["production"],
        metadata={"tier": "enterprise"},
    )
    _, params = client.calls[0]
    assert params["userId"] == "user-1"
    assert params["tags"] == ["agent"]
    assert params["release"] == "v2"
    assert params["version"] == "1.2"
    assert params["name"] == "agent-run"
    assert params["environment"] == ["production"]
    assert "tier" in params["filter"]


def test_list_session_ids_uses_traces_when_attribute_filter_present() -> None:
    client = _FakeClient(
        {
            "/traces": {
                "data": [
                    {"id": "t1", "sessionId": "s1"},
                    {"id": "t2", "sessionId": "s1"},
                    {"id": "t3", "sessionId": "s2"},
                ],
                "meta": {"totalPages": 1},
            }
        }
    )
    ids = client.list_session_ids(SessionFilters(user_id="user-1"), max_sessions=10)
    # De-duplicated, first-seen order preserved.
    assert ids == ["s1", "s2"]
    assert client.calls[0][0] == "/traces"


def test_list_session_ids_uses_sessions_without_attribute_filter() -> None:
    client = _FakeClient(
        {"/sessions": {"data": [{"id": "s1"}, {"id": "s2"}], "meta": {"totalPages": 1}}}
    )
    ids = client.list_session_ids(
        SessionFilters(environment=["production"]), max_sessions=10
    )
    assert ids == ["s1", "s2"]
    assert client.calls[0][0] == "/sessions"


def test_list_session_ids_shortcuts_explicit_session_id() -> None:
    client = _FakeClient({})
    ids = client.list_session_ids(SessionFilters(session_id="s-explicit"))
    assert ids == ["s-explicit"]
    assert client.calls == []  # no API call needed


def test_pagination_follows_total_pages() -> None:
    pages = {
        1: {"data": [{"id": "s1"}], "meta": {"totalPages": 2, "limit": 1}},
        2: {"data": [{"id": "s2"}], "meta": {"totalPages": 2, "limit": 1}},
    }
    client = _FakeClient({"/sessions": lambda path, params: pages[params["page"]]})
    sessions = client.iter_sessions()
    assert [s["id"] for s in sessions] == ["s1", "s2"]
    assert len(client.calls) == 2


def test_client_requires_credentials() -> None:
    with pytest.raises(LangfuseError):
        LangfuseClient(host="https://x", public_key="", secret_key="")


# --------------------------------------------------------------------------- #
# Filter merging                                                               #
# --------------------------------------------------------------------------- #
def test_build_filters_merges_defaults_and_overrides() -> None:
    config = TeamEvolverConfig(
        langfuse_default_environment=["production"],
        langfuse_default_user_id="default-user",
        langfuse_default_tags=["agent"],
    )
    # No overrides -> config defaults apply.
    filters = langfuse_pull.build_filters_from_config(config, {})
    assert filters.environment == ["production"]
    assert filters.user_id == "default-user"
    assert filters.tags == ["agent"]

    # Overrides win, including comma-separated string form.
    filters = langfuse_pull.build_filters_from_config(
        config, {"environment": "staging,dev", "user_id": "u-override"}
    )
    assert filters.environment == ["staging", "dev"]
    assert filters.user_id == "u-override"
    assert filters.tags == ["agent"]  # untouched default retained


# --------------------------------------------------------------------------- #
# Pull orchestration                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_pull_sessions_converts_and_ingests(monkeypatch) -> None:
    config = TeamEvolverConfig(langfuse_enabled=True, langfuse_max_sessions=10)

    session_payloads = {
        "s1": {"id": "s1", "traces": [{"id": "t1", "sessionId": "s1"}]},
        "s2": {"id": "s2", "traces": [{"id": "t2", "sessionId": "s2"}]},
    }
    trace_payloads = {
        "t1": _trace("t1", "s1", input="valuable work", observations=[_generation()]),
        "t2": _trace("t2", "s2", input="", observations=[]),  # empty -> skipped as empty
    }

    class _PullFakeClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__({})

        @classmethod
        def from_config(cls, _config):
            return cls()

        def list_session_ids(self, filters, *, max_sessions=0):
            return ["s1", "s2"]

        def fetch_session_with_traces(self, session_id):
            session = session_payloads[session_id]
            traces = [
                trace_payloads[t["id"]] for t in session.get("traces", [])
            ]
            return session, traces

    monkeypatch.setattr(langfuse_pull, "LangfuseClient", _PullFakeClient)

    ingested: list[dict[str, Any]] = []

    async def _ingest(session: dict[str, Any]) -> dict[str, Any]:
        ingested.append(session)
        return {"status": "queued", "queued": True}

    result = await langfuse_pull.pull_sessions(config, _ingest, {}, max_sessions=10)

    assert result["total"] == 2
    assert result["counts"]["queued"] == 1
    assert result["counts"]["empty"] == 1
    # Only the non-empty session was ingested.
    assert len(ingested) == 1
    assert ingested[0]["session_id"] == "s1"
    assert ingested[0]["source"] == "langfuse"


@pytest.mark.anyio
async def test_pull_sessions_requires_enabled() -> None:
    config = TeamEvolverConfig(langfuse_enabled=False)

    async def _ingest(_session: dict[str, Any]) -> dict[str, Any]:
        return {"status": "queued"}

    with pytest.raises(LangfuseError):
        await langfuse_pull.pull_sessions(config, _ingest, {})


def test_config_store_maps_langfuse_tracing_settings(tmp_path) -> None:
    from teamEvolver.config_store import ConfigStore

    store = ConfigStore(config_file=tmp_path / "config.yaml")
    store.set("langfuse.tracing_enabled", True)
    store.set("langfuse.tracing_environment", "local-dev")
    store.set("langfuse.tracing_release", "abc123")
    store.set("langfuse.tracing_sample_rate", 0.25)
    store.set("langfuse.tracing_capture_content", False)

    config = store.to_config()

    assert config.langfuse_tracing_enabled is True
    assert config.langfuse_tracing_environment == "local-dev"
    assert config.langfuse_tracing_release == "abc123"
    assert config.langfuse_tracing_sample_rate == 0.25
    assert config.langfuse_tracing_capture_content is False


def test_langfuse_tracing_runtime_redacts_content() -> None:
    from contextlib import contextmanager

    from teamEvolver.observability.langfuse import (
        LangfuseTracingSettings,
        _LangfuseRuntime,
    )

    class _Observation:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def update(self, **kwargs):
            self.updates.append(kwargs)

    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.observation = _Observation()

        @contextmanager
        def start_as_current_observation(self, **kwargs):
            self.calls.append(kwargs)
            yield self.observation

    runtime = _LangfuseRuntime()
    runtime._settings = LangfuseTracingSettings(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        capture_content=False,
    )
    client = _Client()
    runtime._client = client

    with runtime.observation(
        name="test-generation",
        as_type="generation",
        input={"secret": "prompt"},
        metadata={"operation": "test"},
        model="test-model",
    ) as observation:
        runtime.update(
            observation,
            output={"secret": "response"},
            metadata={"finish_reason": "stop"},
        )

    assert client.calls[0]["input"] == {"redacted": True}
    assert client.calls[0]["metadata"] == {"operation": "test"}
    assert client.observation.updates[0]["output"] == {"redacted": True}
