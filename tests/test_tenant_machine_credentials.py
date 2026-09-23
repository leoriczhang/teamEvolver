"""Tenant machine credential as the single Agent data-plane credential.

The tenant credential must reach the Agent Protocol V1 surface by declaring the
registered ``integration_id`` it acts as; registration only declares identity
(no token is minted) and every non-machine path stays blocked. The retired
per-Agent token is rejected with an actionable 401.
"""

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from session_ingestion.push import routes as push_routes
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations.agent_registry import register_agent
from teamEvolver.integrations.legacy_context_workspace import ContextStateStore
from teamEvolver.proxy import ProxyServer
from teamEvolver.proxy import routes as proxy_routes
from teamEvolver.proxy.tenant_routes import get_tenant_registry
from teamEvolver.tenants.registry import TenantContext, set_current_tenant, reset_current_tenant

TENANT_TOKEN = "tevt_testtenant000"
TENANT_ID = "t_acme"
AGENT_ID = "hermes:analyst"
OTHER_AGENT_ID = "hermes:other"
EXTERNAL_SUBJECT = "ext-1"
LOCAL_USER_ID = "u-1"


class _FakeTenantRegistry:
    """TenantRegistry stand-in: deterministic tevt_ -> tenant mapping, no PG."""

    def __init__(self, *, tenant_id: str = TENANT_ID) -> None:
        self._tenant = TenantContext(tenant_id=tenant_id, display_name="Acme", status="active")

    def resolve_by_agent_token(self, token: str):
        return self._tenant if str(token or "") == TENANT_TOKEN else None

    def default_context(self) -> TenantContext:
        return TenantContext(tenant_id="default", display_name="Default")


def _config(tmp_path: Path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        users_registry_path=str(tmp_path / "users.json"),
        skills_dir=str(tmp_path / "skills"),
        sharing_enabled=False,
        sharing_skill_mirror_enabled=False,
    )


def _seed_agent(config, agent_id: str, *, status: str = "active") -> None:
    register_agent(
        config,
        {
            "agent_id": agent_id,
            "runtime_type": agent_id.split(":", 1)[0],
            "capabilities": ["context.workspace.v1"],
            "display_name": agent_id,
        },
    )
    if status == "active":
        return
    path = Path(config.users_registry_path).parent / "agents.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data.get("agents") or []:
        if item.get("agent_id") == agent_id:
            item["status"] = status
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed_user(config, *, subject: str = EXTERNAL_SUBJECT) -> None:
    Path(config.users_registry_path).write_text(
        json.dumps(
            {
                "users": [
                    {
                        "id": LOCAL_USER_ID,
                        "username": LOCAL_USER_ID,
                        "role": "user",
                        "personal_space": {"viking_user": "vu-1"},
                        "agent_subjects": [
                            {
                                "integration_id": AGENT_ID,
                                "runtime_type": "hermes",
                                "external_subject": subject,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _server(
    tmp_path: Path,
    *,
    disabled: tuple[str, ...] = (),
    with_user: bool = True,
    with_other_agent: bool = True,
) -> tuple[ProxyServer, TeamEvolverConfig]:
    config = _config(tmp_path)
    _seed_agent(config, AGENT_ID)
    if with_other_agent:
        _seed_agent(config, OTHER_AGENT_ID)
    for agent_id in disabled:
        _seed_agent(config, agent_id, status="disabled")
    if with_user:
        _seed_user(config)
    server = ProxyServer(config)
    server._tenant_registry = _FakeTenantRegistry()
    return server, config


def _client(server: ProxyServer) -> TestClient:
    return TestClient(server.app)


class _DurableConfigStub:
    """Hermetic stand-in for the operator's durable config file."""

    def __init__(self, config: TeamEvolverConfig) -> None:
        self._config = config

    def load(self) -> dict:
        return {"sharing": {"viking_deployment": "cloud"}}

    def to_config(self) -> TeamEvolverConfig:
        return self._config


def _tenant_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TENANT_TOKEN}"}


def _describe(
    client: TestClient,
    *,
    integration_id: str = AGENT_ID,
    subject: str = EXTERNAL_SUBJECT,
    token: str = TENANT_TOKEN,
):
    params = {"external_subject": subject}
    if integration_id:
        params["integration_id"] = integration_id
    return client.get(
        "/internal/agents/context/describe",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_tenant_token_resolves_declared_agent(tmp_path):
    server, _config_ = _server(tmp_path)

    response = _describe(_client(server))

    assert response.status_code == 200
    payload = response.json()
    assert payload["integration_id"] == AGENT_ID
    assert payload["subject"]["user_id"] == LOCAL_USER_ID


def test_tenant_token_requires_integration_id(tmp_path):
    server, _config_ = _server(tmp_path)

    response = _describe(_client(server), integration_id="")

    assert response.status_code == 400
    assert response.json()["detail"] == "INTEGRATION_ID_REQUIRED"


def test_tenant_token_rejects_unknown_integration_id(tmp_path):
    server, _config_ = _server(tmp_path)

    response = _describe(_client(server), integration_id="hermes:ghost")

    assert response.status_code == 403
    assert response.json()["detail"] == "UNKNOWN_INTEGRATION_ID"


def test_tenant_token_rejects_disabled_agent(tmp_path):
    server, _config_ = _server(tmp_path, disabled=(AGENT_ID,))

    response = _describe(_client(server))

    assert response.status_code == 403
    assert response.json()["detail"] == "INTEGRATION_DISABLED"


def test_tenant_token_still_needs_subject_mapping(tmp_path):
    server, _config_ = _server(tmp_path, with_user=False)

    response = _describe(_client(server))

    assert response.status_code == 403
    assert response.json()["detail"] == "SUBJECT_NOT_MAPPED"


def test_declared_identity_owns_refs(tmp_path):
    server, config = _server(tmp_path)
    token = set_current_tenant(TenantContext(tenant_id=TENANT_ID))
    try:
        ref_id, _receipt = ContextStateStore(config).issue_ref(
            agent_id=AGENT_ID,
            user_id=LOCAL_USER_ID,
            session_id="",
            scope="personal_memory",
            uri="viking://user/vu-1/memories/smoke.md",
            kind="memory",
        )
    finally:
        reset_current_tenant(token)
    client = _client(server)

    read = client.post(
        "/internal/agents/context/read",
        json={"context_ref": ref_id, "integration_id": OTHER_AGENT_ID, "level": "l1"},
        headers=_tenant_headers(),
    )
    assert read.status_code == 404
    assert read.json()["detail"] == "CONTEXT_REF_INVALID"

    forget = client.post(
        "/internal/agents/context/forget",
        json={"context_ref": ref_id, "integration_id": OTHER_AGENT_ID},
        headers=_tenant_headers(),
    )
    assert forget.status_code == 403
    assert forget.json()["detail"] == "CONTEXT_SCOPE_FORBIDDEN"


def test_registration_no_longer_mints_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVE_INGEST_API_KEY", "control-plane-key")
    server, config = _server(tmp_path)
    # Registration consults the durable config store for the OpenViking
    # deployment mode; keep it hermetic instead of reading the operator's file.
    monkeypatch.setattr(
        proxy_routes,
        "ConfigStore",
        lambda *args, **kwargs: _DurableConfigStub(config),
    )

    response = _client(server).post(
        "/internal/agents/register",
        json={
            "schema_version": "teamevolver.agent-registration.v1",
            "protocol_version": "1.0",
            "agent_id": "hermes:fresh",
            "runtime_type": "hermes",
            "capabilities": ["session.ingest.v1", "context.workspace.v1"],
        },
        headers={"Authorization": "Bearer control-plane-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "credentials" not in payload
    agent = payload["agent"]
    assert agent["capability_ids"] == ["context.workspace.v1", "session.ingest.v1"]
    for retired in ("access_token_configured", "access_scopes", "access_token_rotated_at"):
        assert retired not in agent


def test_retired_agent_access_token_is_rejected(tmp_path):
    server, _config_ = _server(tmp_path)
    client = _client(server)
    headers = {"Authorization": "Bearer tev1_cachedcredential"}

    describe = client.get(
        "/internal/agents/context/describe",
        params={"external_subject": EXTERNAL_SUBJECT, "integration_id": AGENT_ID},
        headers=headers,
    )
    assert describe.status_code == 401
    assert describe.json()["detail"] == "AGENT_ACCESS_TOKEN_RETIRED"

    ingest = client.post("/ingest_session", json=_v1_envelope(), headers=headers)
    assert ingest.status_code == 401
    assert ingest.json()["detail"] == "AGENT_ACCESS_TOKEN_RETIRED"


def test_tenant_token_cannot_reach_admin_or_control_plane_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVE_INGEST_API_KEY", "control-plane-key")
    server, _config_ = _server(tmp_path)
    client = _client(server)

    guarded = client.post("/trigger-dreamcycle", headers=_tenant_headers())
    assert guarded.status_code == 403
    assert guarded.json()["detail"] == "console admin required"

    register = client.post(
        "/internal/agents/register",
        json={"agent_id": "hermes:new", "runtime_type": "hermes"},
        headers=_tenant_headers(),
    )
    assert register.status_code == 403
    assert register.json()["detail"] == "console admin required"

    tenants = client.get("/api/tenants", headers=_tenant_headers())
    assert tenants.status_code in {401, 403}


def _v1_envelope(integration_id: str = AGENT_ID) -> dict:
    return {
        "schema_version": "teamevolver.agent-session.v1",
        "protocol_version": "1.0",
        "session_id": "smoke-1",
        "runtime": {"type": "hermes", "integration_id": integration_id},
        "runtime_context": {"external_subject": EXTERNAL_SUBJECT},
        "turns": [{"role": "user", "content": "hi"}],
    }


def test_v1_ingest_accepts_tenant_token(tmp_path, monkeypatch):
    server, _config_ = _server(tmp_path)
    captured: dict = {}

    async def _fake_ingest(owner, session, *, invalidate_cache=None):
        captured["session"] = session
        return {"ok": True, "session_id": session["session_id"]}

    monkeypatch.setattr(push_routes, "ingest", _fake_ingest)

    response = _client(server).post(
        "/ingest_session",
        json=_v1_envelope(),
        headers=_tenant_headers(),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["session"]["runtime_context"]["team_evolver_user_id"] == LOCAL_USER_ID


def test_v1_ingest_rejects_unregistered_integration_id(tmp_path, monkeypatch):
    server, _config_ = _server(tmp_path)

    async def _fake_ingest(owner, session, *, invalidate_cache=None):  # pragma: no cover
        raise AssertionError("ingest must not be reached")

    monkeypatch.setattr(push_routes, "ingest", _fake_ingest)

    response = _client(server).post(
        "/ingest_session",
        json=_v1_envelope("hermes:ghost"),
        headers=_tenant_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "UNKNOWN_INTEGRATION_ID"


SINGLE_TENANT_TOKEN = "tevt_singletenant000"


def _single_tenant_server(tmp_path, *, token: str = SINGLE_TENANT_TOKEN):
    """Real TenantRegistry in single mode (no PG) with a configured credential."""
    config = _config(tmp_path)
    _seed_agent(config, AGENT_ID)
    _seed_user(config)
    server = ProxyServer(replace(config, tenant_machine_token=token))
    return server, get_tenant_registry(server)


def test_single_tenant_configured_credential_works(tmp_path):
    server, registry = _single_tenant_server(tmp_path)
    assert registry.mode == "single"

    response = _describe(
        _client(server),
        token=SINGLE_TENANT_TOKEN,
    )

    assert response.status_code == 200
    assert response.json()["integration_id"] == AGENT_ID
    assert response.json()["subject"]["user_id"] == LOCAL_USER_ID


def test_single_tenant_rejects_unconfigured_or_wrong_credential(tmp_path):
    server, _registry = _single_tenant_server(tmp_path)

    wrong = _describe(_client(server), token="tevt_wrong")
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "invalid tenant token"

    unconfigured, _registry2 = _single_tenant_server(tmp_path / "bare", token="")
    any_token = _describe(_client(unconfigured), token=SINGLE_TENANT_TOKEN)
    assert any_token.status_code == 401
    assert any_token.json()["detail"] == "invalid tenant token"
