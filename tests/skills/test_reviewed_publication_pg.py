"""Real PG publication fences around synthetic completed runs; no Agent or model execution."""
from __future__ import annotations

import asyncio
import copy
import os

import pytest

from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository
from team_skills.evolution.repository import EvolutionRepository
from team_skills.execution.candidates import revise_execution_detail
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime import build_services, process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, seed_reviewed_execution
from tests.support.postgres import _run

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


@pytest.fixture
def seeded(pg_env):
    return _run(pg_env, "default", lambda conn: seed_reviewed_execution(conn, pg_env.tx))


@pytest.fixture
def publication_worker(pg_env, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("publishing an immutable reviewed result must not run Replay or a model")

    monkeypatch.setattr("team_skills.evolution.candidate_pipeline.evaluate_fixed_job", forbidden)
    return build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                          extras={"skill_replay": forbidden})


def attest(client, seeded, *, notes="Synthetic persistence contract only; no business-quality claim"):
    item = seeded["candidate"]
    response = client.post(f"/api/skill-candidates/{item['candidate_id']}/independent-validation", headers=H, json={
        "candidate_revision": item["revision"], "status": "passed", "method": "programmatic",
        "evidence_ref": "test-only:immutable-persistence-result",
        "case_ids": [case["case_id"] for case in seeded["payload"]["detail"]["replay_cases"]],
        "notes": notes,
    })
    assert response.status_code == 200, response.text
    return response


def submit(client, seeded, *, key="publish-reviewed", mode="auto"):
    return client.post(f"/api/skill-candidates/{seeded['candidate']['candidate_id']}/validate",
                       headers={**H, "Idempotency-Key": key},
                       json={"mode": mode, "candidate_revision": seeded["candidate"]["revision"]})


def command(pg_env, job_id):
    return _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(conn, job_id))


def immutable_history(pg_env, seeded):
    async def read(conn):
        return {"command": await EvolutionRepository(pg_env.tx).command(conn, seeded["run_id"]),
                "replay": await ReplayRepository(pg_env.tx).get(conn, "skill", seeded["run_id"])}
    return _run(pg_env, "default", read)


def assert_no_new_replay(pg_env):
    assert _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).count(conn, "skill")) == 1


async def drift(conn, tx, seeded, kind):
    repo = CandidateRepository(tx)
    cid = seeded["candidate"]["candidate_id"]
    if kind == "marks":
        await repo.set_marks(conn, tenant_id="default", candidate_id=cid,
                             changes={"reviewed": True}, actor="test-drift")
    elif kind in {"skills-config", "replay-config"}:
        configs = ConfigRepository(tx)
        module = "skills" if kind == "skills-config" else "replay"
        row = await configs.read_tenant_module(conn, module)
        # Even identical values at a new revision invalidate the pinned configuration.
        await configs.write_tenant_module(conn, module=module, expected_revision=row.revision, config=row.config)
    elif kind == "budget":
        await repo.increase_validation_budget(
            conn, tenant_id="default", candidate_id=cid, expected_revision=1,
            maximum=1_500_000, reason="Explicit test budget authorization", actor="test-drift",
        )
    elif kind == "adapter":
        adapters = ReplayAdapterRepository(tx)
        source = ('REPLAY_ADAPTER = {"label": "Persistence fixture", "enabled": True}\n'
                  'def build_replay_adapter(config):\n'
                  '    raise AssertionError("This fixture adapter must never execute")\n')
        await adapters.save(conn, tenant_id="default", filename="fixture.py", source=source, expected_revision=0)
        await adapters.bind(conn, tenant_id="default", filename="fixture.py")
    elif kind == "head":
        releases = ExecutionRepository(tx)
        baseline = seeded["baseline"]
        await releases.publish(
            conn, tenant_id="default", profile_id=baseline["profile_id"],
            expected_release_id=baseline["release_id"], document=copy.deepcopy(baseline["document"]),
            actor="test-drift", candidate_id="other-publication", candidate_revision=1,
            validation_ref="test-only:other-completed-validation",
        )
    elif kind == "content":
        item = await repo.require(conn, cid)
        detail = revise_execution_detail(item["detail"], [component_change("Edited after acceptance")])
        await repo.revise_detail(conn, tenant_id="default", candidate_id=cid, detail=detail,
                                 actor="test-drift", expected_revision=1)
    else:
        raise AssertionError("unrecognized test drift")


@pytest.mark.parametrize("mode", ["auto", "force"])
def test_missing_independent_review_rejects_before_any_task_is_enqueued(
    pg_env, tmp_path, monkeypatch, seeded, mode,
):
    before = immutable_history(pg_env, seeded)
    with _pod(pg_env, tmp_path, monkeypatch, pod="missing-review") as client:
        _login(client, bootstrap=True)
        response = submit(client, seeded, mode=mode)
        assert response.status_code == 409, response.text
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks",
    )) == 0
    assert immutable_history(pg_env, seeded) == before
    assert_no_new_replay(pg_env)


@pytest.mark.parametrize("kind", [
    "marks", "skills-config", "replay-config", "budget", "adapter", "head", "content", "review-history",
])
def test_change_after_acceptance_blocks_without_mutating_the_reviewed_run(
    pg_env, tmp_path, monkeypatch, seeded, publication_worker, kind,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="publication-drift") as client:
        _login(client, bootstrap=True)
        attest(client, seeded)
        before = immutable_history(pg_env, seeded)
        response = submit(client, seeded)
        assert response.status_code == 202, response.text
        jid = response.json()["job_id"]
        accepted = command(pg_env, jid)
        assert accepted["kind"] == "skill.evolve"
        assert accepted["payload"]["operation"] == "execution_publish_reviewed"
        if kind == "review-history":
            attest(client, seeded, notes="A subsequent independent review supersedes the queued review event")
        else:
            _run(pg_env, "default", lambda conn: drift(conn, pg_env.tx, seeded, kind))
        pg_env.tx.pool.run(process_once(publication_worker, owner_id="fenced-publication", tenants=("default",)))
    result = command(pg_env, jid)["result"]
    assert result["status"] == "blocked" and result["accepted"] is False
    current = _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).require(
        conn, seeded["candidate"]["candidate_id"],
    ))
    assert current["status"] == "open"
    releases = _run(pg_env, "default", lambda conn: ExecutionRepository(pg_env.tx).releases(conn, "instructor"))
    assert not any(row["candidate_id"] == current["candidate_id"] for row in releases)
    assert immutable_history(pg_env, seeded) == before
    assert_no_new_replay(pg_env)


@pytest.mark.parametrize("kind", ["skills-config", "replay-config", "adapter", "head", "budget"])
def test_preexisting_environment_or_budget_drift_is_rejected_before_queueing(
    pg_env, tmp_path, monkeypatch, seeded, kind,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="prepublication-drift") as client:
        _login(client, bootstrap=True)
        attest(client, seeded)
        _run(pg_env, "default", lambda conn: drift(conn, pg_env.tx, seeded, kind))
        response = submit(client, seeded)
        assert response.status_code == 409, response.text
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks",
    )) == 0
    assert_no_new_replay(pg_env)


def test_two_concurrent_publications_create_only_one_release_and_same_key_replays_acceptance(
    pg_env, tmp_path, monkeypatch, seeded, publication_worker,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="concurrent-publication") as client:
        _login(client, bootstrap=True)
        attest(client, seeded)
        before = immutable_history(pg_env, seeded)
        first = submit(client, seeded, key="publication-one")
        assert first.status_code == 202, first.text
        retried = submit(client, seeded, key="publication-one")
        assert retried.status_code == 202 and retried.json()["job_id"] == first.json()["job_id"]
        second = submit(client, seeded, key="publication-two")
        assert second.status_code == 202, second.text
        assert second.json()["job_id"] != first.json()["job_id"]

        async def run_both():
            return await asyncio.gather(
                process_once(publication_worker, owner_id="publisher-a", tenants=("default",)),
                process_once(publication_worker, owner_id="publisher-b", tenants=("default",)),
            )
        claimed = pg_env.tx.pool.run(run_both())
        assert all(task is not None for task in claimed)
        # Network retries after completion still refer to the original command.
        repeated = submit(client, seeded, key="publication-one")
        assert repeated.status_code == 202 and repeated.json()["job_id"] == first.json()["job_id"]
    results = [command(pg_env, response.json()["job_id"])["result"] for response in (first, second)]
    assert sorted(result["status"] for result in results) == ["blocked", "published"]
    published = next(result for result in results if result["status"] == "published")
    assert published["validation_run_id"] == seeded["run_id"]
    releases = _run(pg_env, "default", lambda conn: ExecutionRepository(pg_env.tx).releases(conn, "instructor"))
    ours = [row for row in releases if row["candidate_id"] == seeded["candidate"]["candidate_id"]]
    assert len(ours) == 1 and ours[0]["validation_ref"] == seeded["run_id"]
    assert immutable_history(pg_env, seeded) == before
    assert_no_new_replay(pg_env)


def test_review_of_old_run_does_not_authorize_new_result_with_identical_inputs(
    pg_env, tmp_path, monkeypatch, seeded,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="different-reviewed-run") as client:
        _login(client, bootstrap=True)
        attest(client, seeded)
        original = immutable_history(pg_env, seeded)

        async def finish_another(conn):
            cid = seeded["candidate"]["candidate_id"]
            run_id = "validate:another-completed-result"
            payload = copy.deepcopy(seeded["payload"])
            evaluation = copy.deepcopy(seeded["evaluation"])
            evaluation["validation_run_id"] = run_id
            evaluation["fixture_result_identity"] = "a different synthetic result, without model output"
            await EvolutionRepository(pg_env.tx).accept(
                conn, tenant_id="default", command_id=run_id, kind="skill.validate",
                request={"candidate_id": cid, "mode": "evaluate", "revision": 1}, payload=payload,
            )
            repo = CandidateRepository(pg_env.tx)
            await repo.transition(conn, tenant_id="default", candidate_id=cid, to_status="validating",
                                  actor="test-new-run", expected_revision=1, validate_task_id=run_id)
            await ReplayRepository(pg_env.tx).accept(
                conn, tenant_id="default", domain="skill", run_id=run_id, subject_id="instructor",
                request={"validation_command": run_id},
                payload={"job": {**copy.deepcopy(payload["detail"]), "job_id": cid, "candidate_revision": 1}},
            )
            await ReplayRepository(pg_env.tx).finish(conn, "skill", run_id, evaluation)
            await EvolutionRepository(pg_env.tx).finish(
                conn, run_id, {"status": "evaluated", "evaluation": evaluation},
            )
            await repo.record_verdict(conn, tenant_id="default", candidate_id=cid, revision=1,
                                      verdict="inconclusive", evaluation=evaluation, task_id=run_id)
        _run(pg_env, "default", finish_another)
        response = submit(client, seeded)
        assert response.status_code == 409, response.text
        assert "independent_validation_result_changed" in response.text
    assert immutable_history(pg_env, seeded) == original
    assert _run(pg_env, "default", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.worker_tasks",
    )) == 0
