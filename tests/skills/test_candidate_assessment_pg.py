"""Review publication and completion reuse through real fenced PG Worker tasks."""
import copy
import json
import os
from types import SimpleNamespace

import pytest

from team_replay.contracts import skill_job_from_spec
from team_replay.repository import ReplayRepository
from team_replay.runner import evaluate_spec
from team_skills.config import load_skills_config
from team_skills.evolution.candidate_pipeline import submit_validation
from team_skills.evolution.repository import EvolutionRepository
from team_skills.handlers import register_handlers
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.candidates import candidate, seed_candidate
from tests.support.postgres import _run
from tests.support.replay import Host, judge, spec

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL required",
)]


def test_experience_review_publishes_without_ma_resources_or_runtime_gate(pg_env, monkeypatch):
    cid = seed_candidate(pg_env)
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                              extras={"config_repo": ConfigRepository(pg_env.tx)})

    async def chat(*args, **kwargs):
        return json.dumps({
            "method": "experience_review", "verdict": "accept", "scope": "工具路由补充",
            "reason": "Evidence supports the added method", "evidence_refs": ["source"], "issues": [],
        })

    async def model(*args, **kwargs):
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr("team_skills.evolution.model.model_for", model)

    async def seed_and_accept(conn):
        await EvolutionRepository(pg_env.tx).record_evidence(
            conn, tenant_id="default", name="demo",
            sessions=[{"session_id": "source", "content_hash": "a" * 64,
                       "_summary": "Resource routing was selected incorrectly; correct integration was available"}],
        )
        config = await load_skills_config(services.extras["config_repo"], conn)
        return await submit_validation(conn, services, tenant_id="default", candidate_id=cid,
                                       command_id="review-once", config=config, actor="admin", revision=1, mode="auto")
    accepted = _run(pg_env, "default", seed_and_accept)
    assert not accepted["result"]
    pg_env.tx.pool.run(process_once(services, owner_id="review-worker", kinds=["skill.validate"]))
    row = candidate(pg_env, cid)
    assert row["status"] == "published"
    assert row["evaluation"]["replay"]["runtime_gate"]["status"] == "not_required"
    assert row["evaluation"]["replay"]["assessment"]["evidence_refs"] == ["source"]
    assert _run(pg_env, "default", lambda c: c.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.ma_external_effects",
    )) == 0


def test_reuse_requires_matching_candidate_task_materials_and_execution_config(pg_env):
    fixed = spec()
    job = skill_job_from_spec(fixed)
    job.update(job_id="same-candidate", candidate_revision=2, skill_ids=["demo"],
               replay_config={"revision": 1, "config": {"timeout_seconds": 600}},
               replay_adapter={"revision": "fake-v1"}, ma_records={})
    result = evaluate_spec(fixed, host=Host(), judge=judge)

    async def store(conn):
        repo = ReplayRepository(pg_env.tx)
        await repo.accept(conn, tenant_id="default", domain="skill", run_id="old-run",
                          subject_id="demo", request={"id": "original"}, payload={"job": job})
        await repo.save_case(conn, tenant_id="default", domain="skill", run_id="old-run",
                             case=job["replay_cases"][0], result=result)
        reused = await repo.reusable_candidate_cases(conn, {**job, "validation_method": "candidate_execution"})
        assert reused and next(iter(reused.values()))["reused_from"] == "old-run"
        for field, replacement in (
            ("candidate_revision", 3), ("candidate_skill", {}), ("current_skill", {}),
            ("replay_config", {"revision": 2}), ("replay_adapter", {"revision": "fake-v2"}),
            ("ma_records", {"changed": {"runtime_hash": "new"}}),
        ):
            changed = copy.deepcopy(job)
            changed[field] = replacement
            assert await repo.reusable_candidate_cases(conn, changed) == {}
        changed = copy.deepcopy(job)
        changed["replay_cases"][0]["query"] = "Different task"
        assert await repo.reusable_candidate_cases(conn, changed) == {}
        changed = copy.deepcopy(job)
        changed["replay_cases"][0]["checks"][0]["text"] = "Different scope"
        assert await repo.reusable_candidate_cases(conn, changed) == {}
    _run(pg_env, "default", store)
