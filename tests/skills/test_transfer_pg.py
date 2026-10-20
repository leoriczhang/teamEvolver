"""Real PG transfer acceptance, atomic import, frozen export and lease recovery."""

import asyncio
import json
import os

import pytest

from team_skills.config import load_skills_config
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from team_skills.transfer.commands import TransferCommands
from team_skills.transfer.packages import discover, read_zip
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from teamEvolver.security.repository import SecretRepository
from teamEvolver.security.secrets import SecretBox
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run
from tests.support.transfer import bundle, encoded

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="real PG is required")


def services(env, **extras):
    return build_services(
        role="worker",
        tx=env.tx,
        handlers=register_handlers(),
        extras={
            "config_repo": ConfigRepository(env.tx),
            "secret_repo": SecretRepository(env.tx, SecretBox("test-master")),
            **extras,
        },
    )


def accept(env, svc, cid="import", operation="import", body=None):
    async def write(conn):
        return await TransferCommands(svc).accept(
            conn,
            tenant_id="default",
            command_id=cid,
            operation=operation,
            body=body or {"channel": "zip", "options": encoded(bundle())},
            config=await load_skills_config(svc.extras["config_repo"], conn),
            actor="tester",
        )

    return _run(env, "default", write)


def drive(env, svc):
    return env.tx.pool.run(process_once(svc, owner_id="worker", kinds=("skill.transfer",)))


def command(env, svc, cid):
    return _run(env, "default", lambda c: TransferCommands(svc).get(c, cid))


def job(env, svc, cid):
    return _run(env, "default", lambda c: svc.tasks.get(c, task_id=cid))


@pytest.mark.postgres
def test_http_async_roundtrip_cross_pod_frozen_versions_and_idempotency(pg_env, tmp_path, monkeypatch):
    svc = services(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a, _pod(pg_env, tmp_path, monkeypatch, pod="b") as b:
        _login(a, bootstrap=True)
        _login(b, bootstrap=False)
        body = {"channel": "zip", "options": encoded(bundle())}
        headers = {**H, "Idempotency-Key": "once"}
        response = a.post("/api/skills/import", headers=headers, json=body)
        assert response.status_code == 202, response.text
        assert a.get("/api/skills", headers=H).json()["skills"] == []
        assert b.post("/api/skills/import", headers=headers, json=body).json() == response.json()
        assert b.post("/api/skills/import", headers=headers, json={**body, "conflict": "skip"}).status_code == 409
        drive(pg_env, svc)
        assert b.get("/api/skills/transfer/skills", headers=H).json()["skills"][0]["version"] == 1
        exported = b.post("/api/skills/export", headers=H, json={"channel": "zip", "names": ["demo"]})
        assert exported.status_code == 202
        a.post("/api/skills", headers=H, json={"name": "demo", "description": "Demo", "body": "edited"})
        drive(pg_env, svc)
        cid = exported.json()["job_id"]
        result = a.get(f"/api/skills/transfer/jobs/{cid}/result", headers=H).json()["result"]
        archive = b.get(result["download_url"], headers=H)
        assert archive.status_code == 200
        assert discover(read_zip(archive.content))[0].files == bundle().files
        assert len(a.get("/api/skills/transfer/channels", headers=H).json()["channels"]) == 3
        assert a.post("/api/skills/import", headers=H, files={"f": b"PK\xff"}).status_code == 415
        for options in (
            {"url": "https://user:password@git.test"},
            {"url": "https://git.test?token=secret"},
            {"url": "/tmp/local"},
            {"url": "https://git.test", "_check": "no"},
        ):
            denied = a.post("/api/skills/import", headers=H, json={"channel": "git", "options": options})
            assert denied.status_code == 400


@pytest.mark.postgres
def test_import_is_all_or_nothing_including_outbox_and_result(pg_env, monkeypatch):
    svc = services(pg_env)
    accept(pg_env, svc, body={"channel": "zip", "options": encoded(bundle("aaa"), bundle("zzz"))})
    original = SkillLibraryRepository.commit_bundle

    async def fail(self, conn, **kwargs):
        if kwargs["name"] == "zzz":
            raise RuntimeError("private-token-must-not-leak")
        return await original(self, conn, **kwargs)

    monkeypatch.setattr(SkillLibraryRepository, "commit_bundle", fail)
    drive(pg_env, svc)
    assert command(pg_env, svc, "import")["result"] is None

    async def counts(conn):
        return [
            await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.{t}")
            for t in ("skills", "skill_versions", "skill_mutations")
        ]

    assert _run(pg_env, "default", counts) == [0, 0, 0]
    assert "private-token" not in str(job(pg_env, svc, "import"))


@pytest.mark.postgres
def test_conflict_modes_noop_tenant_isolation_and_atomic_error(pg_env):
    svc = services(pg_env)
    accept(pg_env, svc)
    drive(pg_env, svc)
    for cid, body in (
        ("same", {"channel": "zip", "options": encoded(bundle())}),
        ("skip", {"channel": "zip", "options": encoded(bundle(body="new")), "conflict": "skip"}),
        ("error", {"channel": "zip", "options": encoded(bundle("aaa"), bundle()), "conflict": "error"}),
    ):
        accept(pg_env, svc, cid, body=body)
        drive(pg_env, svc)
    assert command(pg_env, svc, "same")["result"]["imported"][0]["status"] == "unchanged"
    assert command(pg_env, svc, "skip")["result"]["skipped"][0]["name"] == "demo"
    assert job(pg_env, svc, "error")["status"] == "failed"
    assert _run(pg_env, "default", lambda c: SkillLibraryRepository(pg_env.tx).get_skill(c, "aaa")) is None
    assert _run(pg_env, "alpha", lambda c: TransferCommands(svc).get(c, "import")) is None
    assert _run(pg_env, "alpha", lambda c: SkillLibraryRepository(pg_env.tx).list_skills(c)) == []


@pytest.mark.postgres
def test_credentials_encrypted_and_payload_hash_guard(pg_env):
    svc = services(pg_env)
    accepted = accept(
        pg_env,
        svc,
        body={
            "channel": "git",
            "options": {
                "url": "https://git.test/repo",
                "token": "never-in-payload",
            },
        },
    )
    assert "never-in-payload" not in json.dumps(accepted)

    async def read(conn):
        row = await conn.fetchrow(f"SELECT * FROM {pg_env.schema}.secret_refs")
        assert "never-in-payload" not in str(row)
        assert await svc.extras["secret_repo"].resolve(conn, secret_ref=row["secret_ref"]) == "never-in-payload"
        await conn.execute(f'UPDATE {pg_env.schema}.skill_transfers SET payload=payload || \'{{"actor":"forged"}}\'')

    _run(pg_env, "default", read)
    drive(pg_env, svc)
    assert job(pg_env, svc, "import")["error_code"] == "transfer_payload_mismatch"


@pytest.mark.postgres
def test_unknown_upload_never_blindly_repeats_and_reconciles_readonly(pg_env):
    calls = []

    class Remote:
        def write(self, packages, options):
            calls.append("write")
            options["_checkpoint"]({"effect": "upload"})
            raise RuntimeError("secret response lost")

        def reconcile(self, checkpoint, options):
            calls.append("read")
            return {"channel": "marketplace", "exported": ["demo"]}

    svc = services(pg_env)
    accept(pg_env, svc)
    drive(pg_env, svc)
    svc = services(pg_env, skill_transfer_adapters={"marketplace": Remote()})
    accept(pg_env, svc, "export", "export", {"channel": "marketplace", "names": ["demo"]})
    drive(pg_env, svc)
    assert job(pg_env, svc, "export")["status"] == "reconciling"
    _run(
        pg_env,
        "default",
        lambda c: c.execute(
            f"UPDATE {pg_env.schema}.worker_tasks SET lease_until=now()-interval '1 hour' WHERE task_id='export'"
        ),
    )
    drive(pg_env, svc)
    assert calls == ["write", "read"]
    assert job(pg_env, svc, "export")["status"] == "succeeded"


@pytest.mark.postgres
def test_lost_lease_prevents_next_external_effect(pg_env):
    calls = []

    class Remote:
        def read(self, options):
            calls.append("read")
            _run(
                pg_env,
                "default",
                lambda c: c.execute(f"UPDATE {pg_env.schema}.worker_tasks SET owner_id='other' WHERE task_id='import'"),
            )
            options["_check"]()
            calls.append("unsafe")
            return [bundle()]

    svc = services(pg_env, skill_transfer_adapters={"git": Remote()})
    accept(pg_env, svc, body={"channel": "git", "options": {"url": "https://git.test/repo"}})
    drive(pg_env, svc)
    assert calls == ["read"]
    assert command(pg_env, svc, "import")["result"] is None


@pytest.mark.postgres
def test_two_workers_apply_once(pg_env):
    svc = services(pg_env)
    accept(pg_env, svc)

    async def race():
        return await asyncio.gather(
            process_once(svc, owner_id="a", kinds=("skill.transfer",)),
            process_once(services(pg_env), owner_id="b", kinds=("skill.transfer",)),
        )

    results = pg_env.tx.pool.run(race())
    assert sum(r is not None for r in results) == 1
    assert command(pg_env, svc, "import")["result"]["imported"][0]["version"] == 1
