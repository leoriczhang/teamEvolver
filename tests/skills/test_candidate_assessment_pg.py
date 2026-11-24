"""Review publication and completion reuse through real fenced PG Worker tasks."""
import copy
import json
import os
from types import SimpleNamespace

import pytest

from team_replay.contracts import skill_job_from_spec
from team_replay.datasets.repository import DatasetNotFound
from team_replay.repository import ReplayRepository
from team_replay.runner import evaluate_spec
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits, load_skills_config
from team_skills.evolution.candidate_pipeline import submit_validation
from team_skills.evolution.history import candidate_payload
from team_skills.evolution.repository import EvolutionRepository
from team_skills.handlers import register_handlers
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.candidates import candidate, seed_candidate
from tests.support.postgres import _run
from tests.support.replay import Host, judge, spec
from tests.support.skills import _skill_md

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL required",
)]


def test_assessment_timeout_is_validated_tenant_scoped_and_frozen_at_acceptance(pg_env, tmp_path, monkeypatch):
    cid = seed_candidate(pg_env)
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                              extras={"config_repo": ConfigRepository(pg_env.tx)})
    seen = []

    async def chat(messages, **kwargs):
        seen.append(kwargs["total_timeout_seconds"])
        return json.dumps({
            "method": "experience_review", "verdict": "inconclusive", "scope": "工具指引",
            "reason": "No evidence", "evidence_refs": [], "issues": [],
        })

    async def model(*args, **kwargs):
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr("team_skills.evolution.model.model_for", model)
    with _pod(pg_env, tmp_path, monkeypatch, pod="timeout") as client:
        _login(client, bootstrap=True)
        url = "/api/skill-evolution/settings"
        original = client.get(url, headers=H).json()
        assert original["validation"]["assessment_timeout_seconds"] == 600
        for invalid in (0, 29, 1801, True, 600.5, "600", None):
            result = client.put(url, headers=H, json={
                "revision": original["revision"], "validation": {"assessment_timeout_seconds": invalid},
            })
            assert result.status_code == 400, result.text
        changed = client.put(url, headers=H, json={
            "revision": original["revision"], "validation": {"assessment_timeout_seconds": 900},
        })
        assert changed.status_code == 200, changed.text
        assert changed.json()["validation"]["assessment_timeout_seconds"] == 900
        assert client.put(url, headers=H, json={
            "revision": original["revision"], "validation": {"assessment_timeout_seconds": 60},
        }).status_code == 409
        headers = {**H, "Idempotency-Key": "fixed-review-timeout"}
        accepted = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=headers)
        assert accepted.status_code == 202, accepted.text
        assert client.put(url, headers=H, json={
            "revision": changed.json()["revision"], "validation": {"assessment_timeout_seconds": 45},
        }).status_code == 200
        assert client.post(f"/api/skill-candidates/{cid}/evaluate", headers=headers).json() == accepted.json()
        other = _run(pg_env, "alpha", lambda c: load_skills_config(services.extras["config_repo"], c))
        assert other.get("assessment_timeout_seconds") == 600
        command = _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).command(
            c, accepted.json()["job_id"],
        ))
        assert command["payload"]["config"]["assessment_timeout_seconds"] == 900
        pg_env.tx.pool.run(process_once(services, owner_id="timeout-worker", kinds=["skill.validate"]))
        assert seen == [900]
        assert candidate(pg_env, cid)["verdict"] == "inconclusive"


@pytest.mark.parametrize("verdict", ["inconclusive", "reject"])
@pytest.mark.parametrize("publish_method", ["experience_review", None])
def test_force_publishes_saved_experience_review_without_changing_its_verdict(
    pg_env, monkeypatch, verdict, publish_method,
):
    cid = seed_candidate(pg_env)
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                              extras={"config_repo": ConfigRepository(pg_env.tx)})
    calls = []

    async def chat(*args, **kwargs):
        calls.append(True)
        return json.dumps({
            "method": "experience_review", "verdict": verdict, "scope": "工具参数指引",
            "reason": "Evidence does not confirm the complete contract", "evidence_refs": ["source"],
            "issues": ["The proposed contract has not been verified"],
        })

    async def model(*args, **kwargs):
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr("team_skills.evolution.model.model_for", model)
    _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).record_evidence(
        conn, tenant_id="default", name="demo",
        sessions=[{"session_id": "source", "content_hash": "a" * 64, "_summary": "Tool parameter evidence"}],
    ))

    async def accept(conn, mode, command_id):
        config = await load_skills_config(services.extras["config_repo"], conn)
        method = "experience_review" if mode == "evaluate" else publish_method
        return await submit_validation(conn, services, tenant_id="default", candidate_id=cid,
                                       command_id=command_id, config=config, actor="admin",
                                       revision=1, mode=mode,
                                       **({"validation_method": method} if method else {}))

    _run(pg_env, "default", lambda conn: accept(conn, "evaluate", "review-first"))
    pg_env.tx.pool.run(process_once(services, owner_id="review-worker", kinds=["skill.validate"]))
    reviewed = candidate(pg_env, cid)
    assert reviewed["status"] == "open" and reviewed["verdict"] == verdict
    _run(pg_env, "default", lambda conn: accept(conn, "force", "force-publish"))
    pg_env.tx.pool.run(process_once(services, owner_id="publish-worker", kinds=["skill.validate"]))
    published = candidate(pg_env, cid)
    assert published["status"] == "published" and published["published_version"] == 1
    assert published["verdict"] == verdict
    assert published["evaluation"]["recommended_publish"] is False
    assert (published["evaluation"]["replay"]["assessment"]["issues"]
            == reviewed["evaluation"]["replay"]["assessment"]["issues"])
    assert published["decision"]["mode"] == "force"
    assert published["decision"]["review_overridden"] is True
    from team_skills.library.repository import SkillLibraryRepository
    version = _run(pg_env, "default", lambda conn: SkillLibraryRepository(pg_env.tx).get_version(conn, "demo", 1))
    assert version["provenance"]["kind"] == "te"
    assert version["provenance"]["actor"] == "admin"
    assert version["provenance"]["candidate_id"] == cid
    assert len(calls) == 1


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


def test_force_publishes_blocked_execution_without_restarting_assessment(pg_env, monkeypatch):
    cid = seed_candidate(pg_env)
    blocked = {
        "verdict": "inconclusive", "validation_method": "candidate_execution",
        "reason": "Recorded MA configuration or input materials are incomplete",
        "basic_validation": {"status": "blocked"}, "candidate_completed": 0,
    }

    async def evaluate(job, config):
        return copy.deepcopy(blocked)

    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                              extras={"config_repo": ConfigRepository(pg_env.tx), "skill_replay": evaluate})

    async def accept(conn, mode, ident):
        config = await load_skills_config(services.extras["config_repo"], conn)
        return await submit_validation(conn, services, tenant_id="default", candidate_id=cid,
                                       command_id=ident, config=config, actor="admin", revision=1, mode=mode)

    _run(pg_env, "default", lambda c: accept(c, "evaluate", "blocked-review"))
    pg_env.tx.pool.run(process_once(services, owner_id="review-worker", kinds=["skill.validate"]))
    reviewed = candidate(pg_env, cid)
    assert reviewed["status"] == "open" and reviewed["verdict"] == "inconclusive"
    services.extras.pop("skill_replay")
    calls = []

    async def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Force publication must not restart assessment or execution")

    monkeypatch.setattr("team_skills.evolution.model.model_for", unexpected)
    monkeypatch.setattr("team_skills.evolution.candidate_pipeline.evaluate_fixed_job", unexpected)
    services.extras["ma_active_connections"] = unexpected
    services.extras["ma_automatic_fingerprints"] = unexpected
    services.extras["ma_freeze_replay"] = unexpected
    accepted = _run(pg_env, "default", lambda c: accept(c, "force", "force-after-blocked"))
    assert _run(pg_env, "default", lambda c: accept(c, "force", "force-after-blocked")) == accepted
    pending = candidate(pg_env, cid)
    shown = _run(pg_env, "default", lambda c: candidate_payload(c, pg_env.tx, pending, compact=True))
    assert shown["publication_pending"] is True and "replay_progress" not in shown
    pg_env.tx.pool.run(process_once(services, owner_id="publish-worker", kinds=["skill.validate"]))
    published = candidate(pg_env, cid)
    assert not calls, "Clicking force publish started another evaluation"
    assert published["status"] == "published" and published["published_version"] == 1
    assert published["evaluation"] == reviewed["evaluation"]
    assert published["verdict"] == "inconclusive"
    assert published["decision"]["mode"] == "force"
    assert published["decision"]["validation_overridden"] is True
    final = _run(pg_env, "default", lambda c: candidate_payload(c, pg_env.tx, published, compact=True))
    assert final["publication_pending"] is False
    with pytest.raises(DatasetNotFound):
        _run(pg_env, "default", lambda c: ReplayRepository(pg_env.tx).get(c, "skill", "force-after-blocked"))


@pytest.mark.parametrize("changed_after_acceptance", [False, True])
def test_force_keeps_static_and_candidate_revision_checks(pg_env, changed_after_acceptance):
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                              extras={"config_repo": ConfigRepository(pg_env.tx)})

    async def prepare(conn):
        repo = CandidateRepository(pg_env.tx)
        item = await repo.create(
            conn, tenant_id="default", skill_name="demo", limits=BundleLimits(), proposed_action="create",
            bundle={"SKILL.md": _skill_md("demo"), "config.json": b'{"invalid":'},
            detail={"file_changes": [{"path": "config.json", "operation": "upsert"}]},
        )
        config = await load_skills_config(services.extras["config_repo"], conn)
        await submit_validation(conn, services, tenant_id="default", candidate_id=item["candidate_id"],
                                command_id="force-check", config=config, actor="admin", revision=1, mode="force")
        if changed_after_acceptance:
            await repo.revise_content(
                conn, tenant_id="default", candidate_id=item["candidate_id"],
                bundle={"SKILL.md": _skill_md("demo"), "config.json": b"{}"},
                limits=BundleLimits(), actor="admin", expected_revision=1,
            )
        return item["candidate_id"]

    cid = _run(pg_env, "default", prepare)
    pg_env.tx.pool.run(process_once(services, owner_id="publish-worker", kinds=["skill.validate"]))
    item = candidate(pg_env, cid)
    command = _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).command(c, "force-check"))
    assert command["result"]["status"] == ("stale" if changed_after_acceptance else "blocked")
    assert item["status"] == "open" and not item["published_version"] and not item["evaluation"]


def test_new_skill_review_uses_its_active_source_group_evidence(pg_env, monkeypatch):
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                              extras={"config_repo": ConfigRepository(pg_env.tx)})

    async def chat(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert [e["id"] for e in payload["evidence"]] == ["source"]
        return json.dumps({
            "method": "experience_review", "verdict": "accept", "scope": "工具参数约束",
            "reason": "The source group supports the new Skill", "evidence_refs": ["source"], "issues": [],
        })

    async def model(*args, **kwargs):
        return SimpleNamespace(chat=chat)

    monkeypatch.setattr("team_skills.evolution.model.model_for", model)

    async def seed_and_accept(conn):
        repo = EvolutionRepository(pg_env.tx)
        await repo.record_evidence(
            conn, tenant_id="default", name="__no_skill__",
            sessions=[{"session_id": sid, "content_hash": "a" * 64, "_summary": "Tool parameter contract"}
                      for sid in ("source", "archived", "unrelated")],
        )
        await repo.archive_evidence(conn, "__no_skill__", "archived", "manual update")
        item = await CandidateRepository(pg_env.tx).create(
            conn, tenant_id="default", skill_name="new-tool-guide",
            bundle={"SKILL.md": _skill_md("new-tool-guide")}, limits=BundleLimits(), proposed_action="create",
            detail={"evolution_context": {"skill_name": "__no_skill__"},
                    "session_ids": ["source", "archived"]},
        )
        config = await load_skills_config(services.extras["config_repo"], conn)
        command = await submit_validation(
            conn, services, tenant_id="default", candidate_id=item["candidate_id"],
            command_id="review-new-skill", config=config, actor="admin", revision=1, mode="evaluate",
        )
        assert [e["session_id"] for e in command["payload"]["evidence"]] == ["source"]
        return item

    original = _run(pg_env, "default", seed_and_accept)
    pg_env.tx.pool.run(process_once(services, owner_id="review-worker", kinds=["skill.validate"]))
    row = candidate(pg_env, original["candidate_id"])
    assert row["status"] == "open"
    assert row["verdict"] == "accept"
    assert row["revision"] == original["revision"] and row["tree_sha256"] == original["tree_sha256"]
    assert row["evaluation"]["replay"]["assessment"]["evidence_refs"] == ["source"]


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
