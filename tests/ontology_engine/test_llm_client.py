"""LLM 客户端测试：形状修复、结构化输出校验与重试。"""

from __future__ import annotations

import pytest

from team_ontology.engine.config import LLMConfig
from team_ontology.engine.llm.client import LLMClient, LLMError, _shape_repair

SINGLE_ARRAY_SCHEMA = {
    "type": "object",
    "properties": {"xs": {"type": "array", "items": {}}},
    "required": ["xs"],
    "additionalProperties": False,
}


def test_shape_repair_wraps_bare_list():
    assert _shape_repair([{"a": 1}], SINGLE_ARRAY_SCHEMA) == {"xs": [{"a": 1}]}


def test_shape_repair_ambiguous_list_untouched():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "array"}, "b": {"type": "array"}},
    }
    assert _shape_repair([1], schema) == [1]


def test_shape_repair_dict_untouched():
    assert _shape_repair({"xs": [1]}, SINGLE_ARRAY_SCHEMA) == {"xs": [1]}


def test_complete_json_shape_repairs_bare_list(monkeypatch):
    client = LLMClient(LLMConfig(model="m"))
    calls = {"n": 0}

    def fake_call(_system, _user, _schema, extra_note=None):
        calls["n"] += 1
        return [{"ok": True}]

    monkeypatch.setattr(client, "_call", fake_call)
    result = client.complete_json(system="s", user="u", json_schema=SINGLE_ARRAY_SCHEMA, prompt_version="p")
    assert result == {"xs": [{"ok": True}]}
    assert calls["n"] == 1


def test_complete_json_retries_until_valid(monkeypatch):
    import time as time_module

    monkeypatch.setattr(time_module, "sleep", lambda _s: None)
    schema = {
        "type": "object",
        "properties": {"xs": {"type": "array", "items": {"type": "object"}}},
        "required": ["xs"],
        "additionalProperties": False,
    }
    client = LLMClient(LLMConfig(model="m"))
    responses = iter([{"xs": "不是数组"}, {"xs": [{"ok": True}]}])

    def fake_call(_system, _user, _schema, extra_note=None):
        return next(responses)

    monkeypatch.setattr(client, "_call", fake_call)
    result = client.complete_json(system="s", user="u", json_schema=schema, prompt_version="p")
    assert result == {"xs": [{"ok": True}]}  # 第二次才合法


def test_complete_json_raises_after_retries(monkeypatch):
    import time as time_module

    monkeypatch.setattr(time_module, "sleep", lambda _s: None)
    client = LLMClient(LLMConfig(model="m"))

    def fake_call(_system, _user, _schema, extra_note=None):
        return ["still a list"]  # 单数组键可包裹，但 items 是 {"type":"object"}？此处用宽松 items

    monkeypatch.setattr(client, "_call", fake_call)
    schema = {
        "type": "object",
        "properties": {"xs": {"type": "array", "items": {"type": "object"}}},
        "required": ["xs"],
        "additionalProperties": False,
    }
    with pytest.raises(LLMError):
        client.complete_json(system="s", user="u", json_schema=schema, prompt_version="p")
