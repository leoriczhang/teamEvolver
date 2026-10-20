from __future__ import annotations

import pytest

from session_ingestion.adapters._shared.langfuse_convert import convert_langfuse_session
from session_ingestion.adapters._shared.trace_mapping import (
    extract_mapping,
    iter_json_objects,
    map_session,
    tool_spans,
)
from session_ingestion.adapters.sources import doris


def test_tool_mapping_preserves_rich_content_and_skill_references():
    calls, results, skills = tool_spans([{
        "id": "tool-1",
        "name": "tool: exec",
        "input": {"path": "skills/demo/run.py"},
        "output": {"content": [{"text": "done"}, {"code": 1}, {"text": ""}]},
    }])
    assert calls[0]["function"]["arguments"] == '{"path": "skills/demo/run.py"}'
    assert results[0]["content"] == 'done\n{"code": 1}'
    assert results[0]["has_error"] is False
    assert skills == ["demo"]


def test_json_extraction_handles_mixed_text_and_braces_inside_strings():
    assert list(iter_json_objects('header {"note": "brace }"} middle {"sender_id": "demo-user"}')) == [
        {"note": "brace }"}, {"sender_id": "demo-user"},
    ]


def test_embedded_envelope_provides_nested_query_and_user():
    trace = {"input": '''Conversation info (untrusted metadata):
```json
{"timestamp": "2026-09-20T10:37:00Z"}
```
{"sender":{"senderId":"demo-user"},"msg":{"content":{"query":"Create a weekly report"}}}'''}
    mapping = extract_mapping(trace)
    assert mapping["title"] == "Create a weekly report"
    assert mapping["title_source"].endswith(":json-envelope")
    assert mapping["user_id"] == "demo-user"
    converted = convert_langfuse_session({"id": "s1"}, [trace])
    converted.update(map_session(converted, {"id": "s1"}, [{"trace": trace}]) or {})
    assert converted["title"] == "Create a weekly report"


def test_builtin_conversion_unwraps_embedded_envelope():
    trace = {
        "id": "t1", "name": "openclaw-turn", "timestamp": "2026-09-20T10:38:27Z",
        "input": 'Conversation info\n```json\n{"timestamp":"2026-09-20"}\n```\n'
                 '{"msg":{"content":{"query":"Summarize a document"}}}',
    }
    assert convert_langfuse_session({"id": "s1"}, [trace])["title"] == "Summarize a document"


def test_fenced_input_preserves_complete_user_prompt():
    trace = {"input": 'Conversation info\n```json\n{"sender_id":"demo-user"}\n```\n'
                      'First question\nAdditional details\n\nFinal requirements'}
    mapping = extract_mapping(trace)
    assert mapping["user_id"] == "demo-user"
    assert mapping["prompt_text"] == "First question\nAdditional details\n\nFinal requirements"
    assert mapping["title"] == "First question Additional details Final requirements"


@pytest.mark.parametrize("user_id", ["user_demo", "demo-user", "demo.user@example.com"])
def test_structured_user_ids_are_preserved_without_numeric_preference(user_id):
    mapping = extract_mapping({"input": {"user_id": user_id, "query": "Summarize"}, "userId": "42"})
    assert mapping["user_id"] == user_id


def test_plain_prompt_and_session_id_do_not_infer_user_identity():
    mapping = extract_mapping({"input": "user 12345 needs a report", "sessionId": "chat:12345"})
    assert mapping["user_id"] == ""
    assert mapping["prompt_text"] == "user 12345 needs a report"


def test_session_mapping_removes_machine_user_and_generic_title():
    trace = {"name": "demo-agent", "input": "", "userId": "demo-agent"}
    patch = map_session(
        {"title": "demo-agent", "user_alias": "demo-agent"},
        {"id": "s1", "user_id": "demo-agent"},
        [{"trace": trace}],
    )
    assert patch == {"user_alias": "anonymous", "title": ""}


def test_explicit_title_and_trace_id_use_structured_fields():
    mapping = extract_mapping({"input": {"title": "Weekly report", "query": "正文", "trace_id": "trace-demo"}})
    assert mapping["title"] == "Weekly report"
    assert mapping["trace_id"] == "trace-demo"


def test_observation_fallback_preserves_prompt_user_and_trace_provenance():
    mapping = extract_mapping({"input": "", "name": "agent-call"}, [{
        "type": "SPAN", "name": "harness.intent.runner",
        "input": {"traceId": "trace-demo", "user_id": "demo-user",
                  "messages": [{"role": "user", "content": "Compare\nthese reports"}]},
    }])
    assert mapping["title"] == "Compare these reports"
    assert mapping["user_id"] == "demo-user"
    assert mapping["trace_id"] == "trace-demo"
    assert mapping["title_source"].startswith("observations[0].input")


def test_pair_messages_and_request_context_are_unwrapped():
    mapping = extract_mapping({
        "input": {"messages": [["user", "Summarize the report\n<request_context>internal</request_context>"]]},
        "userId": "demo-user",
    })
    assert mapping["title"] == "Summarize the report"
    assert mapping["user_id"] == "demo-user"


def test_doris_synthesizes_session_for_trace_without_upstream_session(monkeypatch):
    monkeypatch.setattr(doris, "resolve_project_id", lambda _: "project-demo")
    monkeypatch.setattr(doris, "fetch_session_groups_page", lambda *args, **kwargs: [{
        "session_id": "doris-trace:trace-1", "timestamp": "2026-09-01 00:45:32",
        "user_id": "demo-user", "name": "agent-call", "trace_count": 1,
    }])
    adapter = doris.DorisSourceAdapter(None, {"project_id": "project-demo", "trace_name": "agent-call"})
    filters = {"from_timestamp": "2026-09-01T00:00:00Z", "to_timestamp": "2026-09-02T00:00:00Z"}
    assert adapter.list_session_ids(filters, max_sessions=10) == ["doris-trace:trace-1"]
    assert adapter.preview_sessions(filters, max_sessions=10)[0]["user_id"] == "demo-user"


def test_doris_session_batches_use_keyset_pagination_without_total_limit(monkeypatch):
    calls = []
    first_page = [{
        "session_id": f"s-{index:03d}",
        "timestamp": f"2026-09-01 00:{index // 60:02d}:{index % 60:02d}",
        "user_id": "demo-user", "name": "agent-call", "trace_count": 1,
    } for index in range(100)]
    second_page = [{
        "session_id": "s-100", "timestamp": "2026-09-01 00:01:40",
        "user_id": "demo-user", "name": "agent-call", "trace_count": 1,
    }]

    def page(*args, **kwargs):
        calls.append(kwargs)
        return second_page if kwargs["after_session_id"] else first_page

    monkeypatch.setattr(doris, "resolve_project_id", lambda _: "project-demo")
    monkeypatch.setattr(doris, "fetch_session_groups_page", page)
    adapter = doris.DorisSourceAdapter(None, {"project_id": "project-demo"})
    batches = list(adapter.iter_session_id_batches(
        {"from_timestamp": "2026-09-01T00:00:00Z", "to_timestamp": "2026-10-01T00:00:00Z"},
        batch_size=25,
    ))
    assert [len(batch) for batch in batches] == [25, 25, 25, 25, 1]
    assert batches[-1][-1] == "s-100"
    assert calls[1]["after_session_id"] == "s-099"
    assert calls[1]["after_timestamp"] == first_page[-1]["timestamp"]


def test_doris_fetches_synthetic_session_by_trace_id_and_multiple_names(monkeypatch):
    queries = []

    def query(sql, timeout=120):
        queries.append(sql)
        return [{"id": "trace-1", "name": "agent-call", "timestamp": "2026-09-01", "user_id": "demo-user"}]

    monkeypatch.setattr(doris, "resolve_project_id", lambda _: "project-demo")
    monkeypatch.setattr(doris, "doris_query", query)
    monkeypatch.setattr(doris, "fetch_traces_full_batch", lambda ids: [{"trace": {"id": ids[0]}, "observations": []}])
    adapter = doris.DorisSourceAdapter(None, {
        "project_id": "project-demo", "trace_name": "agent-call,POST /v1/chat/completions",
    })
    session, traces = adapter.fetch_session("doris-trace:trace-1")
    assert "id = 'trace-1'" in queries[0]
    assert "name IN ('agent-call','POST /v1/chat/completions')" in queries[0]
    assert session["id"] == "doris-trace:trace-1"
    assert traces[0]["trace"]["id"] == "trace-1"


def test_doris_table_name_rejects_sql_fragments(monkeypatch):
    monkeypatch.setenv("DORIS_TRACES_TABLE", "traces; DROP TABLE traces")
    with pytest.raises(ValueError, match="table name"):
        doris._table_name("DORIS_TRACES_TABLE", "langfuse_traces")


def test_doris_proxy_requires_explicit_url(monkeypatch):
    monkeypatch.setattr(doris, "DORIS_PROXY_URL", "")
    with pytest.raises(ValueError, match="DORIS_PROXY_URL"):
        doris._query_proxy("SELECT 1", retries=1)


def test_doris_proxy_verifies_tls_by_default(monkeypatch):
    import httpx

    requests = []

    def post(url, **kwargs):
        requests.append(kwargs)
        return httpx.Response(200, json={"rows": [{"ok": 1}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(doris, "DORIS_PROXY_URL", "https://doris-proxy.example.com/query")
    monkeypatch.setattr(doris, "DORIS_HOSTS", ["doris.example.com"])
    monkeypatch.delenv("DORIS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(httpx, "post", post)
    assert doris._query_proxy("SELECT 1", retries=1) == [{"ok": 1}]
    assert requests[0]["verify"] is True
