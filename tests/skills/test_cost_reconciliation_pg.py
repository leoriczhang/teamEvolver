"""Append-only reconciliation uses current CAS while binding a separate old revision."""

from __future__ import annotations

import asyncio
import copy

import pytest

from team_replay._util import stable_hash
from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import BundleLimits
from team_skills.evolution.history import candidate_payload, feedback
from team_skills.execution.costs import budget_gate
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.reconciliation import (
    historical_cost_context,
    historical_zero_receipts,
    record_cost_reconciliation,
)
from team_skills.execution.repository import ExecutionRepository
from team_skills.library.repository import SkillLibraryError
from tests.support.cost_reconciliation import (
    ADAPTER_SOURCE,
    approve,
    cost_context,
    fixture_context,
    known_output,
    reviewed_proof,
    usage,
)
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def seed(env):
    async def setup(conn):
        raw = profile_raw("cost-customer")
        raw["affected_skill_ids"] = []
        _, release = await ExecutionRepository(env.tx).register(
            conn,
            tenant_id="alpha",
            raw=raw,
            skill_dependencies=[],
            actor="fixture",
        )
        proposal = attach_execution_proposal(
            {"component_changes": [component_change()]},
            {},
            context={
                "candidate_kind": "execution",
                "execution_profile_id": raw["profile_id"],
                "base_release_id": release["release_id"],
                "baseline_execution_plan": release["document"]["execution_plan"],
                "affected_skill_ids": [],
                "skill_dependencies": [],
            },
            allowed_modules=["prompt"],
            working_bundle={},
            skill_changed=False,
        )["_execution_detail"]
        repo = CandidateRepository(env.tx)
        candidate = await repo.create(
            conn, tenant_id="alpha", skill_name=None, bundle={}, limits=BundleLimits(), detail=proposal, actor="fixture"
        )
        for _ in range(3):
            candidate = await repo.revise_detail(
                conn,
                tenant_id="alpha",
                candidate_id=candidate["candidate_id"],
                detail=proposal,
                actor="fixture",
                expected_revision=candidate["revision"],
            )
        await ReplayAdapterRepository(env.tx).save(
            conn, tenant_id="alpha", filename="reconciliation.py", source=ADAPTER_SOURCE, expected_revision=0
        )
        context = fixture_context(candidate_id=candidate["candidate_id"])
        replay = ReplayRepository(env.tx)
        for key in ("main", "probe"):
            run = context[key]
            await replay.accept(
                conn,
                tenant_id="alpha",
                domain="skill",
                run_id=run["run_id"],
                subject_id=raw["profile_id"],
                request={"fixture": key},
                payload=run["input"],
            )
            if key == "probe":
                for case in run["input"]["job"]["replay_cases"]:
                    await replay.save_case(
                        conn,
                        tenant_id="alpha",
                        domain="skill",
                        run_id=run["run_id"],
                        case=case,
                        result=context["checkpoints"][case["case_id"]],
                    )
            await replay.finish(conn, "skill", run["run_id"], run["result"])
        return candidate

    return _run(env, "alpha", setup)


def load(env, cid):
    return _run(
        env,
        "alpha",
        lambda conn: historical_cost_context(
            conn,
            env.tx,
            candidate_id=cid,
            run_id="validate-old",
        ),
    )


def audit_request(env, cid):
    context = load(env, cid)
    candidate = context["candidate"]
    events = _run(env, "alpha", lambda c: CandidateRepository(env.tx).events(c, cid))
    proof, files = reviewed_proof(context)
    return dict(
        tenant_id="alpha",
        candidate_id=cid,
        expected_revision=candidate["revision"],
        expected_state_hash=CandidateRepository.cost_state_hash(candidate),
        expected_event_seq=events[-1]["seq"],
        reconciliation_id="reviewed-old-aa",
        proof=proof,
        evidence_files=files,
        actor="independent-admin",
        approve_review=approve(proof),
    )


def record(env, request):
    return _run(env, "alpha", lambda c: record_cost_reconciliation(c, env.tx, **request))


def test_append_only_idempotent_cost_audit_preserves_assets_results_marks_and_ui_history(pg_env):
    candidate = seed(pg_env)
    cid = candidate["candidate_id"]
    candidate = _run(
        pg_env,
        "alpha",
        lambda c: CandidateRepository(pg_env.tx).set_marks(
            c,
            tenant_id="alpha",
            candidate_id=cid,
            changes={"reviewed": True},
            actor="human-reviewer",
        ),
    )
    before = load(pg_env, cid)
    assert candidate["revision"] == 4 and before["main"]["input"]["job"]["candidate_revision"] == 3
    events_before = _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).events(c, cid))
    request = audit_request(pg_env, cid)
    result = record(pg_env, request)
    assert record(pg_env, request) == result  # stale event CAS is harmless only for an identical retry
    after = load(pg_env, cid)
    assert after == before
    events = _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).events(c, cid))
    assert len(events) == len(events_before) + 1
    event = events[-1]
    assert event["kind"] == "feedback" and event["detail"]["event_type"] == "cost_reconciliation"
    assert event["revision"] == 4 and event["detail"]["proof"]["candidate_revision"] == 3
    assert set(event["detail"]["proof"]["evidence"]) == set(request["evidence_files"])
    assert all(isinstance(m["sha256"], str) for m in event["detail"]["proof"]["evidence"].values())
    old_feedback, new_feedback = feedback(candidate, events_before), feedback(candidate, events)
    assert old_feedback["history"] == new_feedback["history"]
    assert len(new_feedback["history"]) == 1 and new_feedback["history"][0]["field"] == "reviewed"
    assert new_feedback["reviewed"] is True
    assert new_feedback["version"] == result["event_seq"]
    payload = _run(pg_env, "alpha", lambda c: candidate_payload(c, pg_env.tx, candidate))
    assert payload["candidate_revision"] == 4 and payload["feedback"]["history"] == new_feedback["history"]
    assert payload["candidate_kind"] == "execution" and payload["candidate_skill"] is None
    receipts = _run(
        pg_env, "alpha", lambda c: historical_zero_receipts(c, pg_env.tx, candidate_id=cid, run_id="validate-old")
    )
    assert len(receipts) == 4 and all(r.event_seq == result["event_seq"] for r in receipts)
    prior = {
        **after["main"]["result"]["replay"],
        "generation_cost": usage("generation", 194541),
        "_cost_context": cost_context(after),
    }
    current = known_output("new", left=3, right=7, judge=11)
    gate = budget_gate(
        {
            "generation_cost": usage("generation", 194541),
            "validation_plan": {"probe_policy": {"max_total_tokens": 1000000}},
        },
        current,
        prior_evaluations=[prior],
        zero_dispatch_receipts=receipts,
    )
    assert gate["known_total_tokens"] == 241741 and gate["status"] == "passed"
    assert after["main"]["result"]["replay"]["probe_gate"]["status"] == "inconclusive"


@pytest.mark.parametrize("change", ["asset_revision", "state_hash", "event_seq", "old_run", "known_dispatch"])
def test_stale_or_wrong_evidence_is_rejected_without_audit_or_asset_mutation(pg_env, change):
    candidate = seed(pg_env)
    cid = candidate["candidate_id"]
    request = audit_request(pg_env, cid)
    before = load(pg_env, cid)
    events = _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).events(c, cid))
    if change == "asset_revision":
        request["expected_revision"] = 3
    elif change == "state_hash":
        request["expected_state_hash"] = "not-current"
    elif change == "event_seq":
        request["expected_event_seq"] -= 1
    elif change == "old_run":
        request["proof"]["run_id"] = "another-run"
    else:
        request["proof"]["branches"][0]["observation_hash"] = "known-dispatch-cannot-match"
    request["approve_review"] = approve(request["proof"])
    with pytest.raises((SkillLibraryError, ValueError)):
        record(pg_env, request)
    assert load(pg_env, cid) == before
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).events(c, cid)) == events


def test_different_body_same_id_conflicts_and_concurrent_new_events_serialize(pg_env):
    cid = seed(pg_env)["candidate_id"]
    request = audit_request(pg_env, cid)
    record(pg_env, request)
    conflict = copy.deepcopy({k: v for k, v in request.items() if k != "approve_review"})
    conflict["proof"]["review"]["notes"] += " Another approved review."
    conflict["approve_review"] = approve(conflict["proof"])
    with pytest.raises(CandidateStateError, match="different proof"):
        record(pg_env, conflict)
    request = audit_request(pg_env, cid)
    second = {**request, "reconciliation_id": "second-review"}
    third = {**request, "reconciliation_id": "third-review"}

    async def compete():
        async def one(body):
            async with pg_env.tx.tenant_tx("alpha") as conn:
                return await record_cost_reconciliation(conn, pg_env.tx, **body)

        return await asyncio.gather(one(second), one(third), return_exceptions=True)

    results = pg_env.tx.pool.run(compete())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, CandidateStateError) for r in results) == 1
    assert load(pg_env, cid)["candidate"]["revision"] == 4


def test_tenant_isolation_and_nonmatching_run_never_consumes_a_receipt(pg_env):
    cid = seed(pg_env)["candidate_id"]
    request = audit_request(pg_env, cid)
    record(pg_env, request)
    assert _run(pg_env, "beta", lambda c: CandidateRepository(pg_env.tx).cost_reconciliations(c, cid)) == []
    assert (
        _run(pg_env, "beta", lambda c: historical_zero_receipts(c, pg_env.tx, candidate_id=cid, run_id="validate-old"))
        == []
    )
    assert (
        _run(pg_env, "alpha", lambda c: historical_zero_receipts(c, pg_env.tx, candidate_id=cid, run_id="other-run"))
        == []
    )
    with pytest.raises(SkillLibraryError):
        _run(pg_env, "beta", lambda c: record_cost_reconciliation(c, pg_env.tx, **{**request, "tenant_id": "beta"}))
    changed = {**request, "proof": {**request["proof"], "candidate_id": "another-candidate"}}
    with pytest.raises(SkillLibraryError):
        record(pg_env, changed)
    assert stable_hash(load(pg_env, cid)["main"]["result"]) == request["proof"]["result_hash"]
