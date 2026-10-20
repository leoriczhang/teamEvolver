"""Real PostgreSQL acceptance tests for persisted Evolution and candidate review."""

from __future__ import annotations

import asyncio
import copy
import os

import pytest

from session_ingestion.schemas import session_payload_hash
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.replay_case_synthesis import hydrate_cross_session_cases
from team_skills.evolution.repository import EvolutionRepository
from team_skills.evolution.schedule import evolution_tick
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.candidates import candidate, seed_candidate
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"),
    reason="TEAMEVOLVER_TEST_PG_DSN is not configured",
)


def command(env, command_id):
    return _run(env, "default", lambda conn: EvolutionRepository(env.tx).command(conn, command_id))


async def accept_replay(job, config):
    return {"verdict": "accept", "accepted": True, "runtime_gate": {"status": "passed"}}


async def synthesize_replay_cases(**kwargs):
    source_ids = [session["session_id"] for session in kwargs["sessions"]]
    cases = [{
        "case_id": f"cross-session-{index + 1}",
        "query": f"Representative task {index + 1}",
        "skill_ids": [kwargs["skill_name"]],
        "checks": [
            {"id": f"R{number + 1:02d}", "kind": "output", "text": f"Condition {number + 1}"}
            for number in range(7)
        ],
        "materials": [],
        "provenance": {"session_ids": source_ids},
        "replay": {"progressive_disclosure": {"batch_size": 2}},
        "metadata": {},
    } for index in range(2)]
    return hydrate_cross_session_cases(cases, kwargs["sessions"], skill_name=kwargs["skill_name"])


def worker(env, **extras):
    return build_services(
        role="worker",
        tx=env.tx,
        handlers=register_handlers(),
        extras={
            "skill_replay": accept_replay,
            "replay_case_synthesizer": synthesize_replay_cases,
            **extras,
        },
    )


def drive(env, services, **options):
    return env.tx.pool.run(process_once(services, owner_id="worker-a", **options))


@pytest.mark.postgres
def test_candidate_routes_are_shared_audited_and_rejection_is_terminal(pg_env, tmp_path, monkeypatch):
    cid = seed_candidate(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a, _pod(pg_env, tmp_path, monkeypatch, pod="b") as b:
        _login(a, bootstrap=True)
        _login(b, bootstrap=False)
        url = f"/api/skill-candidates/{cid}"
        marked = a.patch(url + "/feedback", headers=H, json={"reviewed": True})
        assert marked.status_code == 200, marked.text
        assert b.get(url + "/detail", headers=H).json()["feedback"]["reviewed"]
        assert a.patch(url + "/feedback", headers=H, json={"adopted": True}).status_code == 400
        assert b.post(url + "/reject", headers=H).json()["feedback"]["rejected"]
        assert a.put(url + "/content", headers=H, json={"content": "late"}).status_code == 409
        events = _run(pg_env, "default", lambda c: CandidateRepository(pg_env.tx).events(c, cid))
        assert [e["kind"] for e in events] == ["created", "feedback", "transition"]
        assert a.get("/api/skill-candidates?scope=invalid", headers=H).status_code == 400


@pytest.mark.postgres
def test_acceptance_survives_api_restart_and_idempotent_submit(pg_env, tmp_path, monkeypatch):
    cid = seed_candidate(pg_env)
    seen = []

    async def replay(job, config):
        seen.append(config.get("min_group_users"))
        return await accept_replay(job, config)

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        headers = {**H, "Idempotency-Key": "evaluate-once"}
        url = f"/api/skill-candidates/{cid}/evaluate"
        response = a.post(url, headers=headers)
        assert response.status_code == 202, response.text
        jid = response.json()["job_id"]
        assert not seen
    with _pod(pg_env, tmp_path, monkeypatch, pod="b") as b:
        _login(b, bootstrap=False)
        assert b.post(url, headers=headers).json()["job_id"] == jid
        assert (
            b.post(f"/api/skill-candidates/{cid}/validate", headers=headers, json={"mode": "force"}).status_code == 409
        )

        # The accepted task pins the full effective config.
        async def change_config(conn):
            await conn.execute(
                f"UPDATE {pg_env.schema}.tenant_configs SET config=jsonb_set(config,'{{min_group_users}}','99') "
                "WHERE module='skills'",
            )

        _run(pg_env, "default", change_config)
        drive(pg_env, worker(pg_env, skill_replay=replay))
        assert seen == [2]
        assert b.get(response.json()["status_url"], headers=H).json()["status"] == "succeeded"
        assert candidate(pg_env, cid)["verdict"] == "accept"
        assert command(pg_env, jid)["result"]["status"] == "evaluated"


@pytest.mark.postgres
def test_edit_while_replay_runs_discards_stale_verdict(pg_env, tmp_path, monkeypatch):
    cid = seed_candidate(pg_env)

    async def replay(job, config):
        async with pg_env.tx.tenant_tx("default") as conn:
            await CandidateRepository(pg_env.tx).revise_content(
                conn,
                tenant_id="default",
                candidate_id=cid,
                bundle={"SKILL.md": _skill_md("demo", "new")},
                limits=BundleLimits(),
                actor="human",
                expected_revision=1,
            )
        return await accept_replay(job, config)

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        r = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": "auto"})
        drive(pg_env, worker(pg_env, skill_replay=replay))
    row = candidate(pg_env, cid)
    assert (row["revision"], row["status"], row["verdict"]) == (2, "open", "")
    assert command(pg_env, r.json()["job_id"])["result"]["status"] == "stale"


@pytest.mark.postgres
def test_publish_and_version_provenance_are_atomic(pg_env, tmp_path, monkeypatch):
    cid = seed_candidate(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        r = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": "auto"})
        assert r.status_code == 202
        drive(pg_env, worker(pg_env))
        row = candidate(pg_env, cid)
        assert row["status"] == "published" and row["adopted"] and not row["reviewed"]
        assert command(pg_env, r.json()["job_id"])["result"]["version"] == 1
        provenance = client.get("/api/skills/demo/versions/1", headers=H).json()["evolution"]
        assert provenance["job_id"] == cid and provenance["candidate_revision"] == 1


@pytest.mark.postgres
@pytest.mark.parametrize("mode", ["auto", "force"])
def test_runtime_failure_never_publishes_even_in_force_mode(pg_env, tmp_path, monkeypatch, mode):
    cid = seed_candidate(pg_env)

    async def replay(job, config):
        return {"verdict": "accept", "runtime_gate": {"status": "inconclusive"}}

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        r = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": mode})
        drive(pg_env, worker(pg_env, skill_replay=replay))
    assert candidate(pg_env, cid)["status"] == "open"
    assert command(pg_env, r.json()["job_id"])["result"]["status"] == "inconclusive"


@pytest.mark.postgres
def test_publish_transaction_failure_does_not_claim_published(pg_env, tmp_path, monkeypatch):
    cid = seed_candidate(pg_env)
    original = SkillLibraryRepository.commit_bundle

    async def fail_after_version(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("injected commit failure")

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": "auto"})
        monkeypatch.setattr(SkillLibraryRepository, "commit_bundle", fail_after_version)
        drive(pg_env, worker(pg_env))
        row = candidate(pg_env, cid)
        assert row["status"] == "validating" and not row["adopted"]
        assert command(pg_env, response.json()["job_id"])["result"] is None
        assert client.get("/api/skills", headers=H).json()["skills"] == []
        monkeypatch.setattr(SkillLibraryRepository, "commit_bundle", original)
        _run(
            pg_env,
            "default",
            lambda conn: conn.execute(
                f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE status='retry_wait'",
            ),
        )
        drive(pg_env, worker(pg_env))
        assert candidate(pg_env, cid)["published_version"] == 1


class Payloads:
    def __init__(self):
        self.data = {}

    def get_session(self, ref):
        return copy.deepcopy(self.data[ref])


def seed_session(env, payloads, sid, user, skill=""):
    session = {
        "session_id": sid,
        "_summary": "A reusable lesson.",
        "turns": [
            {
                "turn_num": 1,
                "prompt_text": "Do this",
                "response_text": "Done",
                "read_skills": [skill] if skill else [],
            }
        ],
        "_judge_scores": {
            "source": "llm",
            **{
                k: 0.8
                for k in (
                    "overall_score",
                    "task_completion",
                    "response_quality",
                    "efficiency",
                    "tool_usage",
                )
            },
        },
        "value_judge": {"mode": "merged", "decision": "valuable", "confidence": 0.9, "reason": "Reusable"},
    }
    payloads.data[sid] = session

    async def seed(conn):
        await conn.execute(
            f"INSERT INTO {env.schema}.sessions "
            "(tenant_id,session_id,user_id,decision,content_ref,content_hash) "
            "VALUES ('default',$1,$2,'valuable',$1,$3)",
            sid,
            user,
            session_payload_hash(session),
        )

    _run(env, "default", seed)


@pytest.mark.postgres
def test_evolution_worker_creates_candidate_and_consumes_once(pg_env, tmp_path, monkeypatch):
    payloads = Payloads()
    seed_session(pg_env, payloads, "s1", "alice")
    seed_session(pg_env, payloads, "s2", "bob")
    calls = []

    async def planner(**kwargs):
        calls.append(kwargs["name"])
        return {
            "action": "create_skill",
            "skill": {
                "name": "demo",
                "description": "Use for testing",
                "content": "Steps",
            },
            "rationale": "shared lesson",
        }

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        first = client.post("/api/skill-evolution/trigger", headers=H, json={})
        assert first.status_code == 202, first.text
        services = worker(pg_env, skill_planner=planner, session_payloads=payloads)
        drive(pg_env, services, kinds=["skill.evolve"])
        result = command(pg_env, first.json()["job_id"])["result"]
        assert result["sessions"] == 2 and result["candidates_queued"] == 1
        rows = client.get("/api/skill-candidates", headers=H).json()["candidates"]
        assert len(rows) == 1 and rows[0]["review_status"] == "validating"
        item = candidate(pg_env, rows[0]["job_id"])
        assert len(item["detail"]["replay_cases"]) == 2
        assert all(
            len(case["provenance"]["session_ids"]) >= 2
            and len(case["checks"]) == 7
            and case["provenance"]["synthesis_mode"] == "cross_session"
            for case in item["detail"]["replay_cases"]
        )
        drive(pg_env, services, kinds=["skill.validate"])
        client.post("/api/skill-evolution/trigger", headers=H, json={})
        drive(pg_env, services, kinds=["skill.evolve"])
        assert len(calls) == 1
        assert client.get("/history?session_id=s1", headers=H).json()["cycles"][0]["result"]["sessions"] == 2


@pytest.mark.postgres
def test_automatic_evolution_and_publish_policies(pg_env, tmp_path, monkeypatch):
    payloads = Payloads()
    seed_session(pg_env, payloads, "auto-s1", "alice")
    seed_session(pg_env, payloads, "auto-s2", "bob")

    async def planner(**kwargs):
        return {
            "action": "create_skill",
            "skill": {"name": "automatic-demo", "description": "Automatic policy", "content": "Steps"},
        }

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        settings = client.get("/api/skill-evolution/settings", headers=H).json()
        saved = client.put(
            "/api/skill-evolution/settings",
            headers=H,
            json={
                "revision": settings["revision"],
                "evolve": {
                    **settings["evolve"],
                    "evolution_trigger_mode": "automatic",
                    "evolution_interval_seconds": 60,
                },
                "validation": {"auto_evaluate": True, "publish_mode": "automatic"},
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["validation"]["publish_mode"] == "automatic"
        services = worker(pg_env, skill_planner=planner, session_payloads=payloads)
        services.extras["config_repo"] = ConfigRepository(pg_env.tx)
        dispatched = pg_env.tx.pool.run(evolution_tick(services, tenant_id="default"))
        assert dispatched["state"] == "enqueued"
        assert pg_env.tx.pool.run(evolution_tick(services, tenant_id="default"))["state"] == "active"
        drive(pg_env, services, kinds=["skill.evolve"])
        evolution = command(pg_env, dispatched["job_id"])
        assert evolution["result"] and evolution["result"]["candidates_queued"] == 1, evolution
        drive(pg_env, services, kinds=["skill.validate"])
        rows = client.get("/api/skill-candidates?scope=all", headers=H).json()["candidates"]
        assert rows[0]["review_status"] == "published"


@pytest.mark.postgres
def test_missing_analysis_fails_before_candidate_is_created(pg_env, tmp_path, monkeypatch):
    payloads = Payloads()
    seed_session(pg_env, payloads, "s1", "alice")
    payloads.data["s1"].pop("_summary")
    _run(
        pg_env,
        "default",
        lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.sessions SET content_hash=$1 WHERE session_id='s1'",
            session_payload_hash(payloads.data["s1"]),
        ),
    )
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        client.post("/api/skill-evolution/trigger", headers=H, json={})
        drive(pg_env, worker(pg_env, skill_planner=lambda **kw: None, session_payloads=payloads))
        assert client.get("/api/skill-candidates", headers=H).json()["total"] == 0


@pytest.mark.postgres
def test_retry_resumes_completed_branches_without_revising_them(pg_env, tmp_path, monkeypatch):
    payloads = Payloads()
    for number, skill in enumerate(("first", "first", "second", "second")):
        seed_session(pg_env, payloads, f"s{number}", f"user{number}", skill)
    calls = []

    async def planner(**kwargs):
        name = kwargs["name"]
        calls.append(name)
        if name == "second" and calls.count(name) == 1:
            raise RuntimeError("temporary model failure")
        return {
            "action": "create_skill",
            "skill": {
                "name": name,
                "description": "Use when testing",
                "content": "Steps",
            },
        }

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/skill-evolution/trigger", headers=H, json={})
        services = worker(pg_env, skill_planner=planner, session_payloads=payloads)
        drive(pg_env, services, kinds=["skill.evolve"])
        assert command(pg_env, response.json()["job_id"])["result"] is None
        _run(
            pg_env,
            "default",
            lambda conn: conn.execute(
                f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE status='retry_wait'",
            ),
        )
        drive(pg_env, services, kinds=["skill.evolve"])
        rows = client.get("/api/skill-candidates", headers=H).json()["candidates"]
    assert calls == ["first", "second", "second"]
    assert sorted((r["skill_name"], r["candidate_revision"]) for r in rows) == [("first", 1), ("second", 1)]
    assert command(pg_env, response.json()["job_id"])["result"]["sessions"] == 4


@pytest.mark.postgres
def test_worker_renews_long_attempt_and_second_pod_cannot_claim(pg_env):
    async def setup(conn):
        await worker(pg_env).tasks.enqueue(conn, tenant_id="default", task_id="long", kind="skill.validate")

    _run(pg_env, "default", setup)
    observed = []

    async def long_handler(task, services):
        await asyncio.sleep(0.18)
        async with services.tx.tenant_tx("default") as conn:
            await services.tasks.require_current(conn, task)
        from teamEvolver.runtime import HandlerResult

        return HandlerResult.succeed()

    services = build_services(role="worker", tx=pg_env.tx, handlers={"skill.validate": long_handler})

    async def run():
        running = asyncio.create_task(process_once(services, owner_id="a", lease_seconds=0.06))
        await asyncio.sleep(0.12)
        observed.append(await process_once(services, owner_id="b", lease_seconds=0.06))
        await running

    pg_env.tx.pool.run(run())
    assert observed == [None]
    assert _run(pg_env, "default", lambda c: services.tasks.get(c, task_id="long"))["status"] == "succeeded"


@pytest.mark.postgres
def test_new_evolution_tables_are_tenant_isolated(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/skill-evolution/trigger", headers=H, json={})
    assert (
        _run(
            pg_env,
            "alpha",
            lambda c: EvolutionRepository(pg_env.tx).command(
                c,
                response.json()["job_id"],
            ),
        )
        is None
    )


@pytest.mark.postgres
def test_superseded_attempt_stops_before_new_side_effect(pg_env):
    effects = []
    ready = asyncio.Event()

    async def handler(task, services):
        ready.set()
        await asyncio.sleep(0.3)
        effects.append("late write")
        from teamEvolver.runtime import HandlerResult

        return HandlerResult.succeed()

    services = build_services(role="worker", tx=pg_env.tx, handlers={"skill.validate": handler})
    _run(
        pg_env,
        "default",
        lambda conn: services.tasks.enqueue(
            conn,
            tenant_id="default",
            task_id="superseded",
            kind="skill.validate",
        ),
    )

    async def run():
        attempt = asyncio.create_task(process_once(services, owner_id="old", lease_seconds=0.09))
        await ready.wait()
        async with pg_env.tx.tenant_tx("default") as conn:
            await conn.execute(
                f"UPDATE {pg_env.schema}.worker_tasks SET fence=fence+1,owner_id='new' WHERE task_id='superseded'",
            )
        await attempt

    pg_env.tx.pool.run(run())
    assert effects == []
    row = _run(pg_env, "default", lambda conn: services.tasks.get(conn, task_id="superseded"))
    assert row["owner_id"] == "new" and row["status"] == "running"
