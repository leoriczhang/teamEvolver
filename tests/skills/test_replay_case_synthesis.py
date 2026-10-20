"""Current Candidate Cases summarize common patterns across Sessions."""
import json

import pytest

from team_skills.evolution.prompt_studio import build_stage_messages, get_prompt
from team_skills.evolution.replay_case_synthesis import (
    CrossSessionCaseSynthesisError,
    LLMCrossSessionCaseSynthesizer,
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
        for index in range(2):
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
