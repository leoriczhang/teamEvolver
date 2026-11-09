"""Current Candidate Cases summarize common patterns across Sessions."""
import copy
import hashlib
import json

import pytest

from team_skills.evolution.prompt_studio import build_stage_messages, get_prompt
from team_skills.evolution.replay_case_synthesis import (
    CrossSessionCaseSynthesisError,
    LLMCrossSessionCaseSynthesizer,
    cross_session_synthesis_input,
    hydrate_cross_session_cases,
)


def sessions():
    return [
        {
            "session_id": session_id,
            "_summary": f"Shared workflow evidence {index}",
            "_trajectory": "tool sequence",
            "turns": [{"turn_num": 1, "prompt_text": f"Task {index}", "response_text": "Done"}],
            "runtime": {"type": "agentshub"},
            "runtime_context": {"source_session_id": session_id},
        }
        for index, session_id in enumerate(("s1", "s2", "s3"), start=1)
    ]


class Model:
    def __init__(self):
        self.system = ""
        self.payload = {}

    async def chat(self, messages, **kwargs):
        self.system = messages[0]["content"]
        self.payload = json.loads(messages[1]["content"])
        cases = []
        for index in range(kwargs.get("trace_metadata", {}).get("case_count", 2)):
            cases.append({
                "name": f"Common pattern {index + 1}",
                "query": f"Please complete representative task {index + 1}",
                "checks": [
                    {"id": f"R{number + 1:02d}", "kind": "output", "text": f"Condition {number + 1}"}
                    for number in range(7)
                ],
                "source_session_ids": ["s1", "s2", "s3"],
                "evidence_window": "recent",
            })
        return json.dumps({"test_datasets": cases})


@pytest.mark.asyncio
async def test_synthesizes_exactly_two_cross_session_cases_with_six_to_eight_checks():
    model = Model()
    cases = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name="demo",
        sessions=sessions(),
        candidate_skill={"description": "Demo", "content": "Reusable workflow"},
        evidence_context={"current_session_count": 3},
    )
    assert len(cases) == 2
    assert "至少 2 条不同 Session" in model.system
    assert all(len(case["checks"]) == 7 for case in cases)
    assert all(case["provenance"]["session_ids"] == ["s1", "s2", "s3"] for case in cases)
    assert all(case["provenance"]["synthesis_mode"] == "cross_session" for case in cases)
    assert all(case["provenance"]["session_snapshot"]["turns"] for case in cases)


@pytest.mark.asyncio
async def test_evolution_synthesis_uses_evidence_without_candidate_content():
    model = Model()
    cases = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name="__no_skill__", sessions=sessions(), candidate_skill={},
        evidence_context={"current_session_count": 3, "experience_patterns": "Shared procedure evidence"},
    )
    assert len(cases) == 2
    assert "candidate_skill" not in model.payload
    assert model.payload["team_sop_evidence"]["context"]["experience_patterns"] == "Shared procedure evidence"
    assert "不得把候选内容或编辑结果当作需求证据" in model.system


@pytest.mark.asyncio
async def test_current_count_comes_from_frozen_context_and_hydrates_all_three_cases():
    context = {"current_session_count": 3, "replay_case_counts": {"current": 3, "historical": 6}}
    original = copy.deepcopy(context)
    model = Model()
    generated = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name="demo", sessions=sessions(), candidate_skill={}, evidence_context=context,
    )
    assert len(generated) == 3
    assert context == original
    assert model.payload["team_sop_evidence"]["context"]["replay_case_counts"] == context["replay_case_counts"]
    assert all(len(case["checks"]) == 7 for case in generated)
    assert all(case["provenance"]["session_ids"] == ["s1", "s2", "s3"] for case in generated)
    assert all(len(case["provenance"]["source_session_keys"]) == 3 for case in generated)


@pytest.mark.parametrize("invalid", [0, 7, True, "3", None])
def test_invalid_frozen_current_count_is_rejected_before_model_call(invalid):
    with pytest.raises(CrossSessionCaseSynthesisError, match="count"):
        cross_session_synthesis_input(
            skill_name="demo", sessions=sessions(), candidate_skill={},
            evidence_context={"replay_case_counts": {"current": invalid}},
        )


def test_dataset_stage_preview_has_no_invented_candidate():
    messages = build_stage_messages("dataset_synthesis", sessions()[0], system_prompt="Use evidence")
    assert "candidate_skill" not in json.loads(messages[1]["content"])
    assert "candidate Skill" not in get_prompt("dataset_synthesis", {})["variables"]


def test_rejects_a_case_supported_by_only_one_session():
    raw = [{
        "case_id": "one",
        "query": "Task",
        "checks": [
            {"id": f"R{number + 1:02d}", "kind": "output", "text": f"Condition {number + 1}"}
            for number in range(7)
        ],
        "provenance": {"session_ids": ["s1"]},
    }] * 2
    with pytest.raises(CrossSessionCaseSynthesisError, match="at least two"):
        hydrate_cross_session_cases(raw, sessions(), skill_name="demo")


def _anchored_sources():
    result = []
    for sid, traces, targets in (("s1", ["t1"], ["t1"]),
                                 ("s2", ["t2-first", "t2-anchor", "t2-context"], ["t2-first", "t2-anchor"])):
        refs = [{"source_session_key": sid, "trace_id": trace, "revision": trace + "-v1",
                 "context_revision": trace + "-context-v1"} for trace in traces]
        result.append({
            "session_id": sid,
            "turns": [{"trace_id": trace, "turn_num": number,
                       "prompt_text": "Recorded request " + trace, "response_text": "Recorded response " + trace}
                      for number, trace in enumerate(traces, 1)],
            "runtime": {"type": "agentshub"}, "runtime_context": {"agent_id": "agent-" + sid},
            "provenance": {
                "schema_version": "session-source.v1", "source_session_key": sid,
                "identity_verified": True, "trace_identity_verified": True, "trace_revisions": refs,
                "target_trace_refs": [ref for ref in refs if ref["trace_id"] in targets],
                "context_trace_refs": [ref for ref in refs if ref["trace_id"] not in targets],
            },
        })
    return result


def _anchored_cases():
    result = []
    for sid, trace in (("s2", "t2-anchor"), ("s1", "t1")):
        query = "Recorded request " + trace
        result.append({
            "case_id": "case-" + trace, "query": query,
            "checks": [{"id": f"R{number}", "kind": "output", "text": f"Grounded condition {number}"}
                       for number in range(7)],
            "provenance": {
                "session_ids": ["s1", "s2"],
                "evidence_anchor": {
                    "anchor_id": "example-" + trace, "session_id": sid, "trace_id": trace,
                    "evidence_ref": trace, "recorded_query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                },
            },
        })
    return result


def test_anchored_replay_uses_the_selected_source_and_only_its_recorded_target():
    sources = _anchored_sources()
    original = copy.deepcopy(sources)
    cases = hydrate_cross_session_cases(_anchored_cases(), sources, skill_name=None)
    first = cases[0]["provenance"]
    assert first["session_ids"] == ["s1", "s2"]
    assert first["session_id"] == "s2" and first["turn_num"] == 2
    assert first["source_runtime_context"] == {"agent_id": "agent-s2"}
    snapshot = first["session_snapshot"]
    assert snapshot["session_id"] == "s2"
    assert [turn["trace_id"] for turn in snapshot["turns"]] == ["t2-anchor"]
    assert snapshot["provenance"]["context_trace_refs"] == []
    assert [ref["trace_id"] for ref in snapshot["provenance"]["target_trace_refs"]] == ["t2-anchor"]
    assert [ref["trace_id"] for ref in snapshot["provenance"]["trace_revisions"]] == ["t2-anchor"]
    assert snapshot["provenance"]["replay_anchor_projection"] == {
        "anchor_id": "example-t2-anchor", "source_session_id": "s2", "trace_id": "t2-anchor",
    }
    assert [ref["trace_id"] for ref in first["target_trace_refs"]] == ["t2-anchor"]
    assert len(cases) == 2 and all(len(case["checks"]) == 7 for case in cases)
    snapshot["turns"][0]["response_text"] = "Changing a Replay snapshot must not change source Evidence"
    assert sources == original


@pytest.mark.parametrize("change", ["source", "trace", "context_only", "wrong_query", "missing_target"])
def test_anchor_cannot_select_a_different_source_or_non_target_trace(change):
    sources, cases = _anchored_sources(), _anchored_cases()
    anchor = cases[0]["provenance"]["evidence_anchor"]
    if change == "source":
        anchor["session_id"] = "unlisted-source"
    elif change == "trace":
        anchor["trace_id"] = "missing-trace"
    elif change == "context_only":
        anchor["trace_id"] = "t2-context"
    elif change == "wrong_query":
        cases[0]["query"] = "An invented history changes the invitation state"
    elif change == "missing_target":
        sources[1]["provenance"]["target_trace_refs"] = sources[1]["provenance"]["target_trace_refs"][:1]
    with pytest.raises(CrossSessionCaseSynthesisError):
        hydrate_cross_session_cases(cases, sources, skill_name=None)
