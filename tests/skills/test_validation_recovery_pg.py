"""Terminal validation failure cannot permanently lock an unpublished Candidate."""
import os

import pytest

from team_skills.candidates.repository import CandidateRepository
from team_skills.evolution.candidate_pipeline import recover_failed_validations
from teamEvolver.runtime.services import build_services
from tests.support.candidates import candidate, seed_candidate
from tests.support.postgres import _run

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="PostgreSQL required",
)]


@pytest.mark.parametrize("status,kind,recovered", [
    ("failed", "skill.validate", True),
    ("cancelled", "skill.validate", True),
    ("running", "skill.validate", False),
    ("retry_wait", "skill.validate", False),
    ("failed", "ma.skill", False),
])
def test_recovery_releases_only_terminal_validation_and_preserves_bytes(pg_env, status, kind, recovered):
    cid = seed_candidate(pg_env)
    services = build_services(role="worker", tx=pg_env.tx, handlers={})
    repo = CandidateRepository(pg_env.tx)

    async def prepare(conn):
        await services.tasks.enqueue(
            conn, tenant_id="default", task_id="validation", kind=kind,
            subject_type="skill_command", subject_id="validation", schema_version="1",
            dedupe_key="validation", payload_ref="validation", payload_hash="a" * 64,
            config_revision=1,
        )
        await conn.execute(
            f"UPDATE {pg_env.schema}.worker_tasks SET status=$1,error_code='retries_exhausted' "
            "WHERE task_id='validation'", status,
        )
        return await repo.transition(
            conn, tenant_id="default", candidate_id=cid, to_status="validating", actor="test",
            validate_task_id="validation",
        )

    before = _run(pg_env, "default", prepare)
    pg_env.tx.pool.run(recover_failed_validations(services, "other-tenant"))
    assert candidate(pg_env, cid)["status"] == "validating"
    pg_env.tx.pool.run(recover_failed_validations(services, "default"))
    after = candidate(pg_env, cid)
    assert after["status"] == ("open" if recovered else "validating")
    assert after["revision"] == before["revision"] and after["tree_sha256"] == before["tree_sha256"]
    assert after["verdict"] == "", "Worker failure is not a model validation verdict"
    if recovered:
        assert not after["validate_task_id"]
        assert after["evaluation"]["status"] == "failed"
        assert after["evaluation"]["recommended_publish"] is False
        events = _run(pg_env, "default", lambda c: repo.events(c, cid))
        assert events[-1]["actor"] == "validation-recovery"
        assert "retries_exhausted" in events[-1]["detail"]["reason"]
        pg_env.tx.pool.run(recover_failed_validations(services, "default"))
        assert _run(pg_env, "default", lambda c: repo.events(c, cid)) == events
        assert _run(pg_env, "default", lambda c: repo.record_verdict(
            c, tenant_id="default", candidate_id=cid, revision=1,
            verdict="accept", evaluation={}, task_id="validation",
        )) is None, "Late results of the abandoned task must not overwrite the Candidate"
