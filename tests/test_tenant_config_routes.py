"""Route-level tests for the tenant config editor (GET/PUT /api/tenants/{id}/config).

Uses the in-memory tenant registry fake (no PostgreSQL) and a TestClient with
an injected admin console user, mirroring the pattern of
test_openviking_workspace.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.tenant_routes import register_tenant_routes
from teamEvolver.tenants.registry import TenantRegistry

from tests.test_tenant_registry import make_pg_registry


class _TenantRoutesOwner(SimpleNamespace):
    """Minimal owner double for register_tenant_routes."""


def _client(*, role: str = "admin"):
    registry, runtime = make_pg_registry()
    owner = _TenantRoutesOwner(
        config=TeamEvolverConfig(storage_pg_enabled=False),
        _tenant_registry=registry,
        _get_engine_pool=lambda: None,
        _embedded_evolve_apps={},
    )
    app = FastAPI()

    @app.middleware("http")
    async def inject_user(request: Request, call_next):
        request.state.console_user = {"id": "admin", "role": role}
        return await call_next(request)

    register_tenant_routes(owner, app)
    return TestClient(app), owner, registry, runtime


@pytest.fixture()
def tenant_id():
    return "t_ab12cd34"


def _make_tenant(registry, runtime, tenant_id):
    ctx, _token = registry.create_tenant("acme")
    # create_tenant generates its own id; force ours for deterministic URLs.
    registry._by_id.pop(ctx.tenant_id, None)
    runtime.rows[tenant_id] = {
        **runtime.rows.pop(ctx.tenant_id),
        "tenant_id": tenant_id,
    }
    registry._invalidate()
    return tenant_id


def test_get_tenant_config_returns_overrides_and_editable_keys(tenant_id):
    client, _owner, registry, _runtime = _client()
    tid = _make_tenant(registry, _runtime, tenant_id)
    registry.update_tenant_config(tid, {"langfuse_host": "https://lf.example.com"})

    resp = client.get(f"/api/tenants/{tid}/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == tid
    assert body["config_overrides"]["langfuse_host"] == "https://lf.example.com"
    assert "langfuse_host" in body["editable_keys"]
    assert "langfuse_secret_key" in body["editable_keys"]
    assert "langfuse_tracing_enabled" not in body["editable_keys"]
    assert "langfuse_tracing_host" not in body["editable_keys"]


def test_put_tenant_config_merges_and_null_deletes(tenant_id):
    client, _owner, registry, _runtime = _client()
    tid = _make_tenant(registry, _runtime, tenant_id)

    resp = client.put(
        f"/api/tenants/{tid}/config",
        json={"overrides": {"langfuse_host": "https://lf.example.com", "langfuse_enabled": True}},
    )
    assert resp.status_code == 200
    assert resp.json()["config_overrides"]["langfuse_enabled"] is True

    # null deletes langfuse_host, langfuse_enabled survives.
    resp = client.put(
        f"/api/tenants/{tid}/config", json={"overrides": {"langfuse_host": None}}
    )
    assert resp.status_code == 200
    overrides = resp.json()["config_overrides"]
    assert "langfuse_host" not in overrides
    assert overrides["langfuse_enabled"] is True
    assert registry.get(tid).config_overrides == overrides


def test_put_tenant_config_rejects_unknown_keys(tenant_id):
    client, _owner, registry, _runtime = _client()
    tid = _make_tenant(registry, _runtime, tenant_id)

    resp = client.put(
        f"/api/tenants/{tid}/config",
        json={"overrides": {"not_a_real_field": 1}},
    )
    assert resp.status_code == 400
    assert "not_a_real_field" in resp.json()["detail"]


def test_tenant_routes_reject_service_wide_tracing_overrides(tenant_id):
    client, _owner, registry, _runtime = _client()
    tid = _make_tenant(registry, _runtime, tenant_id)

    generic = client.put(
        f"/api/tenants/{tid}/config",
        json={"overrides": {"langfuse_tracing_enabled": True}},
    )
    assert generic.status_code == 400
    assert "langfuse_tracing_enabled" in generic.json()["detail"]

    source = client.post(
        f"/api/tenants/{tid}/langfuse-config",
        json={"tracing_enabled": True},
    )
    assert source.status_code == 409
    assert "service-wide" in source.json()["detail"]


def test_put_tenant_config_rejects_default_tenant():
    client, _owner, _registry, _runtime = _client()

    resp = client.put(
        "/api/tenants/default/config",
        json={"overrides": {"langfuse_host": "https://lf.example.com"}},
    )
    assert resp.status_code == 400
    assert "config.yaml" in resp.json()["detail"]


def test_tenant_config_requires_admin(tenant_id):
    client, _owner, registry, _runtime = _client(role="user")
    tid = _make_tenant(registry, _runtime, tenant_id)

    assert client.get(f"/api/tenants/{tid}/config").status_code == 403
    resp = client.put(
        f"/api/tenants/{tid}/config",
        json={"overrides": {"langfuse_host": "https://lf.example.com"}},
    )
    assert resp.status_code == 403
