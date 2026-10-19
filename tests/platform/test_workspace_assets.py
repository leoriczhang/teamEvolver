"""Platform assets and OpenViking Workspace APIs through the composed application."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from teamEvolver.admin.console_session import ConsoleSessionManager
from teamEvolver.app import create_app
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.runtime.services import RuntimeServices
from teamEvolver.storage import LocalObjectStore
from teamEvolver.tenants.registry import TenantContext

# --------------------------------------------------------------------------- #
# Faithful in-process tenant registry (postgres mode, no live PG)
# --------------------------------------------------------------------------- #


class FakeRegistry:
    """A ``postgres``-mode TenantRegistry stand-in (subset used by )."""

    mode = "postgres"
    default_machine_credential_configured = False

    def __init__(self) -> None:
        self.tenants: dict[str, TenantContext] = {tid: TenantContext(tid, tid) for tid in ("default", "alpha")}
        self.tokens: dict[str, str] = {}

    def get(self, tid):
        return self.tenants.get(tid)

    def list_tenants(self):
        return list(self.tenants.values())

    def default_context(self):
        return self.tenants["default"]

    def resolve_by_agent_token(self, token):
        tid = self.tokens.get(str(token or ""))
        return self.tenants.get(tid) if tid else None


# --------------------------------------------------------------------------- #
# api-role services container with the injected registry + console identity
# --------------------------------------------------------------------------- #


@dataclass
class _FakeTx:
    pool: object = None


def _build_services(config, registry) -> RuntimeServices:
    from teamEvolver.runtime.repository import ResourceLeaseRepository, WorkerTaskRepository
    from teamEvolver.runtime.tasks import build_task_registry

    tx = _FakeTx()
    return RuntimeServices(
        role="api",
        tx=tx,
        tasks=WorkerTaskRepository(tx),
        leases=ResourceLeaseRepository(tx),
        registry=build_task_registry({}, require_complete=False),
        extras={
            "config": config,
            "console_sessions": ConsoleSessionManager(config),
            "tenant_registry": registry,
        },
    )


@pytest.fixture
def config(tmp_path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "skills"),
        sharing_session_backend="local",
        sharing_skill_backend="local",
        sharing_local_root=str(tmp_path / "session"),
        sharing_skill_local_root=str(tmp_path / "skill"),
        sharing_viking_api_key="",
    )


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def app(config, registry, monkeypatch):
    monkeypatch.delenv("TEAMEVOLVER_ROOT_API_KEY", raising=False)
    monkeypatch.delenv("TEAMEVOLVER_PLATFORM_ADMINS", raising=False)
    return create_app(services=_build_services(config, registry))


@pytest.fixture
def root(app) -> TestClient:
    client = TestClient(app, base_url="http://192.0.2.10:1080")
    resp = client.post("/api/auth/bootstrap", json={"username": "root", "password": "test-password"})
    assert resp.status_code == 200, resp.text
    return client


def _register(app, username: str) -> TestClient:
    client = TestClient(app, base_url="http://192.0.2.10:1080")
    resp = client.post("/api/auth/register", json={"username": username, "password": "test-password"})
    assert resp.status_code == 200, resp.text
    return client


# --------------------------------------------------------------------------- #
# Composition / app.py invariants
# --------------------------------------------------------------------------- #


def test_platform_docs_routes_are_served_by_the_single_app(root: TestClient):
    # Both migrated surfaces answer on the one composed FastAPI app rather than
    # 404 (FastAPI 0.143 keeps included routers lazy, so probe by request, not by
    # walking app.routes). Auth is satisfied; the handlers run.
    assert root.get("/api/openviking/workspace/config", headers={"X-Tenant-Id": "default"}).status_code == 200
    # platform-assets requires a seeded store to return 200, but a missing route
    # would 404; here the handler runs and reports an empty (but valid) tree.
    tree = root.get("/api/platform-assets/tree", headers={"X-Tenant-Id": "default"})
    assert tree.status_code == 200
    assert tree.json()["read_only"] is True


def test_app_module_declares_no_business_endpoint():
    """``app.py`` is the composition root only — no inline route decorators."""
    import inspect

    import teamEvolver.app as app_module

    source = inspect.getsource(app_module)
    assert "@app.get" not in source
    assert "@app.post" not in source
    assert "@app.delete" not in source


# --------------------------------------------------------------------------- #
# Platform assets (read-only PG/NAS browser)
# --------------------------------------------------------------------------- #


def _seed_assets(config) -> None:
    session_store = LocalObjectStore(config.sharing_local_root)
    # A ledger row carries BOTH the business lifecycle time and the system
    # ingest/sync time — the dual-time display the migration must preserve.
    session_store.put_object(
        "session_ledger/session-1.json",
        '{"session_id":"session-1","business_time":"2026-10-01T00:00:00Z",'
        '"synced_at":"2026-10-02T12:00:00Z","status":"archived"}',
    )
    session_store.put_object("unrelated.json", '{"hidden":true}')
    skill_store = LocalObjectStore(config.sharing_skill_local_root)
    skill_store.put_object("manifest.json", '{"demo":{"version":2}}')


def test_platform_assets_tree_merges_sources_and_hides_unexposed(root: TestClient, config):
    _seed_assets(config)
    resp = root.get("/api/platform-assets/tree", headers={"X-Tenant-Id": "default"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["read_only"] is True
    uris = {entry["uri"] for entry in payload["entries"]}
    assert "platform://session/session_ledger/session-1.json" in uris
    assert "platform://skill/manifest.json" in uris
    assert all("unrelated.json" not in uri for uri in uris)


def test_platform_asset_content_preserves_business_and_sync_times(root: TestClient, config):
    _seed_assets(config)
    resp = root.get(
        "/api/platform-assets/content",
        params={"uri": "platform://session/session_ledger/session-1.json"},
        headers={"X-Tenant-Id": "default"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend_label"] == "NAS / 本地存储"
    # The stored bytes are returned verbatim, so BOTH timestamps survive.
    assert '"business_time":"2026-10-01T00:00:00Z"' in body["content"]
    assert '"synced_at":"2026-10-02T12:00:00Z"' in body["content"]


def test_platform_assets_require_tenant_admin(app, root, config):
    _seed_assets(config)
    alice = _register(app, "alice")
    resp = alice.get("/api/platform-assets/tree", headers={"X-Tenant-Id": "alpha"})
    assert resp.status_code == 403


def test_platform_state_survives_a_fresh_app(config, registry, monkeypatch):
    """State lives in NAS/OV, not the process: a brand-new app reads it back."""
    monkeypatch.delenv("TEAMEVOLVER_ROOT_API_KEY", raising=False)
    monkeypatch.delenv("TEAMEVOLVER_PLATFORM_ADMINS", raising=False)
    _seed_assets(config)

    first = create_app(services=_build_services(config, registry))
    client1 = TestClient(first, base_url="http://192.0.2.10:1080")
    assert (
        client1.post("/api/auth/bootstrap", json={"username": "root", "password": "test-password"}).status_code == 200
    )

    # A second, independently constructed app shares the same persisted identity
    # store, so it logs the admin in (bootstrap already ran) and sees the same
    # NAS-backed assets — nothing lives in the first process.
    second = create_app(services=_build_services(config, registry))
    client2 = TestClient(second, base_url="http://192.0.2.10:1080")
    assert client2.post("/api/auth/login", json={"username": "root", "password": "test-password"}).status_code == 200
    resp = client2.get("/api/platform-assets/tree", headers={"X-Tenant-Id": "default"})
    assert resp.status_code == 200, resp.text
    uris = {entry["uri"] for entry in resp.json()["entries"]}
    assert "platform://session/session_ledger/session-1.json" in uris


# --------------------------------------------------------------------------- #
# OpenViking workspace surface
# --------------------------------------------------------------------------- #


def test_workspace_config_reports_scopes_without_openviking(root: TestClient):
    resp = root.get("/api/openviking/workspace/config", headers={"X-Tenant-Id": "default"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No endpoint/root key configured in tests → disabled, but the full scope map
    # is still reported from config (migrated handler, unchanged contract).
    assert body["enabled"] is False
    assert set(body["scopes"]) == {
        "personal_memory",
        "team_memory",
        "personal_skills",
        "team_skills",
        "personal_resources",
        "team_resources",
        "personal_workspace",
        "team_workspace",
    }


def test_workspace_list_unknown_scope_is_rejected(root: TestClient):
    resp = root.get(
        "/api/openviking/workspace/list",
        params={"scope": "bogus"},
        headers={"X-Tenant-Id": "default"},
    )
    assert resp.status_code == 400
