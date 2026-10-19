"""Agent Context HTTP acceptance using PG and a fake upstream boundary."""

import os

import pytest

from teamEvolver.tenants.registry import TenantContext
from tests.support.app import _pod
from tests.support.openviking import OV

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="requires PostgreSQL")


@pytest.mark.postgres
def test_context_endpoints_survive_api_restart_and_bind_user(pg_env, tmp_path, monkeypatch):
    ov = OV()

    async def authorize(request, services):
        request.state.tenant = TenantContext(
            "default", "Default", config_overrides={"sharing_viking_account": "account-a"}
        )
        request.state.tenant_source = "token"

    # Use the normal composition middleware while avoiding a separate identity
    # fixture: the resolver still requires the tenant token and user_id.
    from fastapi.testclient import TestClient

    from teamEvolver.app import create_app

    a = _pod(pg_env, tmp_path, monkeypatch, pod="a").app.state.services
    a.extras["context_client_factory"] = lambda *_: ov
    headers = {"Authorization": "Bearer tevt_test"}
    with TestClient(create_app(services=a, authorizer=authorize)) as client:
        base = "/internal/agents/context"
        response = client.post(
            base + "/sessions/start", headers=headers, json={"user_id": "alice", "external_session_id": "one"}
        )
        assert response.status_code == 200, response.text
        sid = response.json()["context_session_id"]
        event = {
            "user_id": "alice",
            "context_session_id": sid,
            "event_id": "e1",
            "sequence": 1,
            "role": "user",
            "content": "hello",
        }
        assert client.post(base + "/sessions/append", headers=headers, json=event).status_code == 200
        assert client.post(base + "/sessions/append", headers=headers, json=event).json()["duplicate"]
        assert (
            client.post(base + "/sessions/append", headers=headers, json={**event, "user_id": "bob"}).status_code == 409
        )
    b = _pod(pg_env, tmp_path, monkeypatch, pod="b").app.state.services
    b.extras["context_client_factory"] = lambda *_: ov
    with TestClient(create_app(services=b, authorizer=authorize)) as client:
        response = client.post(
            base + "/sessions/commit", headers=headers, json={"user_id": "alice", "context_session_id": sid}
        )
        assert response.status_code == 200 and response.json()["committed"]
        assert client.get(base + "/describe", headers=headers).status_code == 400
        assert client.get(base + "/describe", params={"user_id": "alice"}).status_code == 401
    messages = [call for call in ov.calls if call[1].endswith("/messages")]
    assert len(messages) == 1 and messages[0][2]["idempotency_key"]


@pytest.mark.postgres
def test_context_commit_uses_upstream_context_parts(pg_env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from teamEvolver.app import create_app
    from teamEvolver.context.repository import ContextRepository
    from teamEvolver.security.agent_principal import AgentPrincipal

    ov = OV()
    services = _pod(pg_env, tmp_path, monkeypatch, pod="a").app.state.services
    services.extras["context_client_factory"] = lambda *_: ov
    repo = ContextRepository(pg_env.tx)
    p = AgentPrincipal("default", "acct-default", "alice")

    async def seed(conn):
        session, _ = await repo.start_session(conn, p, "external")
        rid, _ = await repo.issue_ref(
            conn,
            p,
            session_id=session["session_id"],
            scope="personal_memory",
            uri="viking://user/alice/memories/a.md",
            kind="memory",
        )
        await repo.save_snapshot(conn, p, session["session_id"], "snap", [{"context_ref": rid, "l0": "retrieved fact"}])
        return session["session_id"], rid

    sid, rid = services.tx.run_tenant_tx("default", seed)

    async def authorize(request, services):
        request.state.tenant = TenantContext("default", "Default")
        request.state.tenant_source = "token"

    with TestClient(create_app(services=services, authorizer=authorize)) as client:
        response = client.post(
            "/internal/agents/context/sessions/commit",
            headers={"Authorization": "Bearer tevt_test"},
            json={"user_id": "alice", "context_session_id": sid, "used_context_refs": [rid]},
        )
        assert response.status_code == 200, response.text
    usage = [c for c in ov.calls if c[1].endswith("/messages")]
    assert usage[0][2]["json_body"]["parts"] == [
        {"type": "context", "context_type": "memory", "uri": "viking://user/alice/memories/a.md"}
    ]
    assert not any(c[1].endswith("/used") for c in ov.calls)


@pytest.mark.postgres
def test_context_resolve_budget_and_skill_identity_dedup(pg_env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from teamEvolver.app import create_app
    from teamEvolver.openviking import OpenVikingResult

    class SearchOV(OV):
        async def request(self, method, path, **kwargs):
            if path == "/api/v1/search/search":
                root = kwargs["json_body"]["target_uri"]
                return OpenVikingResult(
                    True, 200, [{"uri": root + "/slug/reference.md", "abstract": "a" * 300, "overview": "b" * 800}]
                )
            if path == "/api/v1/content/read":
                return OpenVikingResult(True, 200, "---\nname: actual-name\ndescription: same\n---\nbody")
            if path == "/api/v1/fs/stat":
                return OpenVikingResult(True, 200, {"modTime": "2026-10-13T00:00:00Z"})
            return await super().request(method, path, **kwargs)

    async def authorize(request, services):
        request.state.tenant = TenantContext("default", "Default")
        request.state.tenant_source = "token"

    services = _pod(pg_env, tmp_path, monkeypatch, pod="a").app.state.services
    ov = SearchOV()
    services.extras["context_client_factory"] = lambda *_: ov
    with TestClient(create_app(services=services, authorizer=authorize)) as client:
        response = client.post(
            "/internal/agents/context/resolve",
            headers={"Authorization": "Bearer tevt_test"},
            json={"user_id": "alice", "query": "q", "max_chars": 500, "scopes": ["team_skills", "personal_skills"]},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert len(result["items"]) == 1
        assert result["items"][0]["scope"] == "personal_skills"
        assert result["items"][0]["skill_metadata"]["name"] == "actual-name"
        assert result["budget"]["used_chars"] == 500
        before = len(ov.calls)
        bad = client.post(
            "/internal/agents/context/remember",
            headers={"Authorization": "Bearer tevt_test"},
            json={"user_id": "alice", "content": "fact", "context_session_id": "other-user"},
        )
        assert bad.status_code == 409 and len(ov.calls) == before


@pytest.mark.postgres
def test_context_remember_bootstraps_missing_personal_memory_directories(pg_env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from teamEvolver.app import create_app
    from teamEvolver.openviking import OpenVikingResult

    class MissingDirectoriesOV(OV):
        def __init__(self):
            super().__init__()
            self.directories = {"viking://user"}

        async def request(self, method, path, **kwargs):
            if path == "/api/v1/fs/mkdir":
                self.calls.append((method, path, kwargs))
                uri = kwargs["json_body"]["uri"]
                parent = uri.rsplit("/", 1)[0]
                if parent not in self.directories:
                    return OpenVikingResult(False, 404, error=f"missing parent: {parent}")
                self.directories.add(uri)
                return OpenVikingResult(True, 200, {})
            if path == "/api/v1/content/write":
                self.calls.append((method, path, kwargs))
                parent = kwargs["json_body"]["uri"].rsplit("/", 1)[0]
                if parent not in self.directories:
                    return OpenVikingResult(False, 404, error=f"missing parent: {parent}")
                return OpenVikingResult(True, 200, {"uri": kwargs["json_body"]["uri"]})
            return await super().request(method, path, **kwargs)

    async def authorize(request, services):
        request.state.tenant = TenantContext("default", "Default")
        request.state.tenant_source = "token"

    services = _pod(pg_env, tmp_path, monkeypatch, pod="a").app.state.services
    ov = MissingDirectoriesOV()
    services.extras["context_client_factory"] = lambda *_: ov

    with TestClient(create_app(services=services, authorizer=authorize)) as client:
        response = client.post(
            "/internal/agents/context/remember",
            headers={"Authorization": "Bearer tevt_test"},
            json={"user_id": "alice", "content": "fact"},
        )

    assert response.status_code == 200, response.text
    assert "viking://user/alice/memories/agent" in ov.directories
