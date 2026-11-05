"""Source requests remain short; external edits are fenced, bounded and resumable."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from team_miner.skill_miner.api import sources
from team_miner.skill_miner.config import MiningSettings
from team_miner.skill_miner.source_operations import execute, prepared_uploads, upload

ROOT = "viking://resources/source"
RECORD = {"source_id": "source-1", "account": "team", "uri": ROOT, "tags": ["skill"]}


class Repo:
    def get_source(self, name):
        if name != "source-1":
            raise KeyError(name)
        return dict(RECORD)

    def list_source_records(self):
        return [dict(RECORD)]


class Remote:
    def __init__(self, **kwargs):
        self.files = {}
        self.repository = Repo()
        self.settings = MiningSettings(1, 0, {}, {"account_id": "team"})
        self.task = None
        self.fail_after_write = False
        self.writes = []
        self.__dict__.update(kwargs)

    async def _knowledge_tree(self, account, root):
        return {"exists": True, "entries": [{"uri": uri, "size": len(content), "is_dir": False}
                                            for uri, content in self.files.items()]}

    async def _knowledge_request(self, account, method, path, **kwargs):
        if path == "/api/v1/fs/mkdir":
            return {}
        if path == "/api/v1/content/read":
            return {"content": self.files[kwargs["params"]["uri"]]}
        if path == "/api/v1/content/write":
            body = kwargs["json"]
            uri = body["uri"]
            if body["mode"] == "create" and uri in self.files:
                raise HTTPException(409, "ALREADY_EXISTS")
            self.files[uri] = body["content"]
            self.writes.append(dict(body))
            if self.fail_after_write:
                self.fail_after_write = False
                raise TimeoutError("connection lost after upstream accepted the write")
            return {"ok": True}
        raise AssertionError((method, path))


def encoded(name, content="source"):
    return {"name": name, "content_b64": base64.b64encode(content.encode()).decode()}


def test_source_save_rejects_concurrent_content_without_overwriting_remote():
    remote = Remote()
    remote.files[ROOT + "/lesson.md"] = "another editor committed"
    with pytest.raises(HTTPException) as error:
        asyncio.run(execute("source.save", {"source_id": "source-1", "uri": ROOT + "/lesson.md",
                                           "original_content": "old", "content": "new"}, remote))
    assert error.value.status_code == 409
    assert remote.writes == []


@pytest.mark.parametrize("name", ["../escape.md", "/absolute.md", ".hidden.md", "C:/file.md"])
def test_upload_rejects_paths_before_any_external_write(name):
    remote = Remote()
    with pytest.raises(ValueError):
        asyncio.run(upload(remote, RECORD, {"files": [encoded(name)]}))
    assert remote.writes == []


def test_upload_rejects_conversion_collisions_before_external_write():
    with pytest.raises(ValueError, match="同一路径"):
        asyncio.run(prepared_uploads({"files": [encoded("same.txt"), encoded("same.md")]}))


@pytest.mark.postgres
def test_upload_retry_uses_pg_frozen_destinations_after_lost_write_response(pg_env):
    async def require_current(_conn, _task, **_kwargs):
        return None

    services = SimpleNamespace(tx=pg_env.tx, tasks=SimpleNamespace(require_current=require_current))
    task = SimpleNamespace(task_id="upload-1")
    remote = Remote(services=services, tenant="alpha", task=task, fail_after_write=True)
    body = {"files": [encoded("lesson.txt", "immutable text")]}
    with pytest.raises(TimeoutError):
        asyncio.run(upload(remote, RECORD, body))
    assert list(remote.files) == [ROOT + "/lesson.md"]
    resumed = Remote(services=services, tenant="alpha", task=task)
    resumed.files = remote.files
    result = asyncio.run(upload(resumed, RECORD, body))
    assert result["file_count"] == 1
    assert result["written"][0]["result"]["replayed"] is True
    assert list(resumed.files) == [ROOT + "/lesson.md"]
    assert resumed.writes == []


def test_source_routes_reject_anonymous_before_database_access():
    app = FastAPI()
    app.include_router(sources.create_router(SimpleNamespace()))
    with TestClient(app) as client:
        result = client.get("/api/mining/input-sources")
    assert result.status_code == 403


def test_source_api_accepts_work_without_probe_and_checks_source_boundary(monkeypatch):
    accepted = []

    async def settings(_self, _request, account=""):
        if account not in {"", "team"}:
            raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
        return MiningSettings(1, 0, {}, {"account_id": "team"})

    async def accept(_request, _services, operation, raw, _settings, **_kwargs):
        accepted.append((operation, raw))
        return {"job_id": "operation-1", "status": "queued", "status_url": "/api/jobs/operation-1"}

    monkeypatch.setattr(sources.Context, "settings", settings)
    monkeypatch.setattr(sources.Context, "repo", lambda *_args, **_kwargs: Repo())
    monkeypatch.setattr(sources.task_io, "accept", accept)
    app = FastAPI()
    app.include_router(sources.create_router(SimpleNamespace()))
    with TestClient(app) as client:
        result = client.get("/api/mining/ov-source-content", params={"source_id": "source-1",
                                                                    "uri": ROOT + "/lesson.md"})
        assert result.status_code == 202
        assert accepted[0][0] == "source.content"
        assert client.get("/api/mining/input-sources").json()["sources"][0]["status"] == "unverified"
        result = client.request("DELETE", "/api/mining/ov-entry", json={"source_id": "source-1", "uri": ROOT})
        assert result.status_code == 400
        result = client.get("/api/mining/ov-tree", params={"account": "another-tenant-account"})
        assert result.status_code == 403
    assert len(accepted) == 1


@pytest.mark.postgres
def test_source_registration_runs_on_worker_and_inventory_survives_api_restart(pg_env, tmp_path, monkeypatch):
    from team_miner.skill_miner.handlers import register_handlers
    from teamEvolver.config_runtime import ConfigRepository
    from teamEvolver.openviking import OpenVikingResult
    from teamEvolver.runtime.services import build_services
    from teamEvolver.runtime.worker import process_once
    from tests.support.app import H, _login, _pod
    from tests.support.openviking import OV
    from tests.support.postgres import _run

    class SourceOV(OV):
        async def request(self, method, path, **kwargs):
            if path == "/api/v1/fs/ls":
                self.calls.append((method, path, kwargs))
                return OpenVikingResult(True, 200, [{"uri": kwargs["params"]["uri"] + "/lesson.md",
                                                     "size": 12, "isDir": False}])
            return await super().request(method, path, **kwargs)

    remote = SourceOV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="source-api-a") as client:
        _login(client, bootstrap=True)
        client.app.state.services.extras["skill_mining_client_factory"] = lambda *_args: remote
        response = client.post("/api/mining/ov-sources", headers={**H, "Idempotency-Key": "register-source"},
                               json={"account": "acct-default", "uri": ROOT, "name": "Lesson", "tags": ["skill"]})
        assert response.status_code == 202, response.text
        identifier = response.json()["job_id"]
        assert remote.calls == []

    worker = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                            extras={"config_repo": ConfigRepository(pg_env.tx),
                                    "skill_mining_client_factory": lambda *_args: remote})
    pg_env.tx.pool.run(process_once(worker, owner_id="source-worker"))
    task = _run(pg_env, "default", lambda conn: worker.tasks.get(conn, task_id=identifier))
    assert task["status"] == "succeeded", task.get("error_code")
    assert any(path == "/api/v1/fs/ls" for _, path, _ in remote.calls)
    with _pod(pg_env, tmp_path, monkeypatch, pod="source-api-b") as client:
        _login(client, bootstrap=False)
        response = client.get("/api/mining/input-sources", headers=H)
        assert response.status_code == 200, response.text
        assert [(row["name"], row["uri"]) for row in response.json()["sources"]] == [("Lesson", ROOT)]
