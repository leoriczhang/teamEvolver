"""Offline Prompt Case output capacity leaves legacy calls and explicit options intact."""
from __future__ import annotations

import copy
import json

import pytest

from team_replay.datasets import synthesis
from team_skills.evolution.prompt_context import prompt_context
from team_skills.evolution.prompt_studio import stage_call_options
from team_skills.evolution.replay_case_synthesis import LLMCrossSessionCaseSynthesizer
from teamEvolver.evidence_context import EvidenceContextError
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport

DEFINITION = "原规则：保留唯一条件、完整列表及其例外。" * 300


def sources(*, offline=True):
    result = []
    for index in (1, 2):
        query = f"\r\n当前原请求{index}。原历史未提供。\t"
        ref = {"source_session_key": f"root-{index}", "trace_id": f"trace-{index}",
               "revision": "v1", "context_revision": "c1"}
        request = {"trace_input": {"business_context": {"long_definition": DEFINITION}}}
        if offline:
            request["trace_input"]["reconstruction"] = {
                "constructed_import_batch": True, "actual_original_request_observed": False,
                "original_session_independence": "unknown",
            }
        result.append({"session_id": f"source-{index}", "provenance": {
            "schema_version": "session-source.v1", "source_session_key": f"root-{index}",
            "identity_verified": True, "trace_identity_verified": True,
            "target_trace_refs": [ref], "trace_revisions": [ref], "context_trace_refs": [],
        }, "turns": [{"trace_id": f"trace-{index}", "turn_num": 1, "prompt_text": query,
                      "response_text": "ORIGINAL_SOURCE_REPLY", "messages": [
                          {"role": "user", "content": query},
                          {"role": "assistant", "content": "ORIGINAL_SOURCE_REPLY"},
                          {"role": "user", "content": "ORIGINAL_SOURCE_MACHINE_FEEDBACK"},
                      ], "request_context": request}]})
    return result


def context(*, prompt=True):
    if not prompt:
        return {"allowed_modules": ["skill"]}
    return {"allowed_modules": ["prompt"], "execution_profile_id": "profile",
            "baseline_execution_plan": {"binding": {"runtime_type": "observed_http"},
                "components": [{"component_id": "main", "kind": "agent_prompt", "slot": "system", "order": 0,
                                "content": "遵守完整原业务条件：{{definition}}"}],
                "environment": {"template_variables": {"definition": DEFINITION},
                                "request": {"max_tokens": 73}}}}


def body(messages):
    value = json.loads(messages[1]["content"])
    return decode_evidence_transport(value) if value.get("schema") == SCHEMA else value


class Model:
    def __init__(self, *, truncated=False):
        self.truncated = truncated
        self.calls, self.generations = [], []

    async def chat(self, messages, **options):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(options)))
        payload = body(messages)
        self.generations.append(payload)
        if self.truncated:
            return '{"test_datasets": [{"name": "truncated", "checks": [{"text": "回复不得'
        evidence = payload.get("evidence_context") or {}
        anchors = evidence.get("examples") or []
        cases = []
        for index in range(2):
            checks = [{"id": f"R{i:02}", "kind": "output", "text": f"原始业务条件{i}"} for i in range(1, 7)]
            checks[0]["text"] += " 完整原定义：" + DEFINITION
            case = {"name": f"源任务{index}", "checks": checks,
                    "source_session_ids": ["source-1", "source-2"], "evidence_window": "recent"}
            if payload.get("query_contract"):
                case.update(query_mode="recorded_anchor", anchor_id=anchors[index]["example_id"])
            else:
                case["query"] = anchors[index]["recorded_query"] if anchors else f"完整源任务{index}"
                if evidence.get("anchor_required_for_session_ids"):
                    case["anchor_id"] = anchors[index]["example_id"]
            cases.append(case)
        return json.dumps({"test_datasets": cases}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def no_host_hooks(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


@pytest.mark.parametrize("cap", [8192, 16384, 32768, 65536])
def test_explicit_stage_provider_options_are_never_raised_or_mutated(monkeypatch, cap):
    options = {"max_tokens": cap, "temperature": 0.17, "model": "explicit-stage-model", "stage_id": "dataset_synthesis"}
    before, calls = copy.deepcopy(options), []

    def provider(stage):
        calls.append(stage)
        return options

    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", provider)
    assert synthesis._synthesis_call_options(recorded_anchor_enabled=True) is options
    assert options == before and calls == ["dataset_synthesis"]


@pytest.mark.parametrize(("enabled", "cap"), [(True, 32768), (False, 16384)])
def test_provider_failure_retains_the_existing_fallback_with_scoped_capacity(monkeypatch, enabled, cap):
    def provider(stage):
        raise RuntimeError("Unavailable injected stage-options hook")

    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", provider)
    assert synthesis._synthesis_call_options(recorded_anchor_enabled=enabled) == {"max_tokens": cap, "temperature": 0.3}


@pytest.mark.asyncio
@pytest.mark.parametrize(("prompt", "offline", "cap"), [
    (True, True, 32768), (True, False, 16384), (False, True, 16384), (False, False, 16384),
])
async def test_real_synthesis_dispatch_scopes_capacity_without_changing_source_or_case_contract(prompt, offline, cap):
    sessions, source_context, model = sources(offline=offline), context(prompt=prompt), Model()
    before = copy.deepcopy((sessions, source_context))
    cases = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name=None if prompt else "demo", sessions=sessions, candidate_skill={}, evidence_context=source_context)
    synthesis_calls = [options for _, options in model.calls]
    assert len(synthesis_calls) == 1 and synthesis_calls[0]["max_tokens"] == cap
    assert synthesis_calls[0]["temperature"] == 0.3
    assert len(cases) == 2 and all(len(case["checks"]) == 6 for case in cases)
    assert (sessions, source_context) == before
    assert all(DEFINITION in case["checks"][0]["text"] for case in cases)
    assert len(model.calls) == 1
    if prompt and offline:
        assert [case["query"] for case in cases] == [session["turns"][0]["prompt_text"] for session in sessions]
        assert source_context["baseline_execution_plan"]["environment"]["request"]["max_tokens"] == 73


@pytest.mark.asyncio
async def test_registered_explicit_stage_cap_is_honored_when_host_supplies_its_provider(monkeypatch):
    config = {"stage_settings": {"dataset_synthesis": {"max_tokens": 8192, "temperature": 0.12}}}
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", stage_call_options)
    model = Model()
    with prompt_context(config):
        await LLMCrossSessionCaseSynthesizer(model)(
            skill_name=None, sessions=sources(), candidate_skill={}, evidence_context=context())
    options = model.calls[0][1]
    assert options["max_tokens"] == 8192 and options["temperature"] == 0.12
    assert options["stage_id"] == "dataset_synthesis"
    assert config["stage_settings"]["dataset_synthesis"]["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_partial_json_is_not_completed_or_resampled():
    model = Model(truncated=True)
    with pytest.raises(EvidenceContextError, match="did not produce valid Cases"):
        await LLMCrossSessionCaseSynthesizer(model)(
            skill_name=None, sessions=sources(), candidate_skill={}, evidence_context=context())
    assert len(model.calls) == len(model.generations) == 1
    assert model.calls[0][1]["max_tokens"] == 32768
