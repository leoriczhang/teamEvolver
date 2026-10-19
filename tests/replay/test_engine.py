"""Pure engine, strict Dataset and runtime compatibility tests."""

from __future__ import annotations

import copy

import pytest

from team_replay.contracts import ReplaySpec, ReplayTreatment
from team_replay.datasets.schema import DATASET_SCHEMA, DatasetSchemaError, normalize_document
from team_replay.runner import evaluate_spec
from team_replay.runtime_matrix import evaluate_runtime_compatibility
from tests.support.replay import Host, document, judge, spec


def test_engine_fake_host_pins_peer_skills_and_unsaved_files():
    before = spec()
    original = copy.deepcopy(before.as_dict())
    host = Host()
    result = evaluate_spec(before, host=host, judge=judge)
    assert result["verdict"] == "accept" and result["dataset_hash"] == "frozen"
    assert len(host.closed) == 2
    contexts = {context.treatment.branch: context for context in host.contexts}
    assert contexts["baseline"].request_id != contexts["candidate"].request_id
    candidate = contexts["candidate"].treatment.skill
    baseline = contexts["baseline"].treatment.skill
    assert candidate["skills"][0]["bundle"]["files"] != baseline["skills"][0]["bundle"]["files"]
    assert candidate["skills"][1] == baseline["skills"][1]
    assert before.as_dict() == original


def test_no_runtime_metrics_are_unavailable_and_inconclusive():
    result = evaluate_spec(spec(), host=Host(metrics=False), judge=judge)
    assert result["verdict"] == "inconclusive" and not result["accepted"]
    for branch in ("baseline", "candidate"):
        assert result["efficiency"][branch]["total_tokens"] == "unavailable"
        assert result["efficiency"][branch]["tool_call_count"] == "unavailable"


def test_memory_uses_same_engine_with_fixed_content_and_versions():
    source = spec()
    memory = ReplaySpec(
        run_id="memory-1",
        subject_kind="memory",
        subject_id="facts",
        dataset_format=DATASET_SCHEMA,
        cases=source.cases,
        baseline=ReplayTreatment("memory", "v1", {"uri": "viking://team/facts", "content": "Old"}),
        candidate=ReplayTreatment("memory", "v2", {"uri": "viking://team/facts", "content": "New"}),
        runtime={"type": "fake"},
        limits={"timeout_seconds": 30, "max_interactions": 4},
        metadata={"dataset_hash": "same"},
    )
    host = Host()
    result = evaluate_spec(memory, host=host, judge=judge)
    assert result["verdict"] == "accept"
    assert [c.context_snapshot["items"][0]["expanded"]["full"] for c in host.contexts] == ["Old", "New"]


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "team-replay.dataset.v2"},
        {"items": []},
        {"skill_name": "demo"},
        {"cases": [None]},
        {"skills": "demo"},
        {"skills": ["demo", "demo"]},
        {"metadata": []},
    ],
)
def test_dataset_rejects_old_or_malformed_document(changes):
    with pytest.raises(DatasetSchemaError):
        normalize_document({**document(), **changes}, allow_empty=True)


def test_empty_draft_is_valid_but_replay_requires_case():
    draft = normalize_document(document(empty=True), allow_empty=True)
    assert draft["cases"] == []
    with pytest.raises(DatasetSchemaError):
        normalize_document(draft)


def test_runtime_matrix_does_not_infer_runtime_or_accept_inconclusive():
    policy = {"required_runtimes": ["remote-agent"]}
    unknown = evaluate_runtime_compatibility(policy, [{"status": "evaluated", "accepted": True, "verdict": "accept"}])
    assert unknown["status"] != "passed"
    ambiguous = evaluate_runtime_compatibility(
        policy,
        [
            {"status": "evaluated", "runtime_type": "remote-agent", "accepted": True, "verdict": "inconclusive"},
        ],
    )
    assert ambiguous["status"] == "inconclusive"
    passed = {"status": "evaluated", "runtime_type": "remote-agent", "accepted": True, "verdict": "accept"}
    missing = {"status": "evaluated", "runtime_type": "remote-agent", "accepted": False, "verdict": "inconclusive"}
    for results in ([passed, missing], [missing, passed]):
        assert evaluate_runtime_compatibility(policy, results)["status"] == "inconclusive"
