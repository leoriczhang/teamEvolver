"""Real PostgreSQL: recover an exact rolled-back publication without erasing its costs or history."""
from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import BundleLimits, SkillsModuleConfig
from team_skills.evolution.repository import EvolutionRepository
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.holdout import candidate_validation_policy
from team_skills.execution.publication import publish_candidate
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryError, SkillNotFound
from teamEvolver.runtime import build_services, process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


def seed(env):
    async def setup(conn):
        raw = profile_raw()
        raw["affected_skill_ids"] = []
        raw["environment"]["validation"] = {"max_total_tokens": 1000}
        _, baseline = await ExecutionRepository(env.tx).register(
            conn, tenant_id="default", raw=raw, skill_dependencies=[], actor="test",
        )
        context = {"candidate_kind": "execution", "execution_profile_id": "instructor",
                   "base_release_id": baseline["release_id"],
                   "baseline_execution_plan": baseline["document"]["execution_plan"],
                   "affected_skill_ids": [], "skill_dependencies": []}
        detail = attach_execution_proposal(
            {"component_changes": [component_change()]}, {}, context=context,
            allowed_modules=["prompt"], working_bundle={}, skill_changed=False,
        )["_execution_detail"]
        detail.update(generation_cost={"source": "test-only-fixture", "total_tokens": 17},
                      independent_validation={"status": "passed", "evidence_ref": "test-only"},
                      validation_plan={"input_hash": "test-only"})
        repo = CandidateRepository(env.tx)
        item = await repo.create(conn, tenant_id="default", skill_name=None, bundle={},
                                 limits=BundleLimits(), detail=detail, actor="test")
        await repo.set_marks(conn, tenant_id="default", candidate_id=item["candidate_id"],
                             changes={"reviewed": True}, actor="test")
        # A retained historical command proves recovery never resets this Candidate's cost lineage.
        history = EvolutionRepository(env.tx)
        old, _ = await history.accept(
            conn, tenant_id="default", command_id="test-historical-validation", kind="skill.validate",
            request={"test": True}, payload={"candidate_id": item["candidate_id"], "revision": 1,
                                            "detail": copy.deepcopy(detail)},
        )
        await history.finish(conn, old["command_id"], {"status": "evaluated", "test_only_tokens": 23})
        result = await publish_candidate(
            conn, env.tx, tenant_id="default", payload={"candidate_id": item["candidate_id"], "revision": 1,
                "detail": detail, "base_version": 0, "skill_name": None, "actor": "test", "command_id": "test-run"},
            bundle={}, baseline={}, config=SkillsModuleConfig(1, {}),
        )
        published = await repo.transition(
            conn, tenant_id="default", candidate_id=item["candidate_id"], to_status="published",
            actor="test", expected_revision=1, decision=result, mutation_id=result["release_id"],
        )
        return published, baseline, result
    return _run(env, "default", setup)


def rollback(client, env, original, published):
    response = client.post("/api/skill-evolution/execution-profiles/instructor/rollback", headers=H, json={
        "release_id": original["release_id"], "expected_release_id": published["release_id"],
    })
    assert response.status_code == 202, response.text
    services = build_services(role="worker", tx=env.tx, handlers=register_handlers())
    env.tx.pool.run(process_once(services, owner_id="rollback-test", tenants=("default",)))
    command = _run(env, "default", lambda conn: EvolutionRepository(env.tx).command(conn, response.json()["job_id"]))
    assert command["result"]["status"] == "rolled_back"
    return command["result"]["release_id"]


def body(head, revision=1):
    return {"candidate_revision": revision, "expected_release_id": head,
            "component_changes": [component_change("A corrected Prompt proposal")],
            "reason": "Independent review found a real violation; retain the publication and all costs"}


def test_recovery_requires_exact_completed_rollback_and_keeps_publication_and_cost_history(
    pg_env, tmp_path, monkeypatch,
):
    item, original, published = seed(pg_env)
    cid = item["candidate_id"]
    repo, releases = CandidateRepository(pg_env.tx), ExecutionRepository(pg_env.tx)
    with _pod(pg_env, tmp_path, monkeypatch, pod="recover-prompt") as client:
        _login(client, bootstrap=True)
        path = f"/api/skill-candidates/{cid}/resume-after-rollback"
        assert client.post(path, headers=H, json=body(published["release_id"])).status_code == 409
        restored = rollback(client, pg_env, original, published)
        assert client.post(path, headers=H, json=body(restored, revision=2)).status_code == 409
        assert client.post(path, headers=H, json=body(published["release_id"])).status_code == 409
        response = client.post(path, headers=H, json=body(restored))
        assert response.status_code == 200, response.text
        assert response.json()["candidate_revision"] == 2
        assert client.post(path, headers=H, json=body(restored)).status_code == 409
    changed = _run(pg_env, "default", lambda conn: repo.require(conn, cid))
    assert changed["status"] == "open" and changed["revision"] == 2
    assert not any(changed[key] for key in ("reviewed", "adopted", "rejected", "mutation_id", "validate_task_id"))
    assert changed["decided_at"] is None and changed["decision"] == {} and changed["evaluation"] == {}
    assert changed["detail"]["generation_cost"] == item["detail"]["generation_cost"]
    assert not {"independent_validation", "validation_plan"} & changed["detail"].keys()
    assert changed["detail"]["base_release_id"] == restored
    assert changed["detail"]["candidate_execution_plan"]["components"][0]["revision"] == 4
    retained = _run(pg_env, "default", lambda conn: releases.release(conn, published["release_id"]))
    assert retained["document"]["execution_plan"]["components"][0]["content"] == "Improved business Prompt"
    old = _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(
        conn, "test-historical-validation"))
    assert old["result"] == {"status": "evaluated", "test_only_tokens": 23}
    assert old["payload"]["detail"] == item["detail"]
    events = _run(pg_env, "default", lambda conn: repo.events(conn, cid))
    recovery = [e for e in events if e["detail"].get("event_type") == "resume_after_rollback"]
    assert len(recovery) == 1 and recovery[0]["detail"]["published_release_id"] == published["release_id"]
    assert recovery[0]["detail"]["rollback_release_id"] == restored
    with pytest.raises(CandidateStateError):
        _run(pg_env, "default", lambda conn: repo.transition(
            conn, tenant_id="default", candidate_id=cid, to_status="open", actor="test",
        ))


def test_recovery_refuses_rollback_to_a_different_composition_and_another_live_candidate(
    pg_env, tmp_path, monkeypatch,
):
    item, original, published = seed(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="recovery-conflicts") as client:
        _login(client, bootstrap=True)
        # A rollback to the just-published content is a real rollback command, but not the original baseline.
        restored = rollback(client, pg_env, {"release_id": published["release_id"]}, published)
        response = client.post(f"/api/skill-candidates/{item['candidate_id']}/resume-after-rollback",
                               headers=H, json=body(restored))
        assert response.status_code == 409, response.text
        # A second rollback is not the immediate rollback of this Candidate's publication either.
        restored = rollback(client, pg_env, original, {"release_id": restored})
        assert client.post(f"/api/skill-candidates/{item['candidate_id']}/resume-after-rollback",
                           headers=H, json=body(restored)).status_code == 409


def test_live_proposal_prevents_reopening_a_rolled_back_publication(pg_env, tmp_path, monkeypatch):
    item, original, published = seed(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="recovery-live") as client:
        _login(client, bootstrap=True)
        restored = rollback(client, pg_env, original, published)
        _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).create(
            conn, tenant_id="default", skill_name=None, bundle={}, limits=BundleLimits(),
            detail=item["detail"], actor="another-cycle",
        ))
        response = client.post(f"/api/skill-candidates/{item['candidate_id']}/resume-after-rollback",
                               headers=H, json=body(restored))
        assert response.status_code == 409 and "another live" in response.text


@pytest.mark.parametrize("maximum", [1500, None])
def test_explicit_budget_change_retains_execution_bytes_and_old_costs_and_requires_new_review(
    pg_env, tmp_path, monkeypatch, maximum,
):
    item, original, published = seed(pg_env)
    cid = item["candidate_id"]
    with _pod(pg_env, tmp_path, monkeypatch, pod="budget-cas") as client:
        _login(client, bootstrap=True)
        path = f"/api/skill-candidates/{cid}/validation-budget"
        request = {"candidate_revision": 2, "max_total_tokens": maximum,
                   "reason": "The test owner explicitly authorized the cumulative increase"}
        assert client.post(path, headers=H, json={**request, "candidate_revision": 1}).status_code == 409
        restored = rollback(client, pg_env, original, published)
        assert client.post(f"/api/skill-candidates/{cid}/resume-after-rollback",
                           headers=H, json=body(restored)).status_code == 200
        before = _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).require(conn, cid))
        history = _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(
            conn, "test-historical-validation"))
        assert client.post(path, headers={**H, "X-Tenant-Id": "beta"}, json=request).status_code == 403
        assert client.post(path, headers=H, json={**request, "reason": ""}).status_code == 400
        assert client.post(path, headers=H, json={k: v for k, v in request.items()
                                                if k != "max_total_tokens"}).status_code == 400
        for revision in (True, None, 0, "2"):
            assert client.post(path, headers=H, json={**request, "candidate_revision": revision}).status_code == 400
        if maximum is None:
            with ThreadPoolExecutor(max_workers=2) as executor:
                pending = [executor.submit(client.post, path, headers=H, json=request) for _ in range(2)]
                responses = [future.result(timeout=10) for future in pending]
            assert sorted(value.status_code for value in responses) == [200, 409]
            response = next(value for value in responses if value.status_code == 200)
        else:
            response = client.post(path, headers=H, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["validation_budget"]["max_total_tokens"] == maximum
        assert client.post(path, headers=H, json=request).status_code == 409
        for amount in (True, 1500, 999, 10_000_001, "2000", *((None,) if maximum is None else ())):
            response = client.post(path, headers=H, json={
                **request, "candidate_revision": 3, "max_total_tokens": amount})
            assert response.status_code == 400, response.text
    after = _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).require(conn, cid))
    assert after["revision"] == 3 and not after["reviewed"]
    for key in ("generation_cost", "baseline_execution_plan", "candidate_execution_plan"):
        assert after["detail"][key] == before["detail"][key]
    policy = candidate_validation_policy(after["detail"])
    assert policy["max_total_tokens"] == maximum
    assert policy["repetitions"] == 2 and policy["max_extra_runs"] == 5
    events = _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).events(conn, cid))
    event_type = "validation_budget_unlimited" if maximum is None else "validation_budget_increased"
    assert events[-1]["detail"]["event_type"] == event_type
    assert events[-1]["detail"]["previous_max_total_tokens"] == 1000
    assert events[-1]["detail"]["max_total_tokens"] == maximum
    assert _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(
        conn, "test-historical-validation")) == history


def test_recovery_is_revision_guarded_under_concurrency_and_tenant_private(pg_env, tmp_path, monkeypatch):
    item, original, published = seed(pg_env)
    cid = item["candidate_id"]
    with _pod(pg_env, tmp_path, monkeypatch, pod="recover-concurrent") as client:
        _login(client, bootstrap=True)
        restored = rollback(client, pg_env, original, published)
        path = f"/api/skill-candidates/{cid}/resume-after-rollback"
        assert client.post(path, headers={**H, "X-Tenant-Id": "beta"}, json=body(restored)).status_code == 403
        with pytest.raises(SkillNotFound):
            _run(pg_env, "beta", lambda conn: CandidateRepository(pg_env.tx).require(conn, cid))
        for revision in (None, True, "1", 0):
            assert client.post(path, headers=H, json=body(restored, revision)).status_code == 400
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(client.post, path, headers=H, json=body(restored)) for _ in range(2)]
            responses = [future.result(timeout=10) for future in futures]
        assert sorted(r.status_code for r in responses) == [200, 409]
    retained = _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).validation_cost_runs(
        conn, cid, excluding=""))
    assert [r["command_id"] for r in retained] == ["test-historical-validation"]
    assert _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).require(conn, cid))["revision"] == 2


@pytest.mark.parametrize("override", [
    {}, {"max_total_tokens": 1500},
    {"max_total_tokens": True, "previous_max_total_tokens": 1000, "reason": "test"},
    {"max_total_tokens": 1500, "previous_max_total_tokens": 999, "reason": "test"},
    {"max_total_tokens": 1500, "previous_max_total_tokens": 1000, "reason": ""},
])
def test_malformed_budget_authority_fails_closed(override):
    with pytest.raises(SkillLibraryError):
        candidate_validation_policy({"candidate_execution_plan": {"environment": {
            "validation": {"max_total_tokens": 1000}}}, "validation_budget": override})
