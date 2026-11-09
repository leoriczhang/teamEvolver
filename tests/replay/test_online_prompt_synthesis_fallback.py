"""Online Prompt synthesis shares Skill fallback without weakening offline anchors."""
from __future__ import annotations

import copy
import json

import pytest

from team_replay.datasets import synthesis
from team_skills.evolution.replay_case_synthesis import CrossSessionCaseSynthesisError, LLMCrossSessionCaseSynthesizer
from team_skills.execution.planning import synthesis_context
from teamEvolver.evidence_context import EvidenceContextError
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport


def sources(*, offline=False, context_prefix=False, requirement_count=6):
    values = []
    requirements = "\n### 要求\n" + "\n".join(f"{i}. Original shared requirement {i}" for i in range(requirement_count))
    for i in (1, 2):
        ref = {"source_session_key": f"root-{i}", "trace_id": f"target-{i}",
               "revision": "v1", "context_revision": "c1"}
        target = {"trace_id": ref["trace_id"], "turn_num": 2 if context_prefix else 1,
                  "prompt_text": f"Original task {i}" + requirements,
                  "messages": [{"role": "user", "content": "Correct the original reusable workflow"}]}
        if offline:
            target["request_context"] = {"trace_input": {"reconstruction": {"constructed_import_batch": True}}}
        context_ref = {**ref, "trace_id": f"context-{i}"}
        context = {"trace_id": context_ref["trace_id"], "turn_num": 1,
                   "prompt_text": "CONTEXT_ONLY_QUERY\n### 要求\nCONTEXT_ONLY_CHECK"}
        values.append({"session_id": f"source-{i}", "runtime": {"type": "fake"},
                       "_summary": "SUMMARY_IS_NOT_A_CHECK", "_trajectory": "Recorded trajectory",
                       "turns": [context, target] if context_prefix else [target], "_evidence_turns": [target],
                       "provenance": {"schema_version": "session-source.v1", "source_session_key": f"root-{i}",
                           "identity_verified": True, "trace_identity_verified": True,
                           "target_trace_refs": [ref],
                           "trace_revisions": [ref, context_ref] if context_prefix else [ref],
                           "context_trace_refs": [context_ref] if context_prefix else []}})
    return values


def context(prompt):
    return ({"allowed_modules": ["prompt"], "execution_profile_id": "profile"} if prompt
            else {"allowed_modules": ["skill"]})


class Model:
    def __init__(self, mode="valid"):
        self.mode, self.calls = mode, []

    async def chat(self, messages, **options):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(options)))
        wire = json.loads(messages[1]["content"])
        self.payload = decode_evidence_transport(wire) if wire.get("schema") == SCHEMA else wire
        if self.mode == "invalid_json":
            return "Incomplete JSON {"
        if self.mode == "empty":
            return '{"test_datasets":[]}'
        if self.mode == "provider_error":
            raise RuntimeError("No provider response")
        anchors = (self.payload.get("evidence_context") or {}).get("examples") or []
        count = 3 if self.mode == "excess" else 1 if self.mode == "one_case" else 2
        check_count = 9 if self.mode == "excess" else 3 if self.mode == "too_few_checks" else 7
        cases = []
        for i in range(count):
            item = {"query": f"Synthesized representative task {i}", "source_session_ids": ["source-1", "source-2"],
                    "checks": [{"id": f"R{j}", "kind": "output", "text": f"Original model requirement {j}"}
                               for j in range(check_count)]}
            if self.payload.get("query_contract"):
                item.pop("query")
                item.update(query_mode="recorded_anchor", anchor_id=anchors[i % len(anchors)]["example_id"])
                if self.mode == "fake_anchor":
                    item["anchor_id"] = "invented-anchor"
            cases.append(item)
        return json.dumps({"test_datasets": cases})


async def generate(model, sessions, *, prompt=True):
    return await LLMCrossSessionCaseSynthesizer(model)(skill_name="demo", sessions=sessions,
                                                     candidate_skill={}, evidence_context=context(prompt))


@pytest.fixture(autouse=True)
def native_options(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_json", "empty", "provider_error", "too_few_checks"])
async def test_online_prompt_and_skill_reuse_the_same_grounded_fallback(mode):
    values = sources()
    before = copy.deepcopy(values)
    skill, prompt = Model(mode), Model(mode)
    expected = await generate(skill, values, prompt=False)
    actual = await generate(prompt, values)
    assert actual == expected and len(actual) == 2
    assert all(len(case["checks"]) == 6 and len(case["provenance"]["source_session_keys"]) == 2 for case in actual)
    assert {case["query"] for case in actual} == {session["turns"][0]["prompt_text"] for session in values}
    assert len(skill.calls) == len(prompt.calls) == 1 and values == before


@pytest.mark.asyncio
async def test_online_prompt_and_skill_normalize_extra_cases_and_checks_identically():
    skill, prompt = Model("excess"), Model("excess")
    expected = await generate(skill, sources(), prompt=False)
    actual = await generate(prompt, sources())
    assert actual == expected and len(actual) == 2 and all(len(case["checks"]) == 8 for case in actual)
    assert [case["query"] for case in actual] == [f"Synthesized representative task {i}" for i in range(2)]
    assert len(skill.calls) == len(prompt.calls) == 1


@pytest.mark.asyncio
async def test_online_fallback_uses_target_turns_not_context_seeds_summaries_or_proposal_claims():
    values = sources(context_prefix=True)
    before = copy.deepcopy(values)
    model = Model("invalid_json")
    cases = await synthesis.synthesize_evolution_datasets(
        model, skill_name="demo", sessions=values, evidence_context=context(True),
        candidate_skill={"evidence_classification": {"team_skill": [{"claim": "PROPOSAL_IS_NOT_A_CHECK"}]}},
        replay_windows={"recent": [{"session_id": "source-1", "instruction": "CONTEXT_ONLY_QUERY"}]},
        case_count=2, min_requirements=6, max_requirements=8, min_source_sessions=2,
    )
    assert len(cases) == 2 and len(model.calls) == 1
    assert {case["query"] for case in cases} == {session["_evidence_turns"][0]["prompt_text"] for session in values}
    assert all(len(case["checks"]) == 6 for case in cases)
    check_text = json.dumps([case["checks"] for case in cases])
    assert all(text not in check_text for text in ("CONTEXT_ONLY", "PROPOSAL_IS_NOT", "SUMMARY_IS_NOT"))
    assert values == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_json", "empty", "provider_error", "too_few_checks", "fake_anchor", "excess"])
async def test_explicit_offline_failures_cannot_use_generic_fallback_or_change_tasks(monkeypatch, mode):
    def forbidden(**kwargs):
        pytest.fail("Explicit offline records cannot use generic grounded fallback")

    monkeypatch.setattr(synthesis, "_fallback_datasets", forbidden)
    values = sources(offline=True)
    before = copy.deepcopy(values)
    model = Model(mode)
    with pytest.raises(EvidenceContextError):
        await generate(model, values)
    assert len(model.calls) == 1 and values == before


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["one_source", "same_root", "empty_targets", "context_session", "context_turn"])
async def test_online_fallback_does_not_pad_insufficient_targets_with_context(scope):
    values = sources()
    if scope == "one_source":
        values = values[:1]
    elif scope == "same_root":
        values[1]["provenance"]["source_session_key"] = "root-1"
        for field in ("trace_revisions", "target_trace_refs"):
            values[1]["provenance"][field][0]["source_session_key"] = "root-1"
    elif scope == "empty_targets":
        values[1]["provenance"]["target_trace_refs"] = []
    elif scope == "context_session":
        values[1]["usage"] = "context_only"
    else:
        values[1]["turns"][0]["context_only"] = True
    model = Model("invalid_json")
    with pytest.raises(CrossSessionCaseSynthesisError):
        await generate(model, values)
    assert len(model.calls) == (1 if scope.startswith("context_") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_json", "one_case"])
async def test_online_fallback_does_not_invent_checks_or_pad_a_partial_generated_batch(mode):
    model = Model(mode)
    with pytest.raises(CrossSessionCaseSynthesisError, match="exactly two"):
        await generate(model, sources(requirement_count=5))
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_previous_replay_details_are_excluded_from_actual_synthesis_requests():
    metrics = {"baseline": {"interaction_turns": 2, "tool_call_count": 1, "total_tokens": 30},
               "candidate": {"interaction_turns": 1, "tool_call_count": 0, "total_tokens": 20}}
    ctx = {**context(True), "active_candidate_feedback": metrics,
           "active_candidate_replay_feedback": {"response": "PRIVATE_PREVIOUS_REPLAY_REPLY_SENTINEL"}}
    model = Model()
    await LLMCrossSessionCaseSynthesizer(model)(skill_name="demo", sessions=sources(), candidate_skill={},
                                               evidence_context=synthesis_context(ctx))
    assert "PRIVATE_PREVIOUS_REPLAY_REPLY_SENTINEL" not in json.dumps(model.calls)
    assert "active_candidate_replay_feedback" not in model.payload["team_sop_evidence"]["context"]
    assert model.payload["team_sop_evidence"]["context"]["active_candidate_feedback"] == metrics
