"""Real isolated PG: release history reads stay immutable, tenant-scoped and permission checked."""
from __future__ import annotations

import copy
import os

import pytest
from fastapi.testclient import TestClient

from team_skills.candidates.repository import CandidateRepository
from team_skills.execution.repository import ExecutionRepository
from tests.support.app import H, _login, _pod
from tests.support.execution import profile_raw, seed_reviewed_execution
from tests.support.postgres import _run

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


@pytest.fixture
def published(pg_env):
    async def seed(conn):
        saved = await seed_reviewed_execution(conn, pg_env.tx)
        item, payload = saved["candidate"], saved["payload"]
        saved["release"] = await ExecutionRepository(pg_env.tx).publish(
            conn, tenant_id="default", profile_id="instructor", expected_release_id=saved["baseline"]["release_id"],
            document={"execution_plan": payload["detail"]["candidate_execution_plan"], "skill_dependencies": [],
                      "origin": "candidate-review"}, actor="test-only",
            candidate_id=item["candidate_id"], candidate_revision=1, validation_ref=saved["run_id"],
        )
        return saved
    return _run(pg_env, "default", seed)


def path(release, profile="instructor"):
    return f"/api/skill-evolution/execution-profiles/{profile}/releases/{release['release_id']}"


def test_historical_content_and_validation_survive_later_candidate_edits(pg_env, tmp_path, monkeypatch, published):
    async def later(conn):
        repo = CandidateRepository(pg_env.tx)
        old = published["candidate"]
        revised = await repo.revise_detail(
            conn, tenant_id="default", candidate_id=old["candidate_id"], expected_revision=1,
            detail={**old["detail"], "rationale": "A later revision must not replace the release's evidence"},
            actor="test",
        )
        await repo.transition(conn, tenant_id="default", candidate_id=old["candidate_id"],
                              to_status="validating", actor="test", expected_revision=2, validate_task_id="later-run")
        await repo.record_verdict(conn, tenant_id="default", candidate_id=old["candidate_id"], revision=2,
                                  verdict="reject", task_id="later-run",
                                  evaluation={"candidate_revision": 2, "validation_verdict": "reject"})
        return revised
    _run(pg_env, "default", later)
    with _pod(pg_env, tmp_path, monkeypatch, pod="release-history") as client:
        assert client.get(path(published["release"]), headers=H).status_code == 401
        _login(client, bootstrap=True)
        response = client.get(path(published["release"]), headers=H)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["release"]["document"] == published["release"]["document"]
        assert value["release"]["candidate_revision"] == 1
        assert value["validation"]["status"] == "available"
        assert value["validation"]["run_id"] == published["run_id"]
        assert value["validation"]["evaluation"]["validation_verdict"] == "accept"
        assert value["validation"]["evaluation"]["candidate_revision"] == 1
        frozen_baseline = published["payload"]["detail"]["baseline_execution_plan"]
        assert value["validation"]["baseline_execution_plan"] == frozen_baseline
        baseline = client.get(path(published["baseline"]), headers=H).json()
        assert baseline["validation"]["status"] == "not_applicable"
        assert baseline["validation"]["source_release_id"] is None
        assert baseline["validation"]["evaluation"] is None
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks")) == 0


def test_profile_and_tenant_ownership_missing_history_and_rollback_source(pg_env, tmp_path, monkeypatch, published):
    async def seed(conn):
        repo = ExecutionRepository(pg_env.tx)
        other = profile_raw("other-profile", prefix="other-")
        other["affected_skill_ids"] = []
        await repo.register(conn, tenant_id="default", raw=other, skill_dependencies=[], actor="test")
        profile, document = await repo.rollback_document(conn, "instructor", published["baseline"]["release_id"])
        return await repo.publish(
            conn, tenant_id="default", profile_id="instructor", expected_release_id=profile["current_release_id"],
            document=document, actor="test", candidate_id="test-rollback", candidate_revision=1,
            validation_ref="rollback:" + published["baseline"]["release_id"],
            restored_from=published["baseline"]["release_id"],
        )
    restored = _run(pg_env, "default", seed)
    with _pod(pg_env, tmp_path, monkeypatch, pod="release-ownership") as client:
        _login(client, bootstrap=True)
        assert client.get(path(published["release"], "other-profile"), headers=H).status_code == 404
        assert client.get(path(published["release"]), headers={"X-Tenant-Id": "alpha"}).status_code == 404
        assert client.get(path({"release_id": "missing"}), headers=H).status_code == 404
        value = client.get(path(restored), headers=H).json()
        assert value["release"]["document"] == restored["document"]
        assert value["validation"]["status"] == "not_applicable"
        assert value["validation"]["reason"] == "rollback"
        assert value["validation"]["source_release_id"] == published["baseline"]["release_id"]
        assert value["validation"]["evaluation"] is None
        # A normal member cannot read managed execution assets or invoke their rollback endpoint.
        member = TestClient(client.app, base_url="http://192.0.2.10:1080")
        registered = member.post("/api/auth/register", json={"username": "reader", "password": "test-password"})
        assert registered.status_code == 200, registered.text
        response = client.put("/api/tenants/default/members/reader", headers=H,
                              json={"role": "member", "status": "active"})
        assert response.status_code == 200, response.text
        assert member.get(path(restored), headers=H).status_code == 403
        assert member.post("/api/skill-evolution/execution-profiles/instructor/rollback", headers=H, json={
            "release_id": published["baseline"]["release_id"], "expected_release_id": restored["release_id"],
        }).status_code == 403
        member.close()


def test_legacy_release_without_validation_keeps_its_content_readable(pg_env, tmp_path, monkeypatch, published):
    async def seed(conn):
        return await ExecutionRepository(pg_env.tx).publish(
            conn, tenant_id="default", profile_id="instructor",
            expected_release_id=published["release"]["release_id"],
            document=copy.deepcopy(published["release"]["document"]), actor="test-only",
            candidate_id="legacy-without-evidence", candidate_revision=1, validation_ref="missing-legacy-run",
        )
    legacy = _run(pg_env, "default", seed)
    with _pod(pg_env, tmp_path, monkeypatch, pod="release-legacy") as client:
        _login(client, bootstrap=True)
        response = client.get(path(legacy), headers=H)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["release"]["document"] == legacy["document"]
        assert value["validation"]["status"] == "unavailable"
        assert value["validation"]["reason"] == "validation_command_missing"
        assert value["validation"]["evaluation"] is None
