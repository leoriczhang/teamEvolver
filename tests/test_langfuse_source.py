"""LangfuseSource 契约测试：与 DorisSourceAdapter 同构的直连数据源。

覆盖：env 回退、fetch_session 信封、convert_session 钩子、preview_sessions、
health、以及租户 adapter 文件（产品设计-langfuse.py）经 _runtime 校验的接线。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from session_ingestion.adapters import _runtime
from session_ingestion.adapters.sources import langfuse as langfuse_source_module
from session_ingestion.adapters.sources.langfuse import LangfuseSource

ENV_KEYS = (
    "LANGFUSE_PULL_HOST",
    "LANGFUSE_PULL_PUBLIC_KEY",
    "LANGFUSE_PULL_SECRET_KEY",
)

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "session_ingestion" / "adapters"
PILOT_FILE = ADAPTER_DIR / "产品设计-langfuse.py"


def _clear_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_env(monkeypatch, host="https://lf.example", pk="pk-1", sk="sk-1"):
    monkeypatch.setenv("LANGFUSE_PULL_HOST", host)
    monkeypatch.setenv("LANGFUSE_PULL_PUBLIC_KEY", pk)
    monkeypatch.setenv("LANGFUSE_PULL_SECRET_KEY", sk)


def _trace(trace_id, session_id, timestamp, *, name="openclaw-turn", input_text="你好", output_text="答复", user_id="u1", observations=None):
    return {
        "id": trace_id,
        "sessionId": session_id,
        "name": name,
        "timestamp": timestamp,
        "userId": user_id,
        "input": input_text,
        "output": output_text,
        "observations": observations or [],
    }


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, host, public_key, secret_key, *, timeout=30.0, page_limit=50):
        self.host = host
        self.public_key = public_key
        self.secret_key = secret_key
        self.closed = False
        self.calls: list[tuple[str, dict]] = []
        FakeClient.instances.append(self)

    def health(self):
        self.calls.append(("health", {}))
        return {"ok": True, "host": self.host, "total_sessions": 3}

    def list_session_ids(self, filters, *, max_sessions=0):
        self.calls.append(("list_session_ids", {"filters": filters.as_dict(), "max_sessions": max_sessions}))
        return [filters.session_id] if filters.session_id else ["s1", "s2"]

    def fetch_session_with_traces(self, session_id, *, trace_name=""):
        self.calls.append(("fetch_session_with_traces", {"session_id": session_id, "trace_name": trace_name}))
        flat = copy.deepcopy(_TRACES_BY_SESSION.get(session_id, []))
        return {"id": session_id, "createdAt": "2026-09-19T10:00:00Z"}, flat

    def iter_traces(self, *, name="", user_id="", from_timestamp="", to_timestamp="", order_by="timestamp.asc", max_items=0, **kwargs):
        self.calls.append(("iter_traces", {"name": name, "user_id": user_id, "from": from_timestamp, "to": to_timestamp, "max_items": max_items}))
        rows = _TRACES_BY_SESSION["s1"] + _TRACES_BY_SESSION["s2"]
        if user_id:
            rows = [t for t in rows if t.get("userId") == user_id]
        return copy.deepcopy(rows[:max_items or None])

    def close(self):
        self.closed = True


_TRACES_BY_SESSION = {
    "s1": [
        _trace("t1", "s1", "2026-09-19T10:00:00Z"),
        _trace("t2", "s1", "2026-09-19T10:05:00Z"),
    ],
    "s2": [
        _trace("t3", "s2", "2026-09-18T09:00:00Z", user_id="u2", input_text="Conversation info (untrusted metadata):\n```json\n{\"sender_id\": \"80006722\"}\n```\n真实问题标题"),
    ],
}


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(langfuse_source_module, "LangfuseClient", FakeClient)
    yield
    FakeClient.instances = []


# --------------------------------------------------------------------- #
# env 回退与配置解析                                                     #
# --------------------------------------------------------------------- #

def test_env_fallback_when_options_empty(monkeypatch):
    _set_env(monkeypatch, host="https://lf-env.example")
    source = LangfuseSource(None, {})
    assert source.host == "https://lf-env.example"
    assert source.public_key == "pk-1"
    assert source.secret_key == "sk-1"


def test_options_override_env(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {
        "host": "https://lf-opt.example",
        "public_key": "pk-opt",
        "secret_key": "sk-opt",
        "trace_name": "openclaw-turn",
    })
    assert source.host == "https://lf-opt.example"
    assert source.public_key == "pk-opt"
    assert source.secret_key == "sk-opt"
    assert source.trace_name == "openclaw-turn"


def test_unconfigured_health_reports_error(monkeypatch):
    _clear_env(monkeypatch)
    source = LangfuseSource(None, {"trace_name": "openclaw-turn"})
    result = source.health()
    assert result["ok"] is False
    assert "LANGFUSE_PULL_HOST" in result["error"]


def test_unconfigured_client_raises(monkeypatch):
    _clear_env(monkeypatch)
    source = LangfuseSource(None, {})
    with pytest.raises(Exception):
        source.list_session_ids({}, max_sessions=10)


# --------------------------------------------------------------------- #
# fetch_session 信封（Doris 同构）                                        #
# --------------------------------------------------------------------- #

def test_fetch_session_returns_doris_envelope(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {"trace_name": "openclaw-turn"})
    session, traces = source.fetch_session("s1")
    assert session["id"] == "s1"
    assert len(traces) == 2
    for item in traces:
        assert set(item) == {"trace", "observations"}
        assert "observations" not in item["trace"]
        assert item["trace"]["id"] in ("t1", "t2")
    source.close()


def test_fetch_session_applies_trace_name_filter(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {"trace_name": "openclaw-turn"})
    source.fetch_session("s1")
    client = FakeClient.instances[-1]
    assert client.calls[-1][1]["trace_name"] == "openclaw-turn"
    source.close()


# --------------------------------------------------------------------- #
# convert_session 钩子                                                   #
# --------------------------------------------------------------------- #

def test_convert_session_marks_source_and_applies_hooks(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(
        None,
        {"trace_name": "openclaw-turn"},
        mapper=lambda trace, observations, turn_num, defaults: {"prompt_text": f"[{turn_num}]覆盖"},
        session_mapper=lambda converted, session, traces: {"title": "自定义标题"},
        meta_mapper=lambda converted, session, traces: {"user_id": "u1", "session_id": "s1", "trace_id": "t1"},
    )
    raw, traces = source.fetch_session("s1")
    converted = source.convert_session(raw, traces)

    assert converted["source"] == "langfuse"
    assert isinstance(converted["turns"], list) and len(converted["turns"]) == 2
    assert converted["turns"][0]["prompt_text"].startswith("[1]覆盖")
    assert converted["title"] == "自定义标题"
    assert converted["meta"]["user_id"] == "u1"
    source.close()


def test_convert_session_session_mapper_receives_envelope(monkeypatch):
    _set_env(monkeypatch)
    seen: dict = {}

    def session_mapper(converted, session, traces):
        seen["traces"] = traces
        return None

    source = LangfuseSource(None, {}, session_mapper=session_mapper)
    raw, traces = source.fetch_session("s1")
    source.convert_session(raw, traces)
    assert seen["traces"] == traces
    for item in seen["traces"]:
        assert "trace" in item and "observations" in item
    source.close()


def test_convert_session_tolerates_meta_failure(monkeypatch):
    _set_env(monkeypatch)

    def broken_meta(converted, session, traces):
        raise RuntimeError("boom")

    source = LangfuseSource(None, {}, meta_mapper=broken_meta)
    raw, traces = source.fetch_session("s1")
    converted = source.convert_session(raw, traces)
    assert "meta" not in converted
    source.close()


def test_convert_session_without_hooks(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {})
    raw, traces = source.fetch_session("s1")
    converted = source.convert_session(raw, traces)
    assert converted["session_id"] == "s1"
    assert converted["metrics"]["interaction_turns"] == 2
    assert all("prompt_text" in turn for turn in converted["turns"])
    source.close()


# --------------------------------------------------------------------- #
# preview_sessions                                                       #
# --------------------------------------------------------------------- #

def test_preview_sessions_rich_rows_sorted_desc(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {"trace_name": "openclaw-turn"})
    rows = source.preview_sessions(
        {"from_timestamp": "2026-09-18T00:00:00Z", "to_timestamp": "2026-09-20T00:00:00Z"},
        max_sessions=10,
    )
    assert len(rows) == 2
    assert [r["session_id"] for r in rows] == ["s1", "s2"]  # s1 更新，倒序在前
    row = rows[0]
    assert row["timestamp"] == "2026-09-19T10:05:00Z"  # 组内最新时间
    assert row["user_id"] == "u1"
    assert row["trace_count"] == 2
    assert row["title"] == "你好"
    assert rows[1]["title"] == "真实问题标题"  # 跳过 fenced 元数据行
    source.close()


def test_preview_sessions_caps_and_filters(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {"trace_name": "openclaw-turn"})
    rows = source.preview_sessions(
        {"from_timestamp": "x", "to_timestamp": "y", "user_id": "u2"},
        max_sessions=1,
    )
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s2"
    client = FakeClient.instances[-1]
    call = client.calls[-1]
    assert call[0] == "iter_traces"
    assert call[1]["name"] == "openclaw-turn"
    assert call[1]["user_id"] == "u2"
    assert call[1]["max_items"] == 20
    source.close()


def test_preview_sessions_session_id_short_circuits(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {})
    rows = source.preview_sessions({"session_id": "s1"}, max_sessions=10)
    assert rows == [{"session_id": "s1"}]
    assert FakeClient.instances == []  # 未触碰客户端
    source.close()


# --------------------------------------------------------------------- #
# health                                                                 #
# --------------------------------------------------------------------- #

def test_health_passes_through(monkeypatch):
    _set_env(monkeypatch)
    source = LangfuseSource(None, {})
    result = source.health()
    assert result == {"ok": True, "host": "https://lf.example", "total_sessions": 3}
    source.close()


def test_health_wraps_exceptions(monkeypatch):
    _set_env(monkeypatch)

    class ExplodingClient(FakeClient):
        def health(self):
            raise RuntimeError("network down")

    monkeypatch.setattr(langfuse_source_module, "LangfuseClient", ExplodingClient)
    source = LangfuseSource(None, {})
    result = source.health()
    assert result["ok"] is False
    assert "network down" in result["error"]
    source.close()


# --------------------------------------------------------------------- #
# 租户 adapter 文件接线（_runtime 契约）                                  #
# --------------------------------------------------------------------- #

PILOT_CODE = PILOT_FILE.read_text(encoding="utf-8")


def test_pilot_file_passes_validate_content():
    metadata = _runtime.validate_content(PILOT_CODE, PILOT_FILE.name)
    assert metadata["provider"] == "langfuse"
    assert metadata["enabled"] is True
    assert "from_timestamp" in metadata["supported_filters"]


def test_pilot_file_validate_mode(monkeypatch):
    result = _runtime.test_content(PILOT_CODE, PILOT_FILE.name, "validate", {})
    assert result["ok"] is True
    assert result["metadata"]["provider"] == "langfuse"


def test_pilot_file_session_mode_produces_turns(monkeypatch):
    _set_env(monkeypatch)
    result = _runtime.test_content(PILOT_CODE, PILOT_FILE.name, "session", {"session_id": "s1"})
    assert result["ok"] is True
    session = result["session"]
    assert isinstance(session["turns"], list) and session["turns"]
    assert result["trace_count"] == 2


def test_pilot_file_health_mode(monkeypatch):
    _set_env(monkeypatch)
    result = _runtime.test_content(PILOT_CODE, PILOT_FILE.name, "health", {})
    assert result["ok"] is True
