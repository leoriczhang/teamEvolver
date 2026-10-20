"""Real PG Miner receipts survive API/Worker restarts with one watermark advance."""

import asyncio
import base64
import os

import pytest

from team_miner.knowledge_miner.config import DEFAULTS
from team_miner.knowledge_miner.handlers import register_handlers
from team_miner.knowledge_miner.state import MinerState
from teamEvolver.openviking import OpenVikingResult
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.openviking import OV
from tests.support.postgres import _run

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="requires PostgreSQL")


def services(env, ov):
    return build_services(
        role="worker", tx=env.tx, handlers=register_handlers(), extras={"miner_client_factory": lambda *_: ov}
    )


class MissingWorkspaceOV(OV):
    def __init__(self):
        super().__init__()
        self.directories = {"viking://resources"}

    async def request(self, method, path, **kwargs):
        if path == "/api/v1/fs/mkdir":
            self.calls.append((method, path, kwargs))
            uri = kwargs["json_body"]["uri"]
            parent = uri.rsplit("/", 1)[0]
            if parent not in self.directories:
                return OpenVikingResult(False, 404, error=f"missing parent: {parent}")
            self.directories.add(uri)
            return OpenVikingResult(True, 200, {})
        if path == "/api/v1/fs/tree":
            self.calls.append((method, path, kwargs))
            root = kwargs["params"]["uri"]
            if root not in self.directories:
                return OpenVikingResult(False, 404, error=f"missing root: {root}")
            return OpenVikingResult(True, 200, [])
        return await super().request(method, path, **kwargs)


class MissingMiningSkillOV(OV):
    def __init__(self):
        super().__init__()
        self.skill_installed = False

    async def request(self, method, path, **kwargs):
        if method == "GET" and path == "/api/v1/skills/llm-wiki":
            self.calls.append((method, path, kwargs))
            return OpenVikingResult(
                self.skill_installed,
                200 if self.skill_installed else 404,
                {"revision": "one"} if self.skill_installed else None,
                "" if self.skill_installed else "missing skill",
            )
        if method == "POST" and path == "/api/v1/skills":
            self.calls.append((method, path, kwargs))
            self.skill_installed = True
            return OpenVikingResult(True, 200, {"skill_name": "llm-wiki"})
        if method == "POST" and path == "/api/v1/compile" and not self.skill_installed:
            self.calls.append((method, path, kwargs))
            return OpenVikingResult(False, 424, error="missing skill")
        return await super().request(method, path, **kwargs)


@pytest.mark.postgres
def test_miner_tree_bootstraps_missing_workspace_roots(pg_env, tmp_path, monkeypatch):
    ov = MissingWorkspaceOV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        client.app.state.services.extras["miner_client_factory"] = lambda *_: ov
        response = client.get("/api/knowledge-mining/tree?kind=source", headers=H)

    assert response.status_code == 200, response.text
    assert DEFAULTS["source_uri"] in ov.directories
    assert DEFAULTS["target_uri"] in ov.directories


@pytest.mark.postgres
def test_miner_compile_installs_missing_default_skill(pg_env, tmp_path, monkeypatch):
    ov = MissingMiningSkillOV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/knowledge-mining/tasks", headers=H, json={})
        assert response.status_code == 202, response.text
        task_id = response.json()["job_id"]

    pg_env.tx.pool.run(process_once(services(pg_env, ov), owner_id="a"))

    assert ov.skill_installed is True
    status = _run(
        pg_env,
        "default",
        lambda conn: conn.fetchval(
            f"SELECT status FROM {pg_env.schema}.worker_tasks WHERE task_id=$1",
            task_id,
        ),
    )
    assert status == "succeeded"


@pytest.mark.postgres
def test_miner_resume_does_not_resubmit_or_double_advance_watermark(pg_env, tmp_path, monkeypatch):
    ov = OV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/knowledge-mining/tasks", headers={**H, "Idempotency-Key": "once"}, json={})
        assert response.status_code == 202, response.text
        rid = response.json()["job_id"]
    worker = services(pg_env, ov)
    original = MinerState.finalize

    async def crash(*args, **kwargs):
        raise RuntimeError("crash before finalize")

    monkeypatch.setattr(MinerState, "finalize", crash)
    pg_env.tx.pool.run(process_once(worker, owner_id="a"))
    assert len(ov.submits) == 1
    monkeypatch.setattr(MinerState, "finalize", original)
    _run(
        pg_env,
        "default",
        lambda c: c.execute(
            f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now(),lease_until=now()-interval '1 second' "
            "WHERE task_id=$1",
            rid,
        ),
    )
    pg_env.tx.pool.run(process_once(worker, owner_id="b"))
    pg_env.tx.pool.run(process_once(worker, owner_id="b"))
    state = MinerState(pg_env.tx, "default")
    receipt = _run(pg_env, "default", lambda c: state.get(c, "receipt", rid))
    watermark = _run(pg_env, "default", lambda c: state.get(c, "watermark", state.scope(receipt["settings"])))
    assert len(ov.submits) == 1
    assert watermark["last_compile_time"] == receipt["started_at"]
    assert _run(pg_env, "default", lambda c: state.finalize(c, receipt, "succeeded")) is False
    assert (
        _run(
            pg_env,
            "default",
            lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.worker_tasks WHERE kind='miner.rebuild_index'"),
        )
        == 1
    )
    assert _run(pg_env, "alpha", lambda c: MinerState(pg_env.tx, "alpha").get(c, "receipt", rid)) is None
    with _pod(pg_env, tmp_path, monkeypatch, pod="b") as client:
        _login(client, bootstrap=False)
        client.app.state.services.extras["miner_client_factory"] = lambda *_: ov
        task = client.get("/api/knowledge-mining/tasks/upstream", headers=H)
        assert task.status_code == 200 and task.json()["job_id"] == rid


@pytest.mark.postgres
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_failure_and_cancel_never_advance_miner_watermark(pg_env, tmp_path, monkeypatch, terminal):
    ov = OV()
    ov.status = terminal
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/knowledge-mining/tasks", headers=H, json={})
        rid = response.json()["job_id"]
    pg_env.tx.pool.run(process_once(services(pg_env, ov), owner_id="a"))
    state = MinerState(pg_env.tx, "default")
    receipt = _run(pg_env, "default", lambda c: state.get(c, "receipt", rid))
    assert _run(pg_env, "default", lambda c: state.get(c, "watermark", state.scope(receipt["settings"]))) is None
    assert (
        _run(
            pg_env,
            "default",
            lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.worker_tasks WHERE kind='miner.rebuild_index'"),
        )
        == 0
    )


@pytest.mark.postgres
def test_miner_cancel_from_second_api_and_worker_reconciles(pg_env, tmp_path, monkeypatch):
    ov = OV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        rid = client.post("/api/knowledge-mining/tasks", headers=H, json={}).json()["job_id"]
    worker = services(pg_env, ov)

    async def race():
        ov.block = asyncio.Event()
        work = asyncio.create_task(process_once(worker, owner_id="a"))
        for _ in range(200):
            if ov.submits:
                break
            await asyncio.sleep(0.01)
        state = MinerState(pg_env.tx, "default")
        for _ in range(200):
            async with pg_env.tx.tenant_tx("default") as conn:
                if await state.get(conn, "receipt", rid):
                    break
            await asyncio.sleep(0.01)

        def cancel():
            with _pod(pg_env, tmp_path, monkeypatch, pod="b") as client:
                _login(client, bootstrap=False)
                client.app.state.services.extras["miner_client_factory"] = lambda *_: ov
                return client.post("/api/knowledge-mining/tasks/upstream/cancel", headers=H, json={})

        response = await asyncio.to_thread(cancel)
        assert response.status_code == 200, response.text
        ov.block.set()
        await work

    pg_env.tx.pool.run(race())
    state = MinerState(pg_env.tx, "default")
    receipt = _run(pg_env, "default", lambda c: state.get(c, "receipt", rid))
    assert receipt["status"] == "cancelled"
    assert _run(pg_env, "default", lambda c: state.get(c, "watermark", state.scope(receipt["settings"]))) is None


@pytest.mark.postgres
def test_miner_upload_is_persistent_and_normalized_on_worker(pg_env, tmp_path, monkeypatch):
    ov = OV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        raw = {"files": [{"path": "notes.txt", "content_b64": base64.b64encode("你好".encode()).decode()}]}
        response = client.post("/api/knowledge-mining/upload", headers=H, json=raw)
        assert response.status_code == 202, response.text
        rid = response.json()["job_id"]
        assert not ov.calls
    pg_env.tx.pool.run(process_once(services(pg_env, ov), owner_id="a"))
    writes = [call for call in ov.calls if call[1] == "/api/v1/content/write"]
    assert len(writes) == 1
    assert writes[0][2]["json_body"]["uri"] == DEFAULTS["source_uri"] + "/notes.md"
    assert "你好" in writes[0][2]["json_body"]["content"]
    result = _run(pg_env, "default", lambda c: MinerState(pg_env.tx, "default").get(c, "result", rid))
    assert result["files"][0]["source_format"] == "txt"


@pytest.mark.postgres
def test_late_index_cannot_replace_newer_compile_index(pg_env):
    from team_miner.knowledge_miner.state import digest

    state = MinerState(pg_env.tx, "default")
    settings = {**DEFAULTS, "account_id": "account"}

    async def seed(conn):
        await state.put(conn, "watermark", state.scope(settings), {"task_id": "new", "last_compile_time": "new"})
        await state.put(conn, "index", state.scope(settings), {"compile_task_id": "new", "pages": ["latest"]})
        await state.put(conn, "payload", "old-index", {"settings": settings, "compile_task_id": "old"})
        await pg_env_services.tasks.enqueue(
            conn,
            tenant_id="default",
            task_id="old-index",
            kind="miner.rebuild_index",
            subject_type="miner_index",
            subject_id=settings["target_uri"],
            payload_ref="old-index",
            payload_hash=digest(settings),
        )

    pg_env_services = services(pg_env, OV())
    _run(pg_env, "default", seed)
    pg_env.tx.pool.run(process_once(pg_env_services, owner_id="late"))
    assert _run(pg_env, "default", lambda c: state.get(c, "index", state.scope(settings)))["pages"] == ["latest"]
