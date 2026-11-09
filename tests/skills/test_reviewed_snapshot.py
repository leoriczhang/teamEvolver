"""A review may close its missing-declaration gate, never repair other validation failures."""
from __future__ import annotations

import copy

import pytest

from team_skills.execution.reviewed_publication import candidate_snapshot, reviewed_gate


def reviewed():
    identity = {"validation_run_id": "reviewed-run", "validation_payload_hash": "a" * 64,
                "validation_result_hash": "b" * 64}
    frozen = {"candidate_execution_plan": {"components": []}, "actual_modules": ["prompt"],
              "affected_skill_ids": [], "replay_cases": [{"case_id": "locked-1", "skill_ids": []}],
              "validation_plan": {"input_hash": "c" * 64, "holdout": {"status": "ready"}}}
    declaration = {"schema_version": "teamevolver.independent-validation.v2", "status": "passed",
                   "evidence_ref": "test-only:reviewed-original-output", "candidate_revision": 1,
                   "validation_input_hash": "c" * 64, "case_ids": ["locked-1"], **identity}
    item = {"revision": 1, "detail": {**copy.deepcopy(frozen), "independent_validation": declaration}}
    saved = {**identity, "payload": {"detail": frozen}, "evaluation": {
        "validation_verdict": "accept", "static_validation": {"passed": True},
        "recommended_publish": False, "publication_gate": {"passed": False,
            "reasons": ["independent_validation_missing"]},
        "replay": {"verdict": "inconclusive", **{key: {"status": "passed"} for key in (
            "runtime_gate", "execution_gate", "probe_gate", "cost_gate",
        )}},
    }}
    return item, saved


def test_current_independent_review_closes_only_missing_declaration_without_rewriting_original_result():
    item, saved = reviewed()
    original = copy.deepcopy((item, saved))

    detail, gate = reviewed_gate(item, saved)

    assert gate == {"passed": True, "reasons": []}
    assert detail["independent_validation"] == item["detail"]["independent_validation"]
    assert (item, saved) == original
    assert saved["evaluation"]["replay"]["verdict"] == "inconclusive"


@pytest.mark.parametrize("verdict", [None, "reject", "inconclusive", "passed"])
def test_independent_pass_cannot_replace_missing_or_failed_original_machine_quality(verdict):
    item, saved = reviewed()
    saved["evaluation"]["validation_verdict"] = verdict

    assert "reviewed_validation_not_accepted" in reviewed_gate(item, saved)[1]["reasons"]


@pytest.mark.parametrize("status", [None, "inconclusive", "failed"])
def test_independent_pass_never_substitutes_for_original_runtime_evidence(status):
    item, saved = reviewed()
    saved["evaluation"]["replay"]["runtime_gate"]["status"] = status

    assert "runtime_validation_incomplete" in reviewed_gate(item, saved)[1]["reasons"]


def test_independent_pass_cannot_override_static_validation_failure():
    item, saved = reviewed()
    saved["evaluation"]["static_validation"]["passed"] = False

    assert "static_validation_failed" in reviewed_gate(item, saved)[1]["reasons"]


def test_asset_snapshot_excludes_later_review_but_includes_candidate_budget_authority_and_asset_bytes():
    item = {"candidate_id": "same-candidate", "revision": 8, "detail": {
        "candidate_execution_plan": {"environment": {"validation": {"max_total_tokens": 1000000}}},
        "validation_budget": {"max_total_tokens": 1500000, "previous_max_total_tokens": 1000000,
                              "reason": "Explicit user authorization"},
    }}
    snapshot = candidate_snapshot(item)
    item["detail"]["independent_validation"] = {"status": "passed", "validation_run_id": "real-run"}
    assert candidate_snapshot(item) == snapshot
    item["detail"]["validation_budget"]["max_total_tokens"] = 1600000
    assert candidate_snapshot(item) != snapshot
    item["detail"]["validation_budget"]["max_total_tokens"] = 1500000
    item["detail"]["candidate_execution_plan"]["environment"]["validation"]["max_total_tokens"] = 1500000
    assert candidate_snapshot(item) != snapshot
