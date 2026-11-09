"""Literal frozen templates verified against bytes sent by the model transport."""
from __future__ import annotations

import copy
import hashlib
import json

import httpx
import pytest

from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.engine import make_context
from team_replay.execution_plan import (
    EXECUTION_PLAN_SCHEMA,
    ExecutionPlanError,
    normalize_execution_plan,
    render_components,
    validate_execution_plan_pair,
)
from team_replay.hooks import ReplayUnsupported
from team_replay.request_capture import make_execution_receipt, materialize_messages, verify_execution_receipt


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan(text="Hello {{name}}", variables=None):
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA, "binding": {"runtime_type": "observed_http"},
        "target_component_ids": ["main"],
        "environment": {"template_variables": {"name": "Alice"} if variables is None else variables,
                        "request": {"model": "scripted-model", "temperature": 0, "tools": []}},
        "components": [{"component_id": "main", "kind": "agent_prompt", "slot": "system", "order": 0,
                        "revision": "1", "binding": {}, "content": text, "content_hash": sha(text)}],
    }


def context(value, branch="baseline"):
    return make_context(branch, {}, {"materials": []}, {"runtime": {"type": "observed_http"}}, 10,
                        execution_plan=value)


def dispatch(ctx, *, content=None):
    body = copy.deepcopy(ctx.execution_plan["environment"]["request"])
    body["messages"] = materialize_messages(ctx, "Task")
    if content is not None:
        body["messages"][0]["content"] = content
    sent = []
    _, identity = ctx.request_observer.dispatch(body, lambda raw: sent.append(raw), destination="https://model.test/chat")
    return make_execution_receipt(ctx, 1, [identity]), sent[0]


def verify(ctx, receipt):
    return verify_execution_receipt(ctx, receipt, turn_num=1, user_message="Task", after=0)


def test_scalar_rendering_is_literal_nonrecursive_and_keeps_ordinary_json_braces():
    value = plan('{"profile":{"name":"{{ name }}","enabled":{{enabled}},"count":{{count}}}}',
                 {"name": "{{not_evaluated}}", "enabled": True, "count": 3.5})
    rendered = render_components(normalize_execution_plan(value))[0]
    assert rendered["content"] == '{"profile":{"name":"{{not_evaluated}}","enabled":true,"count":3.5}}'
    assert rendered["template_hash"] == sha(value["components"][0]["content"])
    assert rendered["rendered_content_hash"] == sha(rendered["content"])
    assert rendered["variable_names"] == ["count", "enabled", "name"]
    assert value["components"][0]["content"].startswith('{"profile":{"name":"{{ name }}"')


@pytest.mark.parametrize("text", ["{{name.upper()}}", "{{name|safe}}", "{{items[0]}}", "{{}}", "{{name",
                                  "{% if name %}yes{% endif %}", "{# comment #}", "{{{name}}}"])
def test_unknown_template_syntax_is_explicitly_rejected_without_execution(text):
    with pytest.raises(ExecutionPlanError, match="template syntax"):
        normalize_execution_plan(plan(text))


@pytest.mark.parametrize("variables", [{"name": {"nested": "value"}}, {"name": ["item"]}, {"name": None},
                                       {"name": float("nan")}, {"name": float("inf")}, {"name.value": "x"}])
def test_template_variables_require_finite_json_scalars_and_identifier_names(variables):
    with pytest.raises(ExecutionPlanError):
        normalize_execution_plan(plan(variables=variables))


def test_missing_variable_does_not_fall_back_to_session_environment_or_empty_text():
    with pytest.raises(ExecutionPlanError, match="missing Prompt template variable: name"):
        normalize_execution_plan(plan(variables={}))


def test_rendered_byte_limits_are_checked_before_unbounded_expansion():
    with pytest.raises(ExecutionPlanError, match="rendered.*size limit"):
        normalize_execution_plan(plan("{{name}}" * 1000, {"name": "中" * 1000}))
    with pytest.raises(ExecutionPlanError, match="128 variables"):
        normalize_execution_plan(plan("Static", {f"name{i}": "x" for i in range(129)}))


def test_baseline_and_candidate_must_share_exact_frozen_variables():
    before, after = plan(), plan("Improved {{name}}")
    validate_execution_plan_pair(before, after)
    after["environment"]["template_variables"]["name"] = "Bob"
    with pytest.raises(ExecutionPlanError, match="environment must remain fixed"):
        validate_execution_plan_pair(before, after)


def test_reference_http_transport_sends_rendered_text_and_receipts_bind_all_three_hashes():
    sent = []

    def post(endpoint, **kwargs):
        sent.append(kwargs["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]},
                              request=httpx.Request("POST", endpoint))

    factory = ObservedHttpFactory({"endpoint": "https://model.test/chat"}, post=post)
    plans = validate_execution_plan_pair(plan(), plan("Improved {{name}}"))
    branches = zip(("baseline", "candidate"), plans, ("Hello Alice", "Improved Alice"), strict=True)
    for branch, value, expected in branches:
        ctx = context(value, branch)
        session = factory.open(ctx)
        observation = session.send("Task")
        session.close()
        captured = verify(ctx, observation.execution_receipt)[0]
        assert json.loads(sent[-1])["messages"][0]["content"] == expected
        assert captured["request_hash"] == hashlib.sha256(sent[-1]).hexdigest()
        rendering = captured["component_renderings"][0]
        assert rendering["template_hash"] == sha(value["components"][0]["content"])
        assert rendering["rendered_content_hash"] == sha(expected)
        variables_json = json.dumps({"name": "Alice"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert rendering["template_variables_hash"] == sha(variables_json)


@pytest.mark.parametrize("content", ["Hello {{name}}", "Hello Bob"])
def test_unrendered_or_wrong_variable_transport_bytes_cannot_pass(content):
    ctx = context(plan())
    receipt, _ = dispatch(ctx, content=content)
    with pytest.raises(ReplayUnsupported, match="ignored, changed"):
        verify(ctx, receipt)


def test_fabricated_or_missing_template_rendering_receipts_cannot_pass():
    ctx = context(plan())
    receipt, _ = dispatch(ctx)
    forged = copy.deepcopy(receipt)
    forged["requests"][0]["component_renderings"][0]["template_variables_hash"] = "0" * 64
    with pytest.raises(ReplayUnsupported, match="rendering hashes"):
        verify(ctx, forged)
    del receipt["requests"][0]["component_renderings"]
    with pytest.raises(ReplayUnsupported, match="rendering hashes"):
        verify(ctx, receipt)


def test_static_nested_json_is_unchanged_and_legacy_receipts_remain_compatible():
    text = '{"static":{"value":3,"nested":{}}}'
    ctx = context(plan(text, {}))
    receipt, raw = dispatch(ctx)
    del receipt["requests"][0]["component_renderings"]
    assert json.loads(raw)["messages"][0]["content"] == text
    assert verify(ctx, receipt)[0]["request"] == json.loads(raw)
