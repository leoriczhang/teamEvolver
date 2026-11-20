"""A shared project keeps one selection through preview and Worker ingestion."""
from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest

from session_ingestion.adapters._runtime import AdapterError, validate_content
from session_ingestion.adapters._shared.langfuse_client import LangfuseClient, LangfuseError
from session_ingestion.adapters.sources import langfuse as source_module
from session_ingestion.adapters.sources.langfuse import LangfuseSource
from session_ingestion.adapters.worker import StreamingAdapter

START = "2026-11-20T00:00:00Z"
END = "2026-11-21T00:00:00Z"
FILTERS = {"from_timestamp": START, "to_timestamp": END}
NAMES = ["customer-turn", "task-turn"]


def event(identity, session, name, agent, minute, *, nested=False):
    metadata = {"teamevolver_execution": {"agent_id": agent}} if nested else {"agent_id": agent}
    return {"id": identity, "projectId": "project-main", "sessionId": session, "name": name,
            "timestamp": f"2026-11-20T00:{minute:02d}:00Z", "userId": "business-user", "metadata": metadata,
            "tags": ["business"], "environment": "production", "version": "v1", "release": "release-1",
            "input": "Perform business task", "output": "Actual reply", "observations": [{
                "id": "call-" + identity, "type": "GENERATION", "input": {
                    "messages": [{"role": "system", "content": "Business rule"},
                                 {"role": "user", "content": "Perform business task"}]}}]}


class Project:
    def __init__(self):
        self.traces = [event("t-foreign", "foreign", "telemetry", "other", 0),
                       event("t-customer", "customer", "customer-turn", "customer-agent", 1),
                       event("t-a", "mixed", "customer-turn", "customer-agent", 2),
                       event("t-b", "mixed", "task-turn", "task-agent", 3, nested=True),
                       event("t-task", "task", "task-turn", "task-agent", 4)]
        self.requests = []

    def respond(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/public")
        if path == "/traces":
            values = self.traces
            params = request.url.params
            if params.get("name"):
                values = [trace for trace in values if trace["name"] == params["name"]]
            if params.get("sessionId"):
                values = [trace for trace in values if trace["sessionId"] == params["sessionId"]]
            if params.get("fromTimestamp"):
                values = [trace for trace in values if trace["timestamp"] >= params["fromTimestamp"]]
            if params.get("toTimestamp"):
                values = [trace for trace in values if trace["timestamp"] <= params["toTimestamp"]]
            values = sorted(values, key=lambda trace: trace["timestamp"],
                            reverse=params.get("orderBy", "").endswith("desc"))
            page, limit = int(params.get("page", 1)), int(params.get("limit", 2))
            return httpx.Response(200, json={"data": values[(page - 1) * limit:page * limit],
                                             "meta": {"totalPages": max(1, (len(values) + limit - 1) // limit)}})
        if path.startswith("/sessions/"):
            sid = path.removeprefix("/sessions/")
            return httpx.Response(200, json={"id": sid, "projectId": "project-main", "traces": [
                {key: value for key, value in trace.items() if key != "observations"}
                for trace in self.traces if trace["sessionId"] == sid]})
        if path.startswith("/traces/"):
            identity = path.removeprefix("/traces/")
            return httpx.Response(200, json=next(trace for trace in self.traces if trace["id"] == identity))
        pytest.fail(f"Unexpected request {path}")


@pytest.fixture
def project(monkeypatch):
    value = Project()

    def client(host, public_key, secret_key):
        result = LangfuseClient(host, public_key, secret_key, page_limit=2)
        result._http = httpx.Client(transport=httpx.MockTransport(value.respond))
        return result

    monkeypatch.setattr(source_module, "LangfuseClient", client)
    monkeypatch.setenv("LANGFUSE_PULL_HOST", "https://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PULL_PUBLIC_KEY", "public-example")
    monkeypatch.setenv("LANGFUSE_PULL_SECRET_KEY", "secret-example")
    return value


def source(**options):
    return LangfuseSource(None, {"trace_names": NAMES, **options})


def test_multiple_explicit_names_share_pagination_and_deduplicate_mixed_sessions(project):
    adapter = source()
    try:
        assert adapter.list_session_ids(FILTERS, max_sessions=10) == ["customer", "mixed", "task"]
        assert list(adapter.iter_session_id_batches(FILTERS, batch_size=2)) == [["customer", "mixed"], ["task"]]
        _, traces = adapter.fetch_session("mixed")
        assert [item["trace"]["id"] for item in traces] == ["t-a", "t-b"]
        preview = adapter.preview_sessions(FILTERS, max_sessions=10)
        assert [row["session_id"] for row in preview] == ["task", "mixed", "customer"]
        assert next(row for row in preview if row["session_id"] == "mixed")["trace_count"] == 2
        trace_requests = [request for request in project.requests if request.url.path.endswith("/traces")]
        assert all(not request.url.params.get("name") for request in trace_requests)
        assert any(request.url.params.get("page") == "3" for request in trace_requests)
    finally:
        adapter.close()


@pytest.mark.parametrize("agent,expected", [("customer-agent", ["customer", "mixed"]),
                                           ("task-agent", ["mixed", "task"])])
def test_agent_filter_applies_to_discovery_preview_and_every_detail_trace(project, agent, expected):
    adapter = source()
    try:
        selected = {**FILTERS, "agent_id": agent}
        assert adapter.list_session_ids(selected, max_sessions=10) == expected
        preview = adapter.preview_sessions(selected, max_sessions=10)
        assert {row["session_id"] for row in preview} == set(expected)
        assert all(row["trace_count"] == 1 for row in preview)
        _, traces = adapter.fetch_session("mixed")
        assert [item["trace"]["id"] for item in traces] == (["t-a"] if agent == "customer-agent" else ["t-b"])
    finally:
        adapter.close()


def test_explicit_session_id_cannot_bypass_trace_name_or_agent_selection(project):
    adapter = source()
    try:
        selected = {**FILTERS, "session_id": "customer", "agent_id": "task-agent"}
        assert adapter.list_session_ids(selected, max_sessions=10) == []
        assert list(adapter.iter_session_id_batches(selected)) == []
        assert adapter.preview_sessions(selected, max_sessions=10) == []
        assert adapter.fetch_session("customer")[1] == []
        assert adapter.preview_sessions({**FILTERS, "session_id": "foreign"}, max_sessions=10) == []
        mixed = adapter.preview_sessions({**selected, "session_id": "mixed"}, max_sessions=10)
        assert len(mixed) == 1 and mixed[0]["trace_count"] == 1
    finally:
        adapter.close()


def test_legacy_single_name_still_selects_only_its_real_traces(project):
    adapter = LangfuseSource(None, {"trace_name": "customer-turn"})
    try:
        assert adapter.list_session_ids(FILTERS, max_sessions=10) == ["customer", "mixed"]
        assert list(adapter.iter_session_id_batches(FILTERS)) == [["customer", "mixed"]]
        assert [item["trace"]["id"] for item in adapter.fetch_session("mixed")[1]] == ["t-a"]
        calls = [request for request in project.requests if request.url.path.endswith("/traces")]
        assert all(request.url.params.get("name") == "customer-turn" for request in calls)
    finally:
        adapter.close()


def test_agent_identity_conflicts_do_not_match_either_agent(project):
    conflicted = copy.deepcopy(project.traces[2])
    conflicted.update(id="conflicted", sessionId="conflicted")
    conflicted["metadata"]["teamevolver_execution"] = {"agent_id": "task-agent"}
    project.traces.append(conflicted)
    adapter = source()
    try:
        for agent in ("customer-agent", "task-agent"):
            assert "conflicted" not in adapter.list_session_ids({**FILTERS, "agent_id": agent}, max_sessions=10)
    finally:
        adapter.close()


def test_detail_rejects_an_upstream_response_for_a_different_session(project, monkeypatch):
    adapter = source()
    client = adapter._client()
    monkeypatch.setattr(client, "get_trace", lambda identity: {**project.traces[2], "sessionId": "another-session"})
    try:
        with pytest.raises(LangfuseError, match="another Session"):
            adapter.fetch_session("mixed")
    finally:
        adapter.close()


def adapter_code(names):
    metadata = {"label": "Shared project", "provider": "langfuse", "enabled": True,
                "trace_names": names, "supported_filters": ["from_timestamp", "to_timestamp", "agent_id"],
                "required_filters": ["from_timestamp", "to_timestamp"]}
    return ("from session_ingestion.adapters._shared.builders import build_langfuse_adapter\n"
            f"SOURCE = {metadata!r}\n"
            "def build_adapter():\n    return build_langfuse_adapter(SOURCE)\n")


@pytest.mark.asyncio
async def test_worker_stream_and_frozen_adapter_source_keep_each_registered_name_selection(project):
    old = StreamingAdapter(adapter_code(["customer-turn"]), "shared.py")
    current = StreamingAdapter(adapter_code(NAMES), "shared.py")
    old_items = [item async for item in old.iter_sessions(FILTERS)]
    current_items = [item async for item in current.iter_sessions(FILTERS)]
    assert [item["external_id"] for item in old_items] == ["customer", "mixed"]
    assert [item["external_id"] for item in current_items] == ["customer", "mixed", "task"]
    scoped = [item async for item in current.iter_sessions({**FILTERS, "agent_id": "task-agent"})]
    assert [item["external_id"] for item in scoped] == ["mixed", "task"]
    assert all([turn["source_trace_id"] for turn in item["session"]["turns"]] in (["t-b"], ["t-task"])
               for item in scoped)
    assert all(item["session"]["source_identity"]["identity_verified"] for item in current_items)


@pytest.mark.parametrize("names", [[], "customer-turn", [""], ["valid", 3], ["x"] * 33])
def test_invalid_trace_name_configuration_fails_during_source_registration(names):
    with pytest.raises(AdapterError):
        validate_content(adapter_code(names), "shared.py")


def test_shared_adapter_example_is_versionable_and_exposes_agent_filter():
    path = Path(__file__).resolve().parents[2] / "session_ingestion/adapters/example-shared-langfuse.py"
    metadata = validate_content(path.read_text(), path.name)
    assert metadata["trace_names"] == ["customer-agent-turn", "task-judge-turn"]
    assert "agent_id" in metadata["supported_filters"] and metadata["enabled"] is False


@pytest.mark.asyncio
async def test_same_registered_shared_adapter_accepts_different_frozen_trace_options(project):
    path = Path(__file__).resolve().parents[2] / "session_ingestion/adapters/example-shared-langfuse.py"
    code = path.read_text().replace('"enabled": False', '"enabled": True')
    a = StreamingAdapter(code, path.name, config={"trace_names": ["customer-turn"]})
    b = StreamingAdapter(code, path.name, config={"trace_names": ["task-turn"]})
    first = [item async for item in a.iter_sessions(FILTERS)]
    second = [item async for item in b.iter_sessions(FILTERS)]
    assert [item["external_id"] for item in first] == ["customer", "mixed"]
    assert [item["external_id"] for item in second] == ["mixed", "task"]
    assert first[1]["session"]["turns"][0]["source_trace_id"] == "t-a"
    assert second[0]["session"]["turns"][0]["source_trace_id"] == "t-b"


@pytest.mark.parametrize("names", [None, [], "", ["  "], ["a"] * 33])
def test_invalid_named_connection_trace_selection_is_rejected_before_acceptance(names):
    from session_ingestion.adapters._shared.connection_settings import normalize_settings

    with pytest.raises(ValueError):
        normalize_settings({"options": {"trace_names": names}},
                           {"provider": "langfuse", "connection_options": ["trace_names"]})
