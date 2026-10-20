"""Real-PG Experience/Pattern authority, outbox and Worker regressions."""

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
import pytest

from team_skills.config import load_skills_config
from team_skills.experiences.commands import ExperienceCommands
from team_skills.experiences.outbox import ExperienceOutbox
from team_skills.experiences.repository import ExperienceRepository
from team_skills.experiences.supervisor import accept_scan, experience_tick
from team_skills.handlers import register_handlers
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="real PG is required")


class Secrets:
    async def resolve(self, conn, *, secret_ref):
        assert secret_ref == "root-key-ref"
        return "runtime-secret"


class Remote:
    def __init__(self):
        self.files, self.calls = {}, []
        self.loss = False
        self.index = "complete"
        self.status = 200

    def __call__(self, request):
        self.calls.append(request)
        if request.method == "GET":
            content = self.files.get(request.url.params["uri"])
            return httpx.Response(200, content=content) if content else httpx.Response(404)
        body = json.loads(request.content)
        if request.url.path.endswith("/mkdir"):
            return httpx.Response(200, json={"status": "ok"})
        if self.status != 200:
            return httpx.Response(self.status, text="runtime-secret arbitrary upstream echo")
        self.files[body["uri"]] = body["content"]
        if self.loss:
            self.loss = False
            raise httpx.ReadTimeout("runtime-secret response lost")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "uri": body["uri"],
                    "content_updated": True,
                    "semantic_status": self.index,
                    "vector_status": self.index,
                },
            },
        )


def services(env, remote=None, **extras):
    return build_services(
        role="worker",
        tx=env.tx,
        handlers=register_handlers(),
        extras={
            "config_repo": ConfigRepository(env.tx),
            "secret_repo": Secrets(),
            "experience_sync_transport": httpx.MockTransport(remote or Remote()),
            **extras,
        },
    )


def configure(env, enabled=True):
    async def write(conn):
        await conn.execute(
            f"UPDATE {env.schema}.tenant_configs SET config=config || $1::jsonb WHERE module='skills'",
            json.dumps(
                {
                    "experience_sync": {
                        "enabled": enabled,
                        "endpoint": "https://ov.test",
                        "account_id": "account",
                        "user_id": "team",
                        "secret_ref": "root-key-ref",
                    }
                }
            ),
        )

    _run(env, "default", write)


def source(sid="s1", *, text="Run regressions before publishing", day=2, kind="exemplary", key="test_first"):
    return {
        "session_id": sid,
        "user_id": sid,
        "business_time": datetime(2026, 1, day, tzinfo=timezone.utc),
        "ingested_at": datetime(2026, 2, day, tzinfo=timezone.utc),
        "judge": {
            "overall_score": 0.9,
            "skill_experiences": [
                {
                    "skill_name": "demo",
                    "kind": kind,
                    "experience_key": key,
                    "description": text,
                }
            ],
        },
    }


def seed(env, rows=None, *, formal=False):
    rows = rows or [source()]

    async def write(conn):
        if formal:
            for s in rows:
                await conn.execute(
                    f"INSERT INTO {env.schema}.sessions "
                    "(tenant_id,session_id,user_id,judge,business_time,ingested_at) VALUES ('default',$1,$2,$3,$4,$5)",
                    s["session_id"],
                    s["user_id"],
                    json.dumps(s["judge"]),
                    s["business_time"],
                    s["ingested_at"],
                )
        else:
            await ExperienceRepository(env.tx).ingest(conn, tenant_id="default", sessions=rows)

    _run(env, "default", write)


def listing(env, **kwargs):
    return _run(env, "default", lambda c: ExperienceRepository(env.tx).list(c, **kwargs))


def outbox(env):
    async def read(conn):
        return [
            dict(r)
            for r in await conn.fetch(f"SELECT * FROM {env.schema}.experience_sync_outbox ORDER BY created_at,event_id")
        ]

    return _run(env, "default", read)


def drive(env, svc):
    async def run():
        for _ in range(10):
            if not await process_once(svc, owner_id="experience-a", kinds=("experience.sync",)):
                break

    env.tx.pool.run(run())


def tick(env, svc):
    return env.tx.pool.run(experience_tick(svc, tenant_id="default"))


def due(env):
    _run(
        env,
        "default",
        lambda c: c.execute(
            f"UPDATE {env.schema}.worker_tasks SET available_at=now()-interval '1 hour' WHERE kind='experience.sync'"
        ),
    )


@pytest.mark.postgres
def test_experience_dedupe_latest_description_and_atomic_intent(pg_env, monkeypatch):
    seed(pg_env)
    seed(pg_env, [source(), source("older", day=1, text="Old advice")])
    item = listing(pg_env)["items"][0]
    assert item["occurrence_count"] == 2
    assert item["description"] == source()["judge"]["skill_experiences"][0]["description"]
    assert item["first_observed_at"].startswith("2026-01-01")
    assert item["last_ingested_at"].startswith("2026-02-02")
    assert len(outbox(pg_env)) == 1
    original = ExperienceOutbox.append

    async def fail(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(ExperienceOutbox, "append", fail)
    with pytest.raises(RuntimeError):
        seed(pg_env, [source("new", day=3, text="Changed")])
    assert listing(pg_env)["items"][0]["occurrence_count"] == 2
    assert len(outbox(pg_env)) == 1


@pytest.mark.postgres
def test_projection_does_not_need_sync_skill_upload_or_evolution(pg_env):
    configure(pg_env, False)
    seed(pg_env, [source(), source("s2", kind="defect")], formal=True)
    svc = services(pg_env)
    assert tick(pg_env, svc)["state"] == "disabled"
    drive(pg_env, svc)
    assert listing(pg_env)["stats"]["total_experiences"] == 2
    assert len(outbox(pg_env)) == 1  # only successful lessons are projected to OV
    assert outbox(pg_env)[0]["status"] == "pending"


@pytest.mark.postgres
def test_rls_for_all_experience_tables(pg_env):
    seed(pg_env)

    async def check(conn):
        tables = (
            "skill_experiences",
            "experience_occurrences",
            "experience_patterns",
            "experience_commands",
            "experience_pattern_compilations",
            "experience_sync_outbox",
            "experience_scan_state",
        )
        return [await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.{t}") for t in tables]

    assert _run(pg_env, "alpha", check) == [0] * 7
    with pytest.raises(Exception, match="row-level security"):
        _run(pg_env, "default", lambda c: c.execute(f"UPDATE {pg_env.schema}.skill_experiences SET tenant_id='alpha'"))


@pytest.mark.postgres
def test_two_dispatchers_and_workers_produce_one_remote_file(pg_env):
    configure(pg_env)
    seed(pg_env)
    remote = Remote()
    one, two = services(pg_env, remote), services(pg_env, remote)

    async def run():
        results = await asyncio.gather(
            experience_tick(one, tenant_id="default", owner_id="a"),
            experience_tick(two, tenant_id="default", owner_id="b"),
        )
        await asyncio.gather(
            process_once(one, owner_id="a", kinds=("experience.sync",)),
            process_once(two, owner_id="b", kinds=("experience.sync",)),
        )
        return results

    dispatches = pg_env.tx.pool.run(run())
    drive(pg_env, two)
    tasks = _run(
        pg_env,
        "default",
        lambda c: c.fetch(f"SELECT task_id,status,error_code,available_at FROM {pg_env.schema}.worker_tasks"),
    )
    assert len(remote.files) == 1, (dispatches, [dict(t) for t in tasks], outbox(pg_env))
    assert outbox(pg_env)[0]["status"] == "synced"
    assert sum(r.url.path.endswith("/write") for r in remote.calls) == 1
    assert tick(pg_env, one)["enqueued"] == 0


@pytest.mark.postgres
def test_response_loss_restart_is_idempotent_and_secrets_not_persisted(pg_env):
    configure(pg_env)
    seed(pg_env)
    remote = Remote()
    remote.loss = True
    svc = services(pg_env, remote)
    tick(pg_env, svc)
    drive(pg_env, svc)
    assert outbox(pg_env)[0]["status"] == "queued" and len(remote.files) == 1
    due(pg_env)
    drive(pg_env, services(pg_env, remote))
    assert outbox(pg_env)[0]["status"] == "synced" and len(remote.files) == 1
    writes = [r for r in remote.calls if r.url.path.endswith("/write")]
    assert writes[0].headers["idempotency-key"] == writes[1].headers["idempotency-key"]
    assert "runtime-secret" not in str(outbox(pg_env))

    async def commands(conn):
        return str(await conn.fetch(f"SELECT payload FROM {pg_env.schema}.experience_commands"))

    assert "runtime-secret" not in _run(pg_env, "default", commands)


@pytest.mark.postgres
@pytest.mark.parametrize("status,retry", [(400, False), (401, False), (408, True), (429, True), (503, True)])
def test_sync_retry_classification_and_attempt_limit(pg_env, status, retry):
    configure(pg_env)
    seed(pg_env)
    remote = Remote()
    remote.status = status
    svc = services(pg_env, remote)
    tick(pg_env, svc)
    drive(pg_env, svc)
    assert outbox(pg_env)[0]["status"] == ("queued" if retry else "dead_letter")
    for _ in range(2):
        due(pg_env)
        drive(pg_env, svc)
    row = outbox(pg_env)[0]
    assert row["status"] == "dead_letter" and row["attempt"] == (3 if retry else 1)
    assert "runtime-secret" not in str(row)


@pytest.mark.postgres
def test_index_pending_never_claims_success(pg_env):
    configure(pg_env)
    seed(pg_env)
    remote = Remote()
    remote.index = "queued"
    svc = services(pg_env, remote)
    tick(pg_env, svc)
    drive(pg_env, svc)
    row = outbox(pg_env)[0]
    assert row["status"] == "queued" and row["content_written"]
    assert row["error_code"] == "OV_INDEX_NOT_READY"
    remote.index = "complete"
    due(pg_env)
    drive(pg_env, svc)
    assert outbox(pg_env)[0]["status"] == "synced"


@pytest.mark.postgres
def test_lost_owner_blocks_next_external_operation(pg_env):
    configure(pg_env)
    seed(pg_env)
    calls = []

    async def remote(request):
        calls.append(request)
        async with pg_env.tx.tenant_tx("default") as conn:
            await conn.execute(
                f"UPDATE {pg_env.schema}.worker_tasks SET owner_id='other' WHERE task_id LIKE 'experience-deliver:%'"
            )
        return httpx.Response(200, json={"status": "ok"})

    svc = services(pg_env, remote)
    tick(pg_env, svc)
    drive(pg_env, svc)
    assert len(calls) == 1 and outbox(pg_env)[0]["status"] == "queued"


@pytest.mark.postgres
def test_compile_api_async_idempotent_and_pg_read_from_another_pod(pg_env, tmp_path, monkeypatch):
    configure(pg_env)
    seed(pg_env, [source(), source("s2", key="test_before_release")])
    calls = []

    async def compiler(**kwargs):
        calls.append(kwargs)
        ids = [e["id"] for e in kwargs["experiences"]]
        return [
            {
                "pattern_key": "test_first",
                "title": "Run tests",
                "guidance": "Verify changes",
                "experience_ids": ids + ["invented"],
            }
        ]

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a, _pod(pg_env, tmp_path, monkeypatch, pod="b") as b:
        _login(a, bootstrap=True)
        _login(b, bootstrap=False)
        url = "/api/skill-experience-patterns/compile"
        headers = {**H, "Idempotency-Key": "compile-once"}
        accepted = a.post(url, headers=headers, json={"skill": "demo"})
        assert accepted.status_code == 202 and not calls
        assert b.post(url, headers=headers, json={"skill": "demo"}).json() == accepted.json()
        assert b.post(url, headers=headers, json={"skill": "demo", "force": True}).status_code == 409
        drive(pg_env, services(pg_env, experience_pattern_compiler=compiler))
        assert len(calls) == 1
        rows = b.get("/api/skill-experience-patterns", headers=H).json()
        assert rows["total"] == 1 and rows["stats"]["ready_patterns"] == 1
        assert len(rows["items"][0]["experience_ids"]) == 2
        assert a.get("/api/skill-experiences", headers=H).json()["stats"]["total_occurrences"] == 2
        assert a.get(accepted.json()["status_url"], headers=H).json()["status"] == "succeeded"
        jid = accepted.json()["job_id"]
        assert b.get(f"/api/experience-sync/jobs/{jid}/result", headers=H).json()["result"]["compiled"]
        # An unchanged evidence set needs no second model invocation.
        a.post(url, headers=H, json={"skill": "demo"})
        drive(pg_env, services(pg_env, experience_pattern_compiler=compiler))
        assert len(calls) == 1
        assert a.get("/api/skill-experience-patterns?kind=unknown", headers=H).status_code == 400
        assert a.post(url, headers=H, json={"skill": "demo", "root_key": "x"}).status_code == 400


@pytest.mark.postgres
def test_stale_compile_input_cannot_overwrite_patterns(pg_env, tmp_path, monkeypatch):
    seed(pg_env)

    async def compiler(**kwargs):
        async with pg_env.tx.tenant_tx("default") as conn:
            await ExperienceRepository(pg_env.tx).ingest(conn, tenant_id="default", sessions=[source("s2", day=3)])
        return [{"experience_ids": [kwargs["experiences"][0]["id"]]}]

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        r = a.post("/api/skill-experience-patterns/compile", headers=H, json={"skill": "demo"})
        drive(pg_env, services(pg_env, experience_pattern_compiler=compiler))
        assert a.get(r.json()["status_url"], headers=H).json()["error_code"] == "experience_source_changed"
        assert listing(pg_env, patterns=True)["total"] == 0


@pytest.mark.postgres
def test_scan_checkpoint_resume_and_sync_api(pg_env, tmp_path, monkeypatch):
    configure(pg_env)
    seed(pg_env, [source(f"s{i}") for i in range(105)], formal=True)
    svc = services(pg_env)
    original = ExperienceCommands.checkpoint
    calls = []

    async def failing(self, conn, cid, checkpoint):
        calls.append(checkpoint["processed"])
        await original(self, conn, cid, checkpoint)
        if len(calls) == 2:
            raise RuntimeError("crash on second page")

    monkeypatch.setattr(ExperienceCommands, "checkpoint", failing)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        r = a.post("/api/experience-sync/import", headers={**H, "Idempotency-Key": "scan-once"})
        assert r.status_code == 202
        drive(pg_env, svc)
        assert listing(pg_env)["stats"]["total_occurrences"] == 100
        monkeypatch.setattr(ExperienceCommands, "checkpoint", original)
        due(pg_env)
        drive(pg_env, services(pg_env))
        assert listing(pg_env)["stats"]["total_occurrences"] == 105
        state = a.get("/api/experience-sync/status", headers=H).json()
        assert state["manual"]["state"] == "completed" and state["counts"]["pending"] == 1
        assert a.post("/api/experience-sync/trigger", headers=H, json={"old_config": {}}).status_code == 400
        assert a.post("/api/experience-sync/trigger", headers=H).status_code == 202


@pytest.mark.postgres
def test_manual_scan_dedupe_after_watermark_moves(pg_env):
    svc = services(pg_env)

    async def accept(conn):
        cfg = await load_skills_config(svc.extras["config_repo"], conn)
        return await accept_scan(conn, svc, tenant_id="default", command_id="scan", operation="sync", config=cfg)

    one = _run(pg_env, "default", accept)
    drive(pg_env, svc)
    two = _run(pg_env, "default", accept)
    assert one["payload_hash"] == two["payload_hash"]


@pytest.mark.postgres
def test_frozen_target_and_revision_order_survive_later_configuration(pg_env):
    configure(pg_env)
    seed(pg_env)
    remote = Remote()
    svc = services(pg_env, remote)
    tick(pg_env, svc)
    seed(pg_env, [source("later", day=3, text="Improved")])

    async def change(conn):
        await conn.execute(
            f"UPDATE {pg_env.schema}.tenant_configs SET config="
            "jsonb_set(config,'{experience_sync,endpoint}','\"https://other.test\"') WHERE module='skills'"
        )

    _run(pg_env, "default", change)
    drive(pg_env, svc)
    assert {r.url.host for r in remote.calls} == {"ov.test"}
    tick(pg_env, svc)
    drive(pg_env, svc)
    writes = [r for r in remote.calls if r.url.path.endswith("/write")]
    assert [r.url.host for r in writes] == ["ov.test", "other.test"]
    assert [r["status"] for r in outbox(pg_env)] == ["synced", "synced"]


@pytest.mark.postgres
def test_pattern_and_outbox_rollback_together_then_candidate_rejection_is_recorded(pg_env, tmp_path, monkeypatch):
    from team_skills.candidates.repository import CandidateRepository
    from team_skills.config import BundleLimits
    from team_skills.experiences.patterns import normalize_patterns, source_fingerprint
    from tests.support.skills import _skill_md

    seed(pg_env, [source(), source("s2")])
    repo = ExperienceRepository(pg_env.tx)

    async def save(conn):
        inputs = await repo.compile_inputs(conn, "demo")
        patterns = normalize_patterns("demo", [{"experience_ids": [inputs[0]["id"]]}], inputs, [])
        await repo.save_patterns(
            conn, tenant_id="default", skill="demo", source_hash=source_fingerprint(inputs), patterns=patterns
        )

    original = ExperienceOutbox.append

    async def fail(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("injected pattern intent failure")

    monkeypatch.setattr(ExperienceOutbox, "append", fail)
    with pytest.raises(RuntimeError):
        _run(pg_env, "default", save)
    assert listing(pg_env, patterns=True)["total"] == 0
    monkeypatch.setattr(ExperienceOutbox, "append", original)
    _run(pg_env, "default", save)
    pattern = listing(pg_env, patterns=True)["items"][0]

    async def candidate(conn):
        return await CandidateRepository(pg_env.tx).create(
            conn,
            tenant_id="default",
            skill_name="demo",
            bundle={"SKILL.md": _skill_md("demo")},
            limits=BundleLimits(),
            detail={"pattern_refs": [{"skill_name": "demo", "pattern_id": pattern["pattern_id"]}]},
        )

    item = _run(pg_env, "default", candidate)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        for _ in range(2):
            assert a.post(f"/api/skill-candidates/{item['candidate_id']}/reject", headers=H).status_code == 200
    outcomes = listing(pg_env, patterns=True)["items"][0]["candidate_outcomes"]
    assert len(outcomes) == 1 and outcomes[0]["status"] == "rejected"
    assert len([r for r in outbox(pg_env) if r["subject_type"] == "patterns"]) == 2
