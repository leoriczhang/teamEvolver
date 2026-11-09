"""Cost reconciliation verifies immutable observations, never retroactively passes Replay."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace

import pytest

from team_replay._util import stable_hash
from team_skills.execution.costs import budget_gate
from team_skills.execution.reconciliation import verify_cost_reconciliation
from team_skills.library.repository import SkillLibraryError
from tests.support.cost_reconciliation import (
    approve,
    cost_context,
    fixture_context,
    known_output,
    reviewed_proof,
    usage,
)


def verify(context, proof, files, **kwargs):
    return verify_cost_reconciliation(proof, context=context, evidence_files=files,
                                      actor="independent-admin", approve_review=approve(proof), **kwargs)


def old_cost(context):
    return {**copy.deepcopy(context["main"]["result"]["replay"]),
            "generation_cost": usage("generation", 194541), "_cost_context": cost_context(context)}


def current_cost():
    return {"generation_cost": usage("generation", 194541),
            "validation_plan": {"probe_policy": {"max_total_tokens": 1000000}}}, known_output(
                "new", left=3, right=7, judge=11)


def test_verified_pair_abort_only_explains_old_agent_unknown_and_preserves_all_known_cost():
    context = fixture_context()
    proof, files = reviewed_proof(context)
    before = copy.deepcopy(context)
    receipts = [replace(r, event_seq=2) for r in verify(context, proof, files)]
    detail, current = current_cost()
    prior = old_cost(context)
    unknown = budget_gate(detail, current, prior_evaluations=[prior])
    assert unknown["status"] == "inconclusive" and unknown["known_total_tokens"] == 241741
    result = budget_gate(detail, current, prior_evaluations=[prior], zero_dispatch_receipts=receipts)
    assert result["status"] == "passed" and result["known_total_tokens"] == 241720 + 21
    assert result["agent_replay_tokens"] == 30103 + 10
    assert len(result["historical_zero_dispatch_reconciliations"]) == 4
    assert context == before
    assert prior["probe_gate"]["status"] == current["runtime_gate"]["status"] == "inconclusive"
    assert prior["probe_gate"]["runs"][0]["result"]["runtime_gate"]["status"] == "unsupported"
    assert all(r.phase == "pair_open_aborted" for r in receipts)


@pytest.mark.parametrize("field", ["tenant_id", "candidate_id", "candidate_revision", "run_id", "input_hash",
                                   "result_hash", "probe_run_id", "probe_input_hash", "probe_result_hash",
                                   "adapter_id", "adapter_revision", "adapter_hash"])
def test_wrong_immutable_binding_is_rejected(field):
    context = fixture_context()
    proof, files = reviewed_proof(context)
    proof[field] = "different"
    with pytest.raises(SkillLibraryError, match="binding mismatch"):
        verify(context, proof, files)


@pytest.mark.parametrize("field", ["case_id", "branch", "case_hash", "observation_hash", "phase"])
def test_every_branch_and_phase_must_match_old_checkpoint(field):
    context = fixture_context()
    proof, files = reviewed_proof(context)
    proof["branches"][0][field] = "wrong"
    with pytest.raises(SkillLibraryError):
        verify(context, proof, files)


@pytest.mark.parametrize("field,value", [
    ("interactions", [{"message": "sent"}]), ("observed_requests", [{"dispatch_id": "known"}]),
    ("execution_receipts", [{"id": "known"}]), ("messages", [{"content": "partial"}]),
    ("total_tokens", 0), ("api_calls", 1), ("status", "timeout"), ("completed", True),
])
def test_known_dispatch_partial_or_timeout_can_never_be_zeroed(field, value):
    context = fixture_context()
    case = context["probe"]["result"]["cases"][0]
    case["baseline"][field] = value
    context["checkpoints"][case["case_id"]]["cases"] = [copy.deepcopy(case)]
    context["main"]["result"]["replay"]["probe_gate"]["runs"][0]["result"] = copy.deepcopy(context["probe"]["result"])
    proof, files = reviewed_proof(context)
    with pytest.raises(SkillLibraryError, match="known dispatch|not a pre-execution"):
        verify(context, proof, files)


@pytest.mark.parametrize("mutation", ["self_assertion", "no_bytes", "changed_bytes", "missing_source",
                                      "duplicate_http", "send_endpoint", "outside_review", "no_checkpoint"])
def test_regex_or_review_boolean_alone_does_not_establish_zero(mutation):
    context = fixture_context()
    proof, files = reviewed_proof(context)
    if mutation == "self_assertion":
        with pytest.raises(SkillLibraryError, match="trusted approval"):
            verify_cost_reconciliation(proof, context=context, evidence_files=files, actor="independent-admin")
        return
    if mutation == "no_bytes":
        files = None
    elif mutation == "changed_bytes":
        files["runtime_before"] += b"send_model()\n"
    elif mutation == "missing_source":
        proof["evidence"].pop("engine_after")
    elif mutation == "duplicate_http":
        proof["http_events"][1] = copy.deepcopy(proof["http_events"][0])
    elif mutation == "send_endpoint":
        proof["http_events"][0]["path"] += "/messages"
    elif mutation == "outside_review":
        proof["evidence"]["http_log"]["line_start"] = 2
    elif mutation == "no_checkpoint":
        context["checkpoints"].pop("aa-one")
    with pytest.raises(SkillLibraryError):
        verify(context, proof, files)


@pytest.mark.parametrize("change", ["dict", "not_persisted", "tenant", "run", "result", "observation",
                                   "primary", "partial_receipts", "current_probe", "unknown_te", "worker_retry"])
def test_receipts_cannot_cross_run_tenant_phase_or_mask_other_unknown_cost(change):
    context = fixture_context()
    proof, files = reviewed_proof(context)
    receipts = [replace(r, event_seq=2) for r in verify(context, proof, files)]
    detail, current = current_cost()
    prior = old_cost(context)
    kwargs = {}
    if change == "dict":
        receipts = [asdict(r) for r in receipts]
    elif change == "not_persisted":
        receipts = [replace(r, event_seq=0) for r in receipts]
    elif change in {"tenant", "run", "result"}:
        key = {"tenant": "tenant_id", "run": "run_id", "result": "result_hash"}[change]
        prior["_cost_context"][key] = "another"
    elif change == "observation":
        prior["probe_gate"]["runs"][0]["result"]["cases"][0]["baseline"]["error"] = "modified 429"
    elif change == "primary":
        prior["cases"] = copy.deepcopy(prior["probe_gate"]["runs"][0]["result"]["cases"])
    elif change == "partial_receipts":
        receipts.pop()
    elif change == "current_probe":
        current["probe_gate"] = copy.deepcopy(prior["probe_gate"])
    elif change == "unknown_te":
        prior["te_llm_usage"]["complete"] = False
    elif change == "worker_retry":
        kwargs["worker_attempt"] = 2
    result = budget_gate(detail, current, prior_evaluations=[prior], zero_dispatch_receipts=receipts, **kwargs)
    assert result["status"] == "inconclusive"
    assert "total_model_cost_unknown" in result["reasons"] or change == "worker_retry"


def test_proof_hash_approval_is_exact_and_evidence_bytes_are_not_stored_in_proof():
    context = fixture_context()
    proof, files = reviewed_proof(context)
    callback = approve(proof)
    proof["review"]["notes"] += " Changed after approval."
    with pytest.raises(SkillLibraryError, match="trusted approval"):
        verify_cost_reconciliation(proof, context=context, evidence_files=files,
                                   actor="independent-admin", approve_review=callback)
    assert stable_hash(files["http_log"].decode()) != stable_hash(proof)


def test_known_aggregation_window_is_verified_against_checkpoint_without_relaxing_branch_bytes():
    context = fixture_context()
    aa = context["probe"]["result"]
    aa["window_results"] = {}
    for pair in aa["cases"]:
        cid = pair["case_id"]
        aa["window_results"]["recent:" + cid] = copy.deepcopy(context["checkpoints"][cid])
        pair["evidence_window"] = "recent:" + cid
    context["main"]["result"]["replay"]["probe_gate"]["runs"][0]["result"] = copy.deepcopy(aa)
    proof, files = reviewed_proof(context)
    assert len(verify(context, proof, files)) == 4
    context["checkpoints"]["aa-one"]["cases"][0]["baseline"]["error"] += " Changed checkpoint"
    with pytest.raises(SkillLibraryError, match="window differs"):
        verify(context, proof, files)
