"""Real PG acceptance fixes each connection's source, settings and credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from session_ingestion.adapters.repository import AdapterRepository
from session_ingestion.adapters.worker import AdapterSource
from session_ingestion.api import create_router
from session_ingestion.connections import ConnectionConflict, ConnectionNotFound, ConnectionRepository
from session_ingestion.handlers import SessionIngestionDeps, register_handlers
from session_ingestion.payloads import SessionPayloads
from teamEvolver.runtime import build_services, process_once
from teamEvolver.security import SecretBox, SecretRepository
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def adapter_code(references):
    metadata = {"enabled": True, "supported_filters": ["agent_id", "from_timestamp", "to_timestamp"],
                "required_filters": ["from_timestamp", "to_timestamp"], "connection_options": ["trace_names"],
                "connection_profiles": {name: {
                    "options": {"host": "https://" + name + ".example", "expected_hash": hashlib.sha256(
                        ("credential-" + name).encode()).hexdigest()}, "secret_refs": {"secret_key": reference},
                } for name, reference in references.items()}}
    return f'''import hashlib
SOURCE = {metadata!r}
class Adapter:
    def __init__(self, config): self.config=config
    def health(self):
        digest=hashlib.sha256(self.config.get("secret_key","").encode()).hexdigest()
        return {{"ok":digest==self.config.get("expected_hash"),"host":self.config.get("host")}}
    def list_session_ids(self, filters, *, max_sessions): return []
    def iter_session_id_batches(self, filters, *, batch_size=100): return iter(())
    def fetch_session(self, identity): return {{}},[]
    def convert_session(self, raw, traces): return {{"turns":[]}}
    def close(self): pass
def build_adapter(config=None): return Adapter(config or {{}})
'''


def prepare(env, tenant="default"):
    secrets = SecretRepository(env.tx, SecretBox("isolated-connection-test-master"))

    async def seed(conn):
        references = {}
        for name in ("alpha", "beta"):
            saved = await secrets.store(conn, tenant_id=tenant, plaintext="credential-" + name)
            references[name] = saved.ref
        version = await AdapterRepository(env.tx).save_source(
            conn, tenant_id=tenant, adapter_id="shared.py", source=adapter_code(references))
        return version

    return _run(env, tenant, seed), secrets


def document(profile="alpha", *, revision=1):
    return {"adapter_id": "shared.py", "adapter_revision": revision, "label": profile,
            "settings": {"profile": profile, "options": {"trace_names": [profile + "-turn"]}},
            "default_filters": {"agent_id": profile}}


def api(env, tmp_path, monkeypatch, secrets):
    client = _pod(env, tmp_path, monkeypatch, pod="connection")
    services = client.app.state.services
    services.extras.update(session_payloads=SessionPayloads(env.tx), secret_repo=secrets)
    client.app.include_router(create_router(services))
    return client, services


def accepted_control(env, job_id):
    async def read(conn):
        row = await conn.fetchrow(
            f"SELECT o.content FROM {env.schema}.worker_tasks t JOIN {env.schema}.storage_objects o "
            "ON o.key=t.payload_ref WHERE t.task_id=$1", job_id,
        )
        return json.loads(json.loads(bytes(row["content"]))["payload"])
    return _run(env, "default", read)


def test_connection_revisions_and_bindings_are_atomic_and_tenant_scoped(pg_env):
    version, _ = prepare(pg_env)
    prepare(pg_env, "alpha")
    repo = ConnectionRepository(pg_env.tx)

    async def save(conn):
        first = await repo.save(conn, tenant_id="default", identity="first", expected_revision=0,
                                document=document(revision=version))
        before = await repo.freeze(conn, "first")
        await repo.save(conn, tenant_id="default", identity="first", expected_revision=1,
                        document=document("beta", revision=version))
        assert (await repo.freeze(conn, "first", revision=1)) == before
        assert (await repo.get(conn, "first"))["revision"] == 2
        binding = await AdapterRepository(pg_env.tx).binding(conn, binding_key="datasource:first")
        assert binding["adapter_id"] == "shared.py" and binding["revision"] == version
        return first

    _run(pg_env, "default", save)
    with pytest.raises(ConnectionConflict):
        _run(pg_env, "default", lambda conn: repo.save(conn, tenant_id="default", identity="first",
                                                      expected_revision=1, document=document()))
    with pytest.raises(ConnectionNotFound):
        _run(pg_env, "alpha", lambda conn: repo.get(conn, "first"))
    assert _run(pg_env, "alpha", lambda conn: repo.list(conn)) == []


def test_api_freezes_each_connection_and_scopes_idempotency_without_plaintext(pg_env, tmp_path, monkeypatch):
    _, secrets = prepare(pg_env)
    client, _ = api(pg_env, tmp_path, monkeypatch, secrets)
    with client:
        assert client.get("/api/datasource/connections", headers=H).status_code in (401, 403)
        _login(client, bootstrap=True)
        for name in ("alpha", "beta"):
            response = client.put("/api/datasource/connections/" + name,
                                  json={"expected_revision": 0, **document(name)}, headers=H)
            assert response.status_code == 200, response.text
        body = {"from_timestamp": "2026-11-19T00:00:00Z", "to_timestamp": "2026-11-20T00:00:00Z"}
        headers = {**H, "Idempotency-Key": "same-caller-key"}
        a = client.post("/api/datasource/connections/alpha/pull", json=body, headers=headers)
        b = client.post("/api/datasource/connections/beta/pull", json=body, headers=headers)
        assert a.status_code == b.status_code == 202
        assert a.json()["job_id"] != b.json()["job_id"]
        repeated = client.post("/api/datasource/connections/alpha/pull", json=body, headers=headers)
        assert repeated.json()["dedupe"] and repeated.json()["job_id"] == a.json()["job_id"]
        original = accepted_control(pg_env, a.json()["job_id"])
        assert original["pull"]["connection"]["settings"] == document()["settings"]
        assert "credential-alpha" not in json.dumps(original)
        assert original["pull"]["filters"]["agent_id"] == "alpha"
        changed = client.put("/api/datasource/connections/alpha", headers=H,
                             json={"expected_revision": 1, **document("beta")})
        assert changed.status_code == 200, changed.text
        assert accepted_control(pg_env, a.json()["job_id"]) == original
        assert client.post("/api/datasource/connections/alpha/pull", json=body, headers=headers).status_code == 409
        assert client.get("/api/datasource/connections/alpha", headers={"X-Tenant-Id": "alpha"}).status_code == 404


@pytest.mark.parametrize("settings", [
    {"profile": "alpha", "options": {"host": "https://attacker.example"}},
    {"profile": "alpha", "options": {"base_url": "https://attacker.example"}},
    {"profile": "alpha", "options": {"trace_names": [{"url": "https://attacker.example"}]}},
    {"profile": "unknown", "options": {}},
    {"profile": "alpha", "secret_refs": {"secret_key": "sref_" + "f" * 32}},
    {"profile": "alpha", "env_refs": {"secret_key": "TEAMEVOLVER_ROOT_API_KEY"}},
])
def test_operator_settings_cannot_redirect_declared_credentials(pg_env, tmp_path, monkeypatch, settings):
    _, secrets = prepare(pg_env)
    client, _ = api(pg_env, tmp_path, monkeypatch, secrets)
    with client:
        _login(client, bootstrap=True)
        response = client.put("/api/datasource/connections/test", headers=H,
                              json={"expected_revision": 0, **document(), "settings": settings})
        assert response.status_code == 400, response.text
        assert _run(pg_env, "default", lambda conn: ConnectionRepository(pg_env.tx).list(conn)) == []


def test_temporary_agent_filter_cannot_expand_named_connection_scope(pg_env, tmp_path, monkeypatch):
    _, secrets = prepare(pg_env)
    client, _ = api(pg_env, tmp_path, monkeypatch, secrets)
    with client:
        _login(client, bootstrap=True)
        assert client.put("/api/datasource/connections/sales", headers=H,
                          json={"expected_revision": 0, **document()}).status_code == 200
        for value in ("beta", "", None):
            response = client.post("/api/datasource/connections/sales/pull", headers=H, json={
                "from_timestamp": "2026-11-19T00:00:00Z", "to_timestamp": "2026-11-20T00:00:00Z",
                "filters": {"agent_id": value}})
            assert response.status_code == 400, response.text


def test_native_worker_uses_frozen_destination_and_reference_after_connection_edit(pg_env, tmp_path, monkeypatch):
    _, secrets = prepare(pg_env)
    client, services = api(pg_env, tmp_path, monkeypatch, secrets)
    with client:
        _login(client, bootstrap=True)
        for name in ("alpha", "beta"):
            assert client.put("/api/datasource/connections/" + name, headers=H,
                              json={"expected_revision": 0, **document(name)}).status_code == 200
        jobs = [client.post(f"/api/datasource/connections/{name}/test", json={}, headers=H).json()["job_id"]
                for name in ("alpha", "beta")]
        assert client.put("/api/datasource/connections/alpha", headers=H,
                          json={"expected_revision": 1, **document("beta")}).status_code == 200
        deps = SessionIngestionDeps(payloads=services.extras["session_payloads"], stage_runner=None,
                                    adapter_source=AdapterSource())
        worker = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
            **services.extras, "session_ingestion": deps})
        async def execute():
            await asyncio.gather(*(process_once(worker, owner_id="connection-" + str(index), tenants=("default",),
                                                 kinds=["session.pull"], max_attempts=1)
                                   for index in range(len(jobs))))
        pg_env.tx.pool.run(execute())
        results = [client.get(f"/api/datasource/jobs/{job}/result", headers=H).json() for job in jobs]
        states = _run(pg_env, "default", lambda conn: conn.fetch(
            f"SELECT status FROM {pg_env.schema}.worker_tasks WHERE task_id=ANY($1::text[])", jobs))
        assert len(states) == 2 and all(row["status"] == "succeeded" for row in states), results
        health = [result["result"]["health"] for result in results]
        assert health == [{"ok": True, "host": "https://alpha.example"},
                          {"ok": True, "host": "https://beta.example"}]
        assert "credential-alpha" not in json.dumps(results)


def test_default_schedule_status_ignores_manual_and_named_jobs(pg_env, tmp_path, monkeypatch):
    revision, secrets = prepare(pg_env)
    client, _ = api(pg_env, tmp_path, monkeypatch, secrets)
    with client:
        _login(client, bootstrap=True)
        bound = client.put("/api/datasource", headers=H, json={"file": "shared.py", "revision": revision})
        assert bound.status_code == 200, bound.text
        original = client.post("/api/datasource/schedule/run", json={}, headers=H)
        assert original.status_code == 202, original.text
        assert client.post("/api/datasource/pull", json={}, headers=H).status_code == 202
        path = "/api/datasource/connections/other"
        assert client.put(path, headers=H, json={**document(), "expected_revision": 0}).status_code == 200
        assert client.post(path + "/test", headers=H, json={}).status_code == 202
        schedule = client.get("/api/datasource/schedule", headers=H).json()["schedule_status"]
        assert schedule["job_type"] == "scheduled" and schedule["job_id"] == original.json()["job_id"]
