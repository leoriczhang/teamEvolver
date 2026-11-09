"""Independent review authorizes one frozen Replay result, never a fresh run."""

from __future__ import annotations

import copy

import pytest

from team_skills.execution.candidates import publication_gate


@pytest.fixture
def reviewed_validation():
    trusted = {
        "candidate_revision": 7,
        "validation_run_id": "validate:reviewed-run",
        "validation_payload_hash": "a" * 64,
        "validation_result_hash": "b" * 64,
    }
    detail = {
        "candidate_execution_plan": {"components": [{"component_id": "business-prompt"}]},
        "actual_modules": ["prompt"],
        "affected_skill_ids": ["demo", "helper"],
        "replay_cases": [
            {"case_id": "case-current", "skill_ids": ["demo"]},
            {"case_id": "case-locked", "skill_ids": ["helper"]},
        ],
        "validation_plan": {"input_hash": "c" * 64, "holdout": {"status": "ready"}},
        "independent_validation": {
            "schema_version": "teamevolver.independent-validation.v2",
            "status": "passed",
            "evidence_ref": "artifact:reviewed-replay-result",
            **trusted,
            "validation_input_hash": "c" * 64,
            "case_ids": ["case-current", "case-locked"],
        },
    }
    evaluation = {
        "runtime_gate": {"status": "passed"},
        "execution_gate": {"status": "passed"},
        "probe_gate": {"status": "passed"},
        "cost_gate": {"status": "passed"},
    }
    return detail, evaluation, trusted


def test_matching_v2_review_authorizes_exact_frozen_result_without_mutating_inputs(reviewed_validation):
    detail, evaluation, trusted = reviewed_validation
    before = copy.deepcopy((detail, evaluation, trusted))

    assert publication_gate(detail, evaluation, **trusted) == {"passed": True, "reasons": []}

    assert (detail, evaluation, trusted) == before


@pytest.mark.parametrize("binding", [
    "validation_run_id", "validation_payload_hash", "validation_result_hash",
])
def test_same_inputs_do_not_authorize_a_different_run_payload_or_result(reviewed_validation, binding):
    detail, evaluation, trusted = reviewed_validation
    original_input_hash = detail["validation_plan"]["input_hash"]
    trusted[binding] = "validate:fresh-run" if binding == "validation_run_id" else "d" * 64
    before = copy.deepcopy((detail, evaluation, trusted))

    result = publication_gate(detail, evaluation, **trusted)

    assert result["passed"] is False
    assert result["reasons"]
    assert detail["validation_plan"]["input_hash"] == original_input_hash
    assert (detail, evaluation, trusted) == before


@pytest.mark.parametrize("binding", [
    "validation_run_id", "validation_payload_hash", "validation_result_hash",
])
@pytest.mark.parametrize("mode", ["missing", "empty", "none"])
def test_review_cannot_supply_its_own_missing_trusted_identity(reviewed_validation, binding, mode):
    detail, evaluation, trusted = reviewed_validation
    if mode == "missing":
        trusted.pop(binding)
    else:
        trusted[binding] = "" if mode == "empty" else None

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


@pytest.mark.parametrize("field", [
    "schema_version", "status", "evidence_ref", "candidate_revision", "validation_run_id",
    "validation_payload_hash", "validation_result_hash", "validation_input_hash", "case_ids",
])
def test_v2_review_requires_every_evidence_binding_and_case_reference(reviewed_validation, field):
    detail, evaluation, trusted = reviewed_validation
    detail["independent_validation"].pop(field)

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


@pytest.mark.parametrize("schema", [None, "", "teamevolver.independent-validation.v1"])
def test_old_or_unversioned_review_is_not_upgraded_by_matching_input_hash(reviewed_validation, schema):
    detail, evaluation, trusted = reviewed_validation
    detail["independent_validation"]["schema_version"] = schema

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


@pytest.mark.parametrize("status", ["failed", "inconclusive"])
def test_bound_review_still_requires_an_independent_pass(reviewed_validation, status):
    detail, evaluation, trusted = reviewed_validation
    detail["independent_validation"]["status"] = status

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


def test_review_from_another_candidate_revision_is_rejected(reviewed_validation):
    detail, evaluation, trusted = reviewed_validation
    detail["independent_validation"]["candidate_revision"] = 6

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


def test_review_must_bind_the_current_frozen_validation_inputs(reviewed_validation):
    detail, evaluation, trusted = reviewed_validation
    detail["validation_plan"]["input_hash"] = "e" * 64

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


@pytest.mark.parametrize("plan", [None, {}, {"holdout": {"status": "ready"}}])
def test_missing_frozen_input_hash_cannot_be_satisfied_by_a_review_claim(reviewed_validation, plan):
    detail, evaluation, trusted = reviewed_validation
    detail["validation_plan"] = plan

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


@pytest.mark.parametrize("case_ids", [[], ["case-current"], ["case-locked"], ["unrelated-case"]])
def test_review_must_cover_every_frozen_case(reviewed_validation, case_ids):
    detail, evaluation, trusted = reviewed_validation
    detail["independent_validation"]["case_ids"] = case_ids

    assert publication_gate(detail, evaluation, **trusted)["passed"] is False


@pytest.mark.parametrize(("gate", "reason"), [
    ("execution_gate", "execution_not_observed"),
    ("probe_gate", "execution_probes_incomplete"),
    ("cost_gate", "total_model_cost_unverified"),
])
@pytest.mark.parametrize("status", [None, "failed", "inconclusive"])
def test_independent_pass_cannot_override_machine_evidence_gates(reviewed_validation, gate, reason, status):
    detail, evaluation, trusted = reviewed_validation
    if status is None:
        evaluation.pop(gate)
    else:
        evaluation[gate]["status"] = status
    before = copy.deepcopy((detail, evaluation))

    result = publication_gate(detail, evaluation, **trusted)

    assert result["passed"] is False
    assert reason in result["reasons"]
    assert (detail, evaluation) == before


def test_independent_pass_cannot_override_missing_locked_acceptance(reviewed_validation):
    detail, evaluation, trusted = reviewed_validation
    detail["validation_plan"]["holdout"] = {"status": "not_ready"}

    result = publication_gate(detail, evaluation, **trusted)

    assert result["passed"] is False
    assert "locked_acceptance_missing" in result["reasons"]


def test_independent_pass_cannot_override_missing_affected_skill_coverage(reviewed_validation):
    detail, evaluation, trusted = reviewed_validation
    detail["replay_cases"][1]["skill_ids"] = []

    result = publication_gate(detail, evaluation, **trusted)

    assert result["passed"] is False
    assert "regression_coverage_missing" in result["reasons"]


def test_independent_pass_cannot_publish_a_noop_component_change(reviewed_validation):
    detail, evaluation, trusted = reviewed_validation
    detail["actual_modules"] = []

    result = publication_gate(detail, evaluation, **trusted)

    assert result["passed"] is False
    assert "no_component_changes" in result["reasons"]


@pytest.mark.parametrize("detail", [{}, {"candidate_execution_plan": None}])
def test_legacy_skill_candidate_keeps_existing_publication_gate_contract(detail):
    evaluation = {"execution_gate": {"status": "failed"}}
    before = copy.deepcopy((detail, evaluation))

    assert publication_gate(detail, evaluation) == {"passed": True, "reasons": []}

    assert (detail, evaluation) == before
