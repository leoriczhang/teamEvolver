"""Source recovery and retries preserve tenant isolation and pinned task bytes."""

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from session_ingestion.repository import SessionIngestionRepository
from team_ma.replay_preparation import prepare
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from team_replay.preparation import prepare_cases
from team_replay.repository import ReplayRepository
from teamEvolver.managed_agents import MAClient
from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.storage.postgres import PostgresObjectStore

pytestmark = pytest.mark.postgres


def test_recovery_uses_archived_boundary_and_records_verified_bytes(pg_env):
    raw = b"pinned video"
    native_calls = []

    def respond(request):
        native_calls.append(str(request.url.path))
        if request.url.host == "media.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=raw)
        assert request.url.path.endswith("/sessions/native")
        return httpx.Response(200, json={"id": "native", "resources": []})

    async def run():
        secrets = SecretRepository(pg_env.tx, SecretBox("test"))
        runtime = {"type": "managed-agents", "connection_id": "one", "connection_revision": 1,
                   "agent_graph": {"entry_agent_id": "root", "agents": {"root": {"id": "root", "version": 1}}},
                   "environment": {"id": "env", "config": {"type": "cloud"}}, "vault_ids": [],
                   "materials_missing": [{"reason": "URL unsupported"}]}
        events = [{"type": "user.message", "content": [
            {"type": "video", "source": {"type": "url", "url": "https://media.example/a.mp4", "fps": 1}},
        ]}]
        async with pg_env.tx.tenant_tx("alpha") as conn:
            secret = await secrets.store(conn, tenant_id="alpha", plaintext="token")
            await MARepository(pg_env.tx).save(conn, tenant_id="alpha", connection_id="one",
                config=validate_config({"display_name": "Replay", "entry_agent_id": "root", "environment_id": "env",
                                        "ma_secret_ref": secret.ref, "tenant_token_ref": "unused"}))
            source = {"runtime": runtime, "runtime_context": {"source_session_id": "native"},
                      "meta": {"raw_events": {"object_ref": "events"}}}
            store = PostgresObjectStore(pg_env.tx, "alpha")
            await store.put(conn, "source", json.dumps({"session": source}).encode())
            await store.put(conn, "events", json.dumps(events).encode())
            await SessionIngestionRepository(pg_env.tx).write_session(
                conn, tenant_id="alpha", session_id="same-id", content_ref="source")
        services = SimpleNamespace(tx=pg_env.tx, extras={
            "secret_repo": secrets,
            "ma_client_factory": lambda url, key: MAClient(url, key, transport=httpx.MockTransport(respond)),
        })
        case = {"case_id": "case", "query": "Read this video", "materials": [],
                "provenance": {"session_id": "same-id", "source_runtime": runtime}}
        before = copy.deepcopy(case)
        value = await prepare(services, SimpleNamespace(tenant_id="alpha"), [case], "new-skill")
        assert not value["ma_errors"]
        assert value["cases"][0]["materials"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert value["cases"][0]["materials"][0]["input_type"] == "video"
        assert value["ma_records"]["case"]["skill_bindings"]["new-skill"]["agents"] == ["root"]
        assert case == before
        assert not any("events" in path for path in native_calls), "Historical inputs must not include later messages"
        cross = await prepare(services, SimpleNamespace(tenant_id="beta"), [case], "new-skill")
        assert cross["ma_errors"] and not cross["ma_records"]

    asyncio.run(run())


def test_preparation_checkpoint_is_immutable_and_tenant_scoped(pg_env):
    calls = []

    async def require_current(*args):
        return None

    async def hook(task, cases, name, **kwargs):
        calls.append(task.tenant_id)
        return {"cases": cases, "ma_records": {}, "ma_errors": {}, "recoveries": [{"owner": task.tenant_id}]}

    async def run():
        services = SimpleNamespace(tx=pg_env.tx, tasks=SimpleNamespace(require_current=require_current),
                                   extras={"ma_prepare_replay": hook})
        for tenant in ("alpha", "beta"):
            task = SimpleNamespace(task_id="same-id", tenant_id=tenant, payload_hash="fixed")
            first = await prepare_cases(task, services, [{"case_id": tenant}])
            second = await prepare_cases(task, services, [{"case_id": "changed"}])
            assert second == first and second["cases"] == [{"case_id": tenant}]
            async with pg_env.tx.tenant_tx(tenant) as conn:
                with pytest.raises(ValueError, match="input hash"):
                    await ReplayRepository(pg_env.tx).preparation(conn, tenant, task.task_id, "different")
        assert calls == ["alpha", "beta"]

    asyncio.run(run())


def test_self_contained_case_recovers_snapshot_without_attaching_source_materials(monkeypatch):
    from team_ma import replay_preparation
    from team_replay import input_contract

    async def resolve(*args):
        return {"mode": "self_contained", "evidence_quote": "视频字节不可用", "source": "model"}

    async def model(*args):
        return object()

    async def recover(services, task, case, runtime):
        assert runtime["materials_missing"] == []
        # This path must restore only the configuration, not excluded attachments.
        return {"runtime": {**runtime, "snapshot_missing": False}, "materials": [],
                "evidence": {"kind": "source_snapshot_recovery"}}

    async def freeze(*args):
        return {"case": {"frozen": True}}

    monkeypatch.setattr(input_contract, "resolve", resolve)
    monkeypatch.setattr(replay_preparation, "recover_source", recover)
    monkeypatch.setattr(replay_preparation, "freeze", freeze)
    case = {"case_id": "case", "query": "视频字节不可用", "checks": [], "materials": [],
            "provenance": {"synthesis_mode": "cross_session", "source_runtime": {
                "type": "managed-agents", "snapshot_missing": True, "materials_missing": ["missing"],
            }}}
    services = SimpleNamespace(extras={"replay_input_model": model})
    result = asyncio.run(prepare(services, SimpleNamespace(tenant_id="alpha"), [case]))
    assert not result["cases"][0]["materials"]


def test_snapshot_only_recovery_never_downloads_later_native_resources(pg_env, monkeypatch):
    import team_ma.replay_preparation as module

    def respond(request):
        assert request.url.path.endswith("/sessions/native")
        return httpx.Response(200, json={"id": "native", "resources": [
            {"type": "file", "file_id": "later-unrelated-file"},
        ]})

    async def snapshot(*args):
        return {"snapshot_missing": False}

    async def unexpected(*args):
        raise AssertionError("Snapshot repair must not collect materials")

    monkeypatch.setattr(module, "runtime_snapshot", snapshot)
    monkeypatch.setattr(module, "materials", unexpected)

    async def run():
        secrets = SecretRepository(pg_env.tx, SecretBox("test"))
        async with pg_env.tx.tenant_tx("alpha") as conn:
            secret = await secrets.store(conn, tenant_id="alpha", plaintext="token")
            await MARepository(pg_env.tx).save(conn, tenant_id="alpha", connection_id="one",
                config=validate_config({"display_name": "Replay", "entry_agent_id": "root", "environment_id": "env",
                                        "ma_secret_ref": secret.ref, "tenant_token_ref": "unused"}))
            source = {"runtime": {"connection_id": "one"}, "runtime_context": {"source_session_id": "native"}}
            await PostgresObjectStore(pg_env.tx, "alpha").put(
                conn, "source", json.dumps({"session": source}).encode())
            await SessionIngestionRepository(pg_env.tx).write_session(
                conn, tenant_id="alpha", session_id="source", content_ref="source")
        services = SimpleNamespace(tx=pg_env.tx, extras={
            "secret_repo": secrets,
            "ma_client_factory": lambda url, key: MAClient(url, key, transport=httpx.MockTransport(respond)),
        })
        result = await module.recover_source(
            services, SimpleNamespace(tenant_id="alpha"),
            {"provenance": {"session_id": "source"}, "materials": []},
            {"connection_id": "one", "snapshot_missing": True, "materials_missing": []})
        assert result["materials"] == [] and not result["runtime"]["snapshot_missing"]
    asyncio.run(run())
