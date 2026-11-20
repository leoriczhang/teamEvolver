"""Evidence review is grounded in differences and never invents runtime proof."""
import json
from types import SimpleNamespace

import pytest

from team_skills.evolution.assessment import assess, assessment_gate
from team_skills.evolution.history import skill_payload
from tests.support.replay import bundle


def review_job():
    return {
        "current_skill": skill_payload("demo", bundle("demo", "Use available integration")),
        "candidate_skill": skill_payload(
            "demo", bundle("demo", "Use available integration; pause without credentials"),
        ),
        "replay_cases": [],
    }


def response(**changes):
    return {
        "method": "experience_review", "verdict": "accept", "reason": "Adds supported dependency handling",
        "scope": "工具路由及依赖处理", "evidence_refs": ["source"], "issues": [],
        **changes,
    }


@pytest.mark.asyncio
async def test_reviewer_sees_real_bundle_diff_and_returns_auditable_refs():
    async def chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert "+Use available integration; pause without credentials" in payload["changes"][0]["diff"]
        return json.dumps(response())
    result = await assess(review_job(), evidence=[{"session_id": "source", "_summary": "Missing credentials"}],
                          requested_method="auto", client=SimpleNamespace(chat=chat))
    assert result["source"] == "model" and result["input_hash"]
    assert assessment_gate({"validation_method": "experience_review", "assessment": result})


@pytest.mark.asyncio
async def test_unknown_evidence_is_rejected_and_no_evidence_cannot_pass():
    async def chat(*args, **kwargs):
        return json.dumps(response(evidence_refs=["fabricated"]))
    with pytest.raises(ValueError, match="Evidence"):
        await assess(review_job(), evidence=[], requested_method="auto", client=SimpleNamespace(chat=chat))
    async def empty(*args, **kwargs):
        return json.dumps(response(evidence_refs=[]))
    result = await assess(review_job(), evidence=[], requested_method="auto", client=SimpleNamespace(chat=empty))
    assert result["verdict"] == "inconclusive"


@pytest.mark.asyncio
async def test_truncated_difference_or_hard_constraint_issue_cannot_pass_review():
    async def chat(*args, **kwargs):
        return json.dumps(response())
    job = review_job()
    job["candidate_skill"] = skill_payload("demo", bundle("demo", "x" * 200000))
    result = await assess(job, evidence=[{"session_id": "source"}], requested_method="auto",
                          client=SimpleNamespace(chat=chat))
    assert result["verdict"] == "inconclusive"
    assert not assessment_gate({
        "validation_method": "experience_review",
        "assessment": {**response(), "source": "model", "issues": ["Violates a hard constraint"]},
    })
