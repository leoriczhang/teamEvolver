from __future__ import annotations

from session_ingestion.adapters._shared.langfuse_convert import convert_langfuse_session
from session_ingestion.adapters._shared.sf_agent_adapter import (
    extract_mapping,
    iter_json_objects,
    map_session,
    tool_spans,
)
from session_ingestion.adapters.sources import doris


def test_shared_adapter_helpers_preserve_custom_tool_mapping():
    calls, results, skills = tool_spans(
        [
            {
                "id": "tool-1",
                "name": "tool: exec",
                "input": {"path": "skills/demo/run.py"},
                "output": {"content": [{"text": "done"}, {"code": 1}, {"text": ""}]},
            }
        ]
    )

    assert calls[0]["function"]["arguments"] == '{"path": "skills/demo/run.py"}'
    assert results[0]["content"] == 'done\n{"code": 1}'
    assert results[0]["has_error"] is False
    assert skills == ["demo"]


def test_shared_adapter_helper_extracts_json_from_mixed_text():
    source = 'header {"note": "brace } in string"} middle {"sender_id": "42"}'

    assert list(iter_json_objects(source)) == [
        {"note": "brace } in string"},
        {"sender_id": "42"},
    ]


def test_envelope_only_prompt_title_comes_from_nested_query():
    """openapi push payloads keep the question inside the JSON envelope only."""
    trace = {
        "input": """Conversation info (untrusted metadata):
```json
{
  "sender_id": "user_44537c967c46417890205e04aa36a83f",
  "timestamp": "Sun 2026-09-20 18:37 GMT+8"
}
```

{"sender":{"senderId":"01061335","name":"李亚亚","platform":"Win"},"msg":{"type":"text","content":{"query":"给我客群迁移客户清单"}},"userMsgId":40867}""",
        "userId": "user_44537c967c46417890205e04aa36a83f",
    }

    mapping = extract_mapping(trace, [], agent="产品设计")

    assert mapping["title"] == "给我客群迁移客户清单"
    assert mapping["title_source"].endswith(":json-envelope")

    converted = convert_langfuse_session({"id": "s1"}, [trace])
    patch = map_session(converted, {"id": "s1"}, [{"trace": trace, "observations": []}], agent="产品设计")
    converted.update(patch or {})

    assert converted["title"] == "给我客群迁移客户清单"


def test_builtin_conversion_unwraps_embedded_envelope_after_metadata_header():
    """Doris/other adapters rely on the built-in title when no mapper can help."""
    trace = {
        "id": "t1",
        "name": "openclaw-turn",
        "timestamp": "2026-09-20T10:38:27Z",
        "input": """Conversation info (untrusted metadata):
```json
{
  "sender_id": "user_44537c967c46417890205e04aa36a83f",
  "timestamp": "Sun 2026-09-20 18:37 GMT+8"
}
```

{"sender":{"senderId":"01061335"},"msg":{"type":"text","content":{"query":"给我客群迁移客户清单"}},"userMsgId":40867}""",
    }

    converted = convert_langfuse_session({"id": "s1"}, [trace])

    assert converted["title"] == "给我客群迁移客户清单"


def test_fenced_input_prefers_business_employee_and_question():
    trace = {
        "input": """Conversation info (untrusted metadata):
```json
{
  "sender_id": "user_f8c3557647944bc2aed326f590d2090d",
  "timestamp": "Wed 2026-09-02 18:49 GMT+8"
}
```
```json
{"sender_id": "80006722", "name": ""}
```
商圈店铺的产品成本以及利润分析
""",
        "userId": "user_f8c3557647944bc2aed326f590d2090d",
    }

    mapping = extract_mapping(trace, [], agent="产品智能体")

    assert mapping["user_id"] == "80006722"
    assert mapping["title"] == "商圈店铺的产品成本以及利润分析"
    assert mapping["title_source"] == "trace.input:fenced-text"


def test_fenced_input_preserves_complete_prompt_outside_metadata():
    trace = {
        "input": """Conversation info (untrusted metadata):
```json
{"sender_id": "80006722"}
```
第一行问题
第二行补充

[定时任务上下文] 这是系统附加说明。
不得混入用户正文。
""",
    }

    mapping = extract_mapping(trace, [], agent="产品智能体")

    assert mapping["prompt_text"] == "第一行问题\n第二行补充"
    assert mapping["title"] == "第一行问题 第二行补充"


def test_fenced_input_prefers_explicit_employee_over_unit_sender():
    trace = {
        "input": """Conversation info (untrusted metadata):
```json
{"sender_id": "UNIT3_A2_3a4ec7513ba3a0f7c006b69004721b2b1788830725811"}
```
工号<01412388>我要获取拜访助手功能测试专用档案的访前洞察报告
""",
        "userId": "UNIT3_A2_3a4ec7513ba3a0f7c006b69004721b2b1788830725811",
    }

    mapping = extract_mapping(trace, [], agent="销售智能体")

    assert mapping["user_id"] == "01412388"
    assert mapping["user_source"] == "trace.input:user-prompt-prefix"


def test_session_mapping_removes_generated_user_and_trace_name_title():
    trace = {
        "name": "customer-service-voice-analysis-agent",
        "input": "",
        "userId": "cemp-marketing-assistant-agent",
    }
    converted = {
        "title": "customer-service-voice-analysis-agent",
        "user_alias": "cemp-marketing-assistant-agent",
    }

    patch = map_session(
        converted,
        {"id": "doris-trace:1", "user_id": trace["userId"]},
        [{"trace": trace, "observations": []}],
        agent="客户声音分析",
    )

    assert patch == {"user_alias": "anonymous", "title": ""}


def test_sfa2a_uses_recipient_employee_title_and_agent_trace_id():
    trace = {
        "input": {
            "method": "message/send",
            "params": {
                "message": {
                    "metadata": {
                        "senderUserId": "000000",
                        "agentTraceId": "000000_1788227103890",
                    },
                    "parts": [
                        {
                            "text": {
                                "userIds": ["01404723"],
                                "msgParam": {
                                    "title": "机会跟进转化双低人员名单(8月31日)",
                                    "text": "正文",
                                },
                            }
                        }
                    ],
                }
            },
        },
        "userId": "000000",
    }

    mapping = extract_mapping(trace, [], agent="客户智能体-upclaw")

    assert mapping["user_id"] == "01404723"
    assert mapping["title"] == "机会跟进转化双低人员名单(8月31日)"
    assert mapping["trace_id"] == "000000_1788227103890"


def test_observation_fallback_extracts_real_prompt_and_user():
    trace = {"input": "", "userId": "01373024", "name": "POST /intent/dingtalk/stream"}
    observations = [
        {
            "type": "SPAN",
            "name": "harness.intent.runner",
            "input": {
                "traceId": "01373024_1788225058738",
                "metadata": {"senderUserId": "01373024"},
                "messages": [{"role": "user", "content": "635VA\n534VA\n这些网点的上级"}],
            },
        }
    ]

    mapping = extract_mapping(trace, observations, agent="场地规划")

    assert mapping["title"] == "635VA 534VA 这些网点的上级"
    assert mapping["user_id"] == "01373024"
    assert mapping["trace_id"] == "01373024_1788225058738"
    assert mapping["title_source"].startswith("observations[0].input")


def test_pair_messages_and_request_context_are_unwrapped():
    trace = {
        "input": {
            "messages": [
                [
                    "user",
                    "用户问题：打卡时间，什么时候算迟到\n<request_context>usercode: 42367029</request_context>",
                ]
            ]
        },
        "userId": "42367029",
    }

    mapping = extract_mapping(trace, [], agent="销售知识问答")

    assert mapping["title"] == "打卡时间，什么时候算迟到"
    assert mapping["user_id"] == "42367029"


def test_generated_user_is_not_presented_as_business_submitter():
    trace = {
        "input": {"messages": [{"role": "user", "content": "月结卡号5717106059的外呼标签"}]},
        "userId": "00000000",
    }

    mapping = extract_mapping(trace, [], agent="外呼智能体")

    assert mapping["title"] == "月结卡号5717106059的外呼标签"
    assert mapping["user_id"] == ""


def test_doris_synthesizes_session_for_trace_without_upstream_session(monkeypatch):
    monkeypatch.setattr(doris, "resolve_project_id", lambda _value: "project-1")
    monkeypatch.setattr(
        doris,
        "fetch_session_groups_page",
        lambda *_args, **_kwargs: [
            {
                "session_id": "doris-trace:trace-1",
                "timestamp": "2026-09-01 00:45:32",
                "user_id": "097988",
                "name": "agent-call",
                "trace_count": 1,
            }
        ],
    )
    adapter = doris.DorisSourceAdapter(
        None,
        {"project_id": "project-1", "trace_name": "agent-call"},
    )

    ids = adapter.list_session_ids(
        {
            "from_timestamp": "2026-09-01T00:00:00Z",
            "to_timestamp": "2026-09-02T00:00:00Z",
        },
        max_sessions=10,
    )

    assert ids == ["doris-trace:trace-1"]
    assert (
        adapter.preview_sessions(
            {
                "from_timestamp": "2026-09-01T00:00:00Z",
                "to_timestamp": "2026-09-02T00:00:00Z",
            },
            max_sessions=10,
        )[0]["user_id"]
        == "097988"
    )


def test_doris_session_batches_use_keyset_pagination_without_total_limit(monkeypatch):
    calls = []
    first_page = [
        {
            "session_id": f"s-{index:03d}",
            "timestamp": f"2026-09-01 00:{index // 60:02d}:{index % 60:02d}",
            "user_id": "u1",
            "name": "agent-call",
            "trace_count": 1,
        }
        for index in range(100)
    ]
    second_page = [
        {
            "session_id": "s-100",
            "timestamp": "2026-09-01 00:01:40",
            "user_id": "u1",
            "name": "agent-call",
            "trace_count": 1,
        }
    ]

    def fake_page(*_args, **kwargs):
        calls.append(kwargs)
        return second_page if kwargs["after_session_id"] else first_page

    monkeypatch.setattr(doris, "resolve_project_id", lambda _value: "project-1")
    monkeypatch.setattr(doris, "fetch_session_groups_page", fake_page)
    adapter = doris.DorisSourceAdapter(
        None,
        {"project_id": "project-1", "trace_name": "agent-call"},
    )

    batches = list(
        adapter.iter_session_id_batches(
            {
                "from_timestamp": "2026-09-01T00:00:00Z",
                "to_timestamp": "2026-10-01T00:00:00Z",
            },
            batch_size=25,
        )
    )

    assert [len(batch) for batch in batches] == [25, 25, 25, 25, 1]
    assert batches[-1][-1] == "s-100"
    assert calls[1]["after_session_id"] == "s-099"
    assert calls[1]["after_timestamp"] == first_page[-1]["timestamp"]


def test_doris_fetches_synthetic_session_by_trace_id_and_multi_trace_name(monkeypatch):
    queries = []

    def fake_query(sql, timeout=120):
        queries.append((sql, timeout))
        return [
            {
                "id": "trace-1",
                "name": "harness.intent.runner",
                "timestamp": "2026-09-01 00:00:00",
                "user_id": "01373024",
            }
        ]

    monkeypatch.setattr(doris, "resolve_project_id", lambda _value: "project-1")
    monkeypatch.setattr(doris, "doris_query", fake_query)
    monkeypatch.setattr(
        doris,
        "fetch_traces_full_batch",
        lambda ids: [{"trace": {"id": ids[0]}, "observations": []}],
    )
    adapter = doris.DorisSourceAdapter(
        None,
        {
            "project_id": "project-1",
            "trace_name": "harness.intent.runner,POST /v1/chat/completions",
        },
    )

    session, traces = adapter.fetch_session("doris-trace:trace-1")

    assert "id = 'trace-1'" in queries[0][0]
    assert "name IN ('harness.intent.runner','POST /v1/chat/completions')" in queries[0][0]
    assert session["id"] == "doris-trace:trace-1"
    assert traces[0]["trace"]["id"] == "trace-1"
