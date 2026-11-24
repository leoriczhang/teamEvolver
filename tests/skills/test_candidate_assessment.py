"""Evidence review is grounded in differences and never invents runtime proof."""
import json
from types import SimpleNamespace

import pytest

from team_skills.evolution.assessment import assess, assessment_gate, review_input
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
async def test_reviewer_receives_source_contract_and_original_visual_availability():
    evidence = {
        "session_id": "source", "_summary": "Cached summary incorrectly rejects all visual evidence",
        "runtime": {"agent_graph": {"agents": {"a": {"system": "opening_hook must cite hook_000_003"}}}},
        "turns": [{"content": [{"type": "video", "url": "secret-input-location"}]}],
    }

    async def chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        contract = payload["source_contracts"][payload["evidence"][0]["source_contract_ref"]]
        assert contract["agents"][0]["system"] == "opening_hook must cite hook_000_003"
        assert contract["input_modalities"] == ["video"]
        assert "secret-input-location" not in messages[1]["content"]
        return json.dumps(response(verdict="inconclusive"))

    result = await assess(review_job(), evidence=[evidence], requested_method="auto",
                          client=SimpleNamespace(chat=chat))
    assert result["verdict"] == "inconclusive"


def test_review_deduplicates_contracts_without_merging_evidence_or_visibility():
    shared = {"agents": [{"id": "a", "system": "Keep the original schema"}], "input_modalities": ["video"]}
    different = {**shared, "input_modalities": ["image"]}
    evidence = [
        {"session_id": f"s{i}", "_summary": f"observation {i}", "_source_contract": dict(shared)}
        for i in range(16)
    ] + [{"session_id": "other", "_source_contract": different}, {"session_id": "missing"}]
    payload = review_input(review_job(), evidence, "auto")
    assert len(payload["source_contracts"]) == 2
    assert len(payload["evidence"]) == 18
    refs = payload["evidence"]
    assert len({e["source_contract_ref"] for e in refs[:16]}) == 1
    assert refs[16]["source_contract_ref"] != refs[0]["source_contract_ref"]
    assert refs[17]["source_contract_ref"] is None
    assert [e["id"] for e in refs] == [e["session_id"] for e in evidence]
    assert [e["summary"] for e in refs[:16]] == [f"observation {i}" for i in range(16)]
    assert all("source_contract" not in e for e in refs)
    assert payload == review_input(review_job(), evidence, "auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [45, 600, 900])
async def test_assessment_passes_configured_total_budget_to_transport(seconds):
    async def chat(messages, **kwargs):
        assert kwargs["total_timeout_seconds"] == seconds
        return json.dumps(response())

    await assess(review_job(), evidence=[{"session_id": "source"}], requested_method="auto",
                 client=SimpleNamespace(chat=chat), timeout_seconds=seconds)


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
