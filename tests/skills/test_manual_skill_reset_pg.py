"""Manual library updates retire related automatic assets without erasing history."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from session_ingestion.schemas import session_payload_hash
from team_replay.datasets.repository import DatasetRepository
from team_replay.datasets.schema import make_document
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import BundleLimits
from team_skills.evolution.repository import EvolutionRepository
from team_skills.evolution.reset import reset_skill_evolution, retire_manual_commit
from team_skills.evolution.session_processing import after_reset
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from team_skills.transfer.packages import package
from team_skills.transfer.service import SkillTransferService
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"),
        reason="isolated PostgreSQL required",
    ),
]


async def _seed(env, conn, *, name="demo", candidate_id="legacy-demo"):
    library = SkillLibraryRepository(env.tx)
    original = {"SKILL.md": _skill_md(name, "original"), "runtime/legacy.py": b"old_runtime = True\n"}
    await library.commit_bundle(
        conn, tenant_id="default", name=name, bundle=original,
        mutation_id=f"seed-{name}", limits=BundleLimits(), source="console",
    )
    await CandidateRepository(env.tx).create(
        conn, tenant_id="default", skill_name=name, candidate_id=candidate_id,
        bundle={**original, "SKILL.md": _skill_md(name, "old proposal")},
        limits=BundleLimits(), base_version=1,
    )
    await EvolutionRepository(env.tx).record_evidence(
        conn, tenant_id="default", name=name,
        sessions=[{"session_id": f"old-{name}", "content_hash": f"hash-{name}", "_summary": "old method"}],
    )
    await DatasetRepository(env.tx).save(
        conn, tenant_id="default",
        document=make_document(
            dataset_id=f"old-{name}", name="Old task", skills=[name], source={"kind": "evolution"},
            cases=[{
                "case_id": "task", "query": "Complete the task", "skill_ids": [name], "materials": [],
                "checks": [{"id": "R01", "kind": "output", "text": "Actual output exists"}],
            }],
        ),
    )


def test_manual_update_archives_old_candidate_evidence_and_case_but_keeps_history(pg_env, tmp_path, monkeypatch):
    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    with _pod(pg_env, tmp_path, monkeypatch, pod="manual-reset") as client:
        _login(client, bootstrap=True)
        response = client.delete("/api/skills/demo/files/runtime/legacy.py", headers=H)
        assert response.status_code == 200, response.text
        assert client.get("/api/skill-candidates", headers=H).json()["candidates"] == []
        old = client.get("/api/skill-candidates/legacy-demo/detail", headers=H).json()
        assert old["review_status"] == "archived"
        assert old["decision"]["reason"]
        assert client.put(
            "/api/skill-candidates/legacy-demo/content",
            headers=H, json={"content": "should not revive old files"},
        ).status_code == 409

    async def verify(conn):
        candidates = CandidateRepository(pg_env.tx)
        historical = await candidates.read_bundle(conn, "legacy-demo")
        assert historical["runtime/legacy.py"] == b"old_runtime = True\n"
        assert await EvolutionRepository(pg_env.tx).evidence(conn, "demo") == []
        datasets = DatasetRepository(pg_env.tx)
        await datasets.save(
            conn, tenant_id="default",
            document=make_document(dataset_id="new-demo", name="New task", skills=["demo"],
                                   cases=(await datasets.get(conn, "old-demo"))["cases"]),
        )
        assert await datasets.historical_cases(conn, skill_id="demo", before_dataset_id="new-demo") == []
        frozen = await datasets.freeze(conn, "old-demo")
        assert frozen["document"]["cases"][0]["query"] == "Complete the task"
        head, current = await SkillLibraryRepository(pg_env.tx).read_current_bundle(conn, "demo")
        assert set(current) == {"SKILL.md"}
        fresh = await candidates.create(
            conn, tenant_id="default", skill_name="demo", bundle=current,
            limits=BundleLimits(), base_version=head["version"],
        )
        assert fresh["candidate_id"] != "legacy-demo"
        assert set(await candidates.read_bundle(conn, fresh["candidate_id"])) == {"SKILL.md"}
        assert fresh["revision"] == 1

    _run(pg_env, "default", verify)


def test_noop_and_other_skill_do_not_reset_related_assets(pg_env, tmp_path, monkeypatch):
    async def seed(conn):
        await _seed(pg_env, conn)
        await _seed(pg_env, conn, name="other", candidate_id="legacy-other")
    _run(pg_env, "default", seed)
    with _pod(pg_env, tmp_path, monkeypatch, pod="manual-reset-noop") as client:
        _login(client, bootstrap=True)
        unchanged = client.post(
            "/api/skills", headers=H, json={"name": "demo", "skill_md": _skill_md("demo", "original").decode()},
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["status"] == "unchanged"
        assert client.get("/api/skill-candidates/legacy-demo/detail", headers=H).json()["review_status"] == "open"
        changed = client.delete("/api/skills/demo/files/runtime/legacy.py", headers=H)
        assert changed.status_code == 200, changed.text
        assert client.get("/api/skill-candidates/legacy-demo/detail", headers=H).json()["review_status"] == "archived"
        assert client.get("/api/skill-candidates/legacy-other/detail", headers=H).json()["review_status"] == "open"
    assert len(_run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).evidence(conn, "other"))) == 1


def test_old_package_cannot_only_change_its_baseline_label(pg_env):
    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    async def verify(conn):
        repo = CandidateRepository(pg_env.tx)
        old = await repo.read_bundle(conn, "legacy-demo")
        with pytest.raises(CandidateStateError, match="baseline changed"):
            await repo.create(conn, tenant_id="default", skill_name="demo", bundle=old,
                              limits=BundleLimits(), base_version=2)
        assert (await repo.require(conn, "legacy-demo"))["base_version"] == 1
    _run(pg_env, "default", verify)


@pytest.mark.parametrize("entry", ["transfer", "workbench", "rollback", "delete"])
def test_manual_entry_points_retire_candidates(pg_env, tmp_path, monkeypatch, entry):
    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    if entry == "transfer":
        async def transfer(conn):
            return await SkillTransferService(pg_env.tx).import_skills(
                conn, tenant_id="default", command_id="import-demo",
                payload={"channel": "zip", "actor": "human", "limits": BundleLimits().__dict__},
                packages=[package("demo", {"SKILL.md": _skill_md("demo", "imported")})],
            )
        _run(pg_env, "default", transfer)
    else:
        with _pod(pg_env, tmp_path, monkeypatch, pod=f"manual-{entry}") as client:
            _login(client, bootstrap=True)
            if entry == "workbench":
                import hashlib
                response = client.post("/api/replay-lab/skills/demo/save", headers=H, json={
                    "candidate_skill_md": _skill_md("demo", "edited").decode(),
                    "expected_sha256": hashlib.sha256(_skill_md("demo", "original")).hexdigest(),
                })
            elif entry == "rollback":
                response = client.post("/api/skills/demo/rollback?target_version=1", headers=H)
            else:
                response = client.delete("/api/skills/demo", headers=H)
            assert response.status_code == 200, response.text
    async def verify(conn):
        assert (await CandidateRepository(pg_env.tx).require(conn, "legacy-demo"))["status"] == "archived"
        assert await EvolutionRepository(pg_env.tx).evidence(conn, "demo") == []
        assert await CandidateRepository(pg_env.tx).read_bundle(conn, "legacy-demo")
    _run(pg_env, "default", verify)


@pytest.mark.parametrize("during_replay", [False, True])
def test_queued_or_running_old_validation_cannot_publish_after_manual_update(
    pg_env, tmp_path, monkeypatch, during_replay,
):
    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    seen = []

    async def manual_update(conn):
        commit = await SkillLibraryRepository(pg_env.tx).commit_bundle(
            conn, tenant_id="default", name="demo", bundle={"SKILL.md": _skill_md("demo", "manual")},
            mutation_id="manual-during-replay", limits=BundleLimits(), source="console",
        )
        await retire_manual_commit(conn, pg_env.tx, commit, tenant_id="default")

    async def replay(job, config):
        seen.append(job)
        if during_replay:
            async with pg_env.tx.tenant_tx("default") as conn:
                await manual_update(conn)
        return {"verdict": "accept", "runtime_gate": {"status": "passed"}}

    with _pod(pg_env, tmp_path, monkeypatch, pod="stale-validation") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/skill-candidates/legacy-demo/validate", headers=H, json={"mode": "auto"})
        assert response.status_code == 202, response.text
        if not during_replay:
            _run(pg_env, "default", manual_update)
        services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                                  extras={"skill_replay": replay})
        pg_env.tx.pool.run(process_once(services, owner_id="reset-test", kinds=["skill.validate"]))
        async def verify(conn):
            old = await CandidateRepository(pg_env.tx).require(conn, "legacy-demo")
            assert old["status"] == "archived" and not old["adopted"]
            head, bundle = await SkillLibraryRepository(pg_env.tx).read_current_bundle(conn, "demo")
            assert head["version"] == 2 and bundle == {"SKILL.md": _skill_md("demo", "manual")}
            cmd = await EvolutionRepository(pg_env.tx).command(conn, response.json()["job_id"])
            assert cmd["result"]["status"] == "stale"
        _run(pg_env, "default", verify)
    assert len(seen) == int(during_replay)


def test_old_business_session_cannot_reenter_evidence_and_new_session_can(pg_env):
    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    async def verify(conn):
        result = await reset_skill_evolution(conn, pg_env.tx, tenant_id="default", name="demo")
        boundary = datetime.fromisoformat(result["evolution_reset_at"])
        sessions = [
            {"session_id": "late-import", "content_hash": "late",
             "business_time": boundary - timedelta(days=1), "ingested_at": boundary + timedelta(seconds=1)},
            {"session_id": "fresh", "content_hash": "fresh", "business_time": boundary + timedelta(seconds=1)},
        ]
        assert not after_reset(sessions[0], boundary)
        await EvolutionRepository(pg_env.tx).record_evidence(conn, tenant_id="default", name="demo", sessions=sessions)
        assert [s["session_id"] for s in await EvolutionRepository(pg_env.tx).evidence(conn, "demo")] == ["fresh"]
        assert result["version"] == 1
    _run(pg_env, "default", verify)


@pytest.mark.parametrize("update_during_planning", [False, True])
def test_evolution_uses_fresh_library_bundle_and_discards_racing_old_proposal(
    pg_env, tmp_path, monkeypatch, update_during_planning,
):
    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    payloads = {}
    seen = []

    class Payloads:
        def get_session(self, ref):
            return payloads[ref]

    async def manual_update(conn):
        commit = await SkillLibraryRepository(pg_env.tx).commit_bundle(
            conn, tenant_id="default", name="demo", bundle={"SKILL.md": _skill_md("demo", "manual")},
            mutation_id="manual-planner", limits=BundleLimits(), source="console",
        )
        await retire_manual_commit(conn, pg_env.tx, commit, tenant_id="default")

    if not update_during_planning:
        _run(pg_env, "default", manual_update)
        async def prior_evidence(conn):
            now = await conn.fetchval("SELECT clock_timestamp()")
            await EvolutionRepository(pg_env.tx).record_evidence(
                conn, tenant_id="default", name="demo", sessions=[{
                    "session_id": "prior-fresh", "content_hash": "prior", "user_alias": "prior-user",
                    "business_time": now, "_summary": "Earlier valid Evidence after the Skill update",
                    "_judge_scores": {"overall_score": .8},
                }],
            )
        _run(pg_env, "default", prior_evidence)
    for sid in ("fresh-1", "fresh-2"):
        payloads[sid] = {
            "session_id": sid, "_summary": "Fresh method after manual update",
            "turns": [{"turn_num": 1, "prompt_text": "Do this", "response_text": "Done", "read_skills": ["demo"]}],
            "_judge_scores": {"source": "llm", **{key: .8 for key in (
                "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage",
            )}},
            "value_judge": {"mode": "merged", "decision": "valuable", "confidence": .9, "reason": "Reusable"},
        }
        async def seed(conn, sid=sid):
            await conn.execute(
                f"INSERT INTO {pg_env.schema}.sessions "
                "(tenant_id,session_id,user_id,decision,content_ref,content_hash) "
                "VALUES ('default',$1,$1,'valuable',$1,$2)", sid, session_payload_hash(payloads[sid]),
            )
        _run(pg_env, "default", seed)

    async def planner(**kwargs):
        seen.append(kwargs)
        if update_during_planning:
            async with pg_env.tx.tenant_tx("default") as conn:
                await manual_update(conn)
        return {"action": "update_skill", "skill": {"name": "demo", "description": "Fresh", "content": "Fresh steps"}}

    async def synthesis(**kwargs):
        return []

    with _pod(pg_env, tmp_path, monkeypatch, pod="fresh-evolution") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/skill-evolution/trigger", headers=H, json={})
        assert response.status_code == 202, response.text
        services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
            "skill_planner": planner, "session_payloads": Payloads(), "replay_case_synthesizer": synthesis,
        })
        pg_env.tx.pool.run(process_once(services, owner_id="planner-reset-test", kinds=["skill.evolve"]))
        async def verify(conn):
            result = (await EvolutionRepository(pg_env.tx).command(conn, response.json()["job_id"]))["result"]
            assert result is not None
            assert result["candidates_queued"] == int(not update_during_planning)
            assert (await CandidateRepository(pg_env.tx).require(conn, "legacy-demo"))["status"] == "archived"
            live = await CandidateRepository(pg_env.tx).live_for_skill(conn, "demo")
            if update_during_planning:
                assert live is None
            else:
                assert live["base_version"] == 2 and live["revision"] == 1
                assert set(await CandidateRepository(pg_env.tx).read_bundle(conn, live["candidate_id"])) == {"SKILL.md"}
        _run(pg_env, "default", verify)
    assert len(seen) == 1
    if not update_during_planning:
        assert [s["session_id"] for s in seen[0]["sessions"]] == ["fresh-1", "fresh-2", "prior-fresh"]


@pytest.mark.parametrize("update_during_planning", [False, True])
def test_explicit_reprocess_uses_consumed_old_sessions_without_reviving_archived_assets(
    pg_env, tmp_path, monkeypatch, update_during_planning,
):
    from team_skills.evolution.replay_case_synthesis import hydrate_cross_session_cases

    _run(pg_env, "default", lambda conn: _seed(pg_env, conn))
    payloads, seen = {}, []

    class Payloads:
        def get_session(self, ref):
            return payloads[ref]

    for sid in ("selected-1", "selected-2", "not-selected"):
        payloads[sid] = {
            "session_id": sid, "_summary": "Historical evidence explicitly selected by the administrator",
            "runtime": {"type": "managed-agents", "connection_id": "active"},
            "runtime_context": {"source_session_id": sid},
            "turns": [{"turn_num": 1, "prompt_text": "Do this", "response_text": "Done", "read_skills": ["demo"]}],
            "_judge_scores": {"source": "llm", **{key: .8 for key in (
                "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage",
            )}},
            "value_judge": {"mode": "merged", "decision": "valuable", "confidence": .9, "reason": "Reusable"},
        }
        async def seed(conn, sid=sid):
            sha = session_payload_hash(payloads[sid])
            await conn.execute(
                f"INSERT INTO {pg_env.schema}.sessions "
                "(tenant_id,session_id,user_id,decision,content_ref,content_hash,business_time) "
                "VALUES ('default',$1,$1,'valuable',$1,$2,clock_timestamp()-interval '1 day')", sid, sha,
            )
            await EvolutionRepository(pg_env.tx).consume(
                conn, tenant_id="default", command_id="previous-cycle",
                sessions=[{"session_id": sid, "content_hash": sha}],
            )
        _run(pg_env, "default", seed)

    async def manual_update(conn, mutation_id):
        commit = await SkillLibraryRepository(pg_env.tx).commit_bundle(
            conn, tenant_id="default", name="demo", bundle={"SKILL.md": _skill_md("demo", mutation_id)},
            mutation_id=mutation_id, limits=BundleLimits(), source="console",
        )
        await retire_manual_commit(conn, pg_env.tx, commit, tenant_id="default")

    _run(pg_env, "default", lambda c: manual_update(c, "manual-before-reprocess"))

    async def planner(**kwargs):
        seen.append(kwargs)
        if update_during_planning:
            async with pg_env.tx.tenant_tx("default") as conn:
                await manual_update(conn, "manual-during-reprocess")
        return {"action": "update_skill", "skill": {"name": "demo", "description": "Fresh", "content": "Fresh steps"}}

    async def synthesis(**kwargs):
        cases = [{
            "case_id": f"selected-case-{number}", "query": f"Task {number}", "skill_ids": ["demo"],
            "checks": [{"id": f"R{i}", "kind": "output", "text": f"Requirement {i}"} for i in range(6)],
            "materials": [], "provenance": {"session_ids": ["selected-1", "selected-2"]},
        } for number in range(2)]
        return hydrate_cross_session_cases(cases, kwargs["sessions"], skill_name="demo")

    async def fingerprints(tenant, *, conn=None):
        return {}  # Automatic inputs cannot use this historical runtime.

    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
        "skill_planner": planner, "session_payloads": Payloads(), "replay_case_synthesizer": synthesis,
        "ma_automatic_fingerprints": fingerprints,
    })
    with _pod(pg_env, tmp_path, monkeypatch, pod="explicit-reprocess") as client:
        _login(client, bootstrap=True)
        for body in ({"reprocess": True}, {"session_ids": ["selected-1"], "reprocess": "true"}):
            assert client.post("/api/skill-evolution/trigger", headers=H, json=body).status_code == 400
        normal = client.post("/api/skill-evolution/trigger", headers=H, json={"session_ids": ["selected-1"]})
        pg_env.tx.pool.run(process_once(services, owner_id="normal-worker", kinds=["skill.evolve"]))
        assert _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).command(
            c, normal.json()["job_id"],
        ))["result"]["sessions"] == 0
        headers = {**H, "Idempotency-Key": "explicit-reprocess-once"}
        body = {"session_ids": ["selected-1", "selected-2"], "reprocess": True}
        response = client.post("/api/skill-evolution/trigger", headers=headers, json=body)
        assert response.status_code == 202, response.text
        assert client.post("/api/skill-evolution/trigger", headers=headers, json=body).json() == response.json()
        pg_env.tx.pool.run(process_once(services, owner_id="explicit-worker", kinds=["skill.evolve"]))

    async def verify(conn):
        repo = EvolutionRepository(pg_env.tx)
        result = (await repo.command(conn, response.json()["job_id"]))["result"]
        assert result["sessions"] == 2
        assert result["candidates_queued"] == int(not update_during_planning)
        assert await repo.evidence(conn, "demo") == []
        assert (await CandidateRepository(pg_env.tx).require(conn, "legacy-demo"))["status"] == "archived"
        live = await CandidateRepository(pg_env.tx).live_for_skill(conn, "demo")
        if update_during_planning:
            assert live is None
        else:
            assert live["base_version"] == 2 and live["revision"] == 1
            assert set(await CandidateRepository(pg_env.tx).read_bundle(conn, live["candidate_id"])) == {"SKILL.md"}
            assert live["detail"]["manual_reprocess"]["session_ids"] == ["selected-1", "selected-2"]
            accepted = await repo.command(conn, live["validate_task_id"])
            assert {e["session_id"] for e in accepted["payload"]["evidence"]} == {"selected-1", "selected-2"}
            assert len(accepted["payload"]["detail"]["replay_cases"]) == 2
            dataset_id = accepted["payload"]["detail"]["replay_sampling"]["current_dataset_id"]
            datasets = DatasetRepository(pg_env.tx)
            dataset = await datasets.get(conn, dataset_id)
            assert not dataset["metadata"]["enabled_for_evolution"]
            assert dataset["source"]["kind"] == "manual_evolution"
            await datasets.save(conn, tenant_id="default", document=make_document(
                dataset_id="next-round", name="Later automatic round", skills=["demo"], cases=dataset["cases"],
                source={"kind": "evolution"},
            ))
            assert await datasets.historical_cases(conn, skill_id="demo", before_dataset_id="next-round") == []
            assert accepted["payload"]["mode"] == "evaluate"
        assert await conn.fetchval(
            f"SELECT count(*) FROM {pg_env.schema}.skill_evolution_sessions WHERE command_id='previous-cycle'",
        ) == 3
    _run(pg_env, "default", verify)
    assert len(seen) == 1
    assert {s["session_id"] for s in seen[0]["sessions"]} == {"selected-1", "selected-2"}
    assert {f["path"] for f in seen[0]["current"]["bundle"]["files"]} == {"SKILL.md"}
