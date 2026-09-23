"""Protocol acceptance tests for tenant/user identity and Context ownership."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from session_ingestion.push import routes as push_routes
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.integrations.agent_principal import AgentPrincipal, normalize_user_id
from teamEvolver.integrations.context_workspace import ContextStateStore, verify_context_usage
from teamEvolver.proxy import ProxyServer
from teamEvolver.tenants.registry import TenantContext, effective_config

TOKEN = "tevt_protocoltest"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/internal/agents/context"


def config_for(tmp_path, **kwargs):
    return TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        skills_dir=str(tmp_path / "skills"),
        sharing_enabled=False, sharing_skill_mirror_enabled=False,
        sharing_viking_account="account-a", tenant_machine_token=TOKEN,
        agent_protocol_identity_mode="tenant_user", **kwargs,
    )


def client_for(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    server = ProxyServer(config)
    calls = []

    async def workspace(user, scope, method, path, **kwargs):
        calls.append((user, scope, method, path, kwargs))
        if path.endswith("/search/search"):
            return [{"uri": scope.root_uri + "/note.md", "content": "Saved context"}]
        if path.endswith("/fs/tree"):
            return []
        if path.startswith("/api/v1/content/"):
            return "Saved context"
        return {}

    monkeypatch.setattr(server, "_workspace_request", workspace)
    # Server boot may initialize its console users; Agent requests must never read it.
    def forbidden(*args, **kwargs):
        raise AssertionError("Agent v2 consulted a registry")

    monkeypatch.setattr("teamEvolver.proxy.users_admin._load_registry", forbidden)
    monkeypatch.setattr("teamEvolver.integrations.agent_registry.resolve_active_agent", forbidden)
    return TestClient(server.app), config, calls


@pytest.mark.parametrize("value", ["../alice", "a/b", "a\\b", "%2e%2e", "alice\nbob", "a" * 161, {}, 123])
def test_invalid_user_ids(value):
    with pytest.raises(ValueError, match="USER_ID_INVALID"):
        normalize_user_id(value)


def test_namespace_normalization():
    assert normalize_user_id("  Ａlice  ") == "Alice"


def test_context_nine_endpoints_and_owner_checks(tmp_path, monkeypatch):
    client, config, calls = client_for(tmp_path, monkeypatch)

    def post(path, body, user_id="alice"):
        return client.post(BASE + path, json={**body, "user_id": user_id}, headers=HEADERS)

    describe = client.get(BASE + "/describe", params={"user_id": "alice"}, headers=HEADERS)
    assert describe.status_code == 200
    assert describe.json()["subject"] == {"tenant_id": "default", "user_id": "alice"}
    assert "integration_id" not in describe.json()
    session = post("/sessions/start", {"external_session_id": "same"}).json()["context_session_id"]
    other = post("/sessions/start", {"external_session_id": "same"}, "bob").json()["context_session_id"]
    assert session != other
    resolved = post("/resolve", {"query": "context", "scopes": ["personal_memory"], "context_session_id": session})
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["schema_version"] == "teamevolver.context-result.v2"
    ref = payload["items"][0]["context_ref"]
    assert post("/read", {"context_ref": ref}).status_code == 200
    assert post("/read", {"context_ref": ref}, "bob").status_code == 404
    assert post("/forget", {"context_ref": ref}, "bob").status_code == 403
    assert client.get(BASE + "/skills", params={"user_id": "alice"}, headers=HEADERS).status_code == 200
    remembered = post("/remember", {"content": "Remember this"}).json()["context_ref"]
    assert post("/forget", {"context_ref": remembered}).json()["forgotten"] is True
    event = {"context_session_id": session, "event_id": "e1", "sequence": 1, "role": "user", "content": "hi"}
    assert post("/sessions/append", event, "bob").status_code == 409
    assert post("/sessions/append", event).json()["duplicate"] is False
    assert post("/sessions/append", event).json()["duplicate"] is True
    assert post("/sessions/commit", {"context_session_id": session}, "bob").status_code == 404
    assert post("/sessions/commit", {"context_session_id": session, "used_context_refs": [ref]}).status_code == 200
    assert post("/sessions/commit", {"context_session_id": session}).json()["duplicate"] is True
    assert all(call[0]["_agent_principal"].account_id == "account-a" for call in calls)
    assert ContextStateStore(config).get_session(
        session, principal=AgentPrincipal("default", "account-a", "alice"),
    )["committed"]


@pytest.mark.parametrize("path,method", [
    ("/describe", "get"), ("/resolve", "post"), ("/read", "post"), ("/skills", "get"),
    ("/remember", "post"), ("/forget", "post"), ("/sessions/start", "post"),
    ("/sessions/append", "post"), ("/sessions/commit", "post"),
])
def test_every_endpoint_requires_user(tmp_path, monkeypatch, path, method):
    client, _, _ = client_for(tmp_path, monkeypatch)
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(BASE + path, headers=HEADERS, **kwargs)
    assert response.status_code == 400
    assert response.json()["detail"] == "USER_ID_REQUIRED"


def test_v2_ingest_no_registration_and_canonical_meta(tmp_path, monkeypatch):
    client, _, _ = client_for(tmp_path, monkeypatch)
    captured = {}

    async def ingest(owner, session, **kwargs):
        captured.update(session)
        return {"ok": True}

    monkeypatch.setattr(push_routes, "ingest", ingest)
    body = {
        "schema_version": "teamevolver.agent-session.v2", "protocol_version": "2.0",
        "session_id": "session-1", "runtime": {"type": "unregistered"},
        "runtime_context": {"user_id": "  alice  ", "team_evolver_user_id": "forged"},
        "meta": {"user_id": "forged"}, "tenant_id": "forged", "account_id": "forged",
        "turns": [{"role": "user", "content": "hi"}],
    }
    response = client.post("/ingest_session", json=body, headers=HEADERS)
    assert response.status_code == 200
    assert captured["meta"]["user_id"] == "alice"
    assert captured["runtime_context"]["team_evolver_user_id"] == "alice"
    assert captured["account_id"] == "account-a"
    assert captured["tenant_id"] == "default"
    alice_session_id = captured["session_id"]
    assert captured["meta"]["session_id"] == "session-1"
    assert captured["runtime_context"]["source_session_id"] == "session-1"
    assert client.post("/ingest_session", json=body, headers=HEADERS).status_code == 200
    assert captured["session_id"] == alice_session_id
    body["runtime_context"]["user_id"] = "bob"
    assert client.post("/ingest_session", json=body, headers=HEADERS).status_code == 200
    assert captured["session_id"] != alice_session_id
    assert client.post("/ingest_session", json=body).status_code == 401
    body["schema_version"], body["protocol_version"] = "teamevolver.agent-session.v1", "1.0"
    assert client.post("/ingest_session", json=body, headers=HEADERS).status_code == 400


def test_tenant_state_and_snapshot_usage_isolation(tmp_path):
    config = config_for(tmp_path)
    a = AgentPrincipal("tenant-a", "account-a", "alice")
    b = AgentPrincipal("tenant-b", "account-b", "alice")
    store_a = ContextStateStore(config, tenant_id=a.tenant_id)
    store_b = ContextStateStore(config, tenant_id=b.tenant_id)
    session_a, _ = store_a.start_session(principal=a, external_session_id="same")
    session_b, _ = store_b.start_session(principal=b, external_session_id="same")
    assert session_a["context_session_id"] != session_b["context_session_id"]
    ref, _ = store_a.issue_ref(principal=a, session_id="", scope="personal_memory", uri="viking://user/alice/memories/x", kind="memory")
    assert store_b.resolve_ref(ref, principal=b) is None
    with pytest.raises(ValueError, match="tenant binding"):
        store_a.resolve_ref(ref, principal=b)
    with pytest.raises(ValueError, match="invalid or expired"):
        verify_context_usage(config, principal=b, turns=[{"context_usage": {"memory_refs": [{"context_ref": ref}]}}])


def test_pg_failure_never_falls_back(tmp_path, monkeypatch):
    config = replace(config_for(tmp_path), storage_pg_enabled=True)
    store = ContextStateStore(config)
    store.path.write_text('{"refs":{"foreign":{}}}')

    def unavailable(*args, **kwargs):
        raise RuntimeError("PG unavailable")

    monkeypatch.setattr("teamEvolver.integrations.context_workspace.read_kv", unavailable)
    with pytest.raises(RuntimeError, match="PG unavailable"):
        store.resolve_ref("foreign", principal=AgentPrincipal("default", "account-a", "alice"))


def test_config_roundtrip_env_and_deployment_switches(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    store.save({
        "agent_protocol": {"identity_mode": "tenant_user"}, "skills": {"delivery_mode": "pull"},
        "replay": {"adapter": "customer.py", "adapters_dir": str(tmp_path)},
        "validation": {"runtimes": ["custom"]},
    })
    config = store.to_config()
    assert config.agent_protocol_identity_mode == "tenant_user"
    assert config.skills_delivery_mode == "pull"
    assert config.validation_runtimes == ["custom"]
    overrides = effective_config(None, TenantContext("tenant-a", config_overrides={
        "agent_protocol_identity_mode": "dual", "skills_delivery_mode": "push",
        "replay_adapters_dir": "/evil", "replay_adapter": "other.py",
    }), config)
    assert overrides.agent_protocol_identity_mode == "tenant_user"
    assert overrides.skills_delivery_mode == "pull"
    assert overrides.replay_adapters_dir == str(tmp_path)
    assert overrides.replay_adapter == "other.py"
    monkeypatch.setenv("TEAMEVOLVER_AGENT_IDENTITY_MODE", "dual")
    assert store.to_config().agent_protocol_identity_mode == "dual"
