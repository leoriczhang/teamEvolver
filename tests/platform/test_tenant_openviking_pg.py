"""Tenant provisioning freezes PG OpenViking settings and resolves its encrypted Root Key."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.tenants.api import create_router
from teamEvolver.tenants.registry import TenantRegistry
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("account_failed", [False, True])
def test_tenant_account_and_directories_share_one_effective_pg_secret_ref_snapshot(
    pg_env, monkeypatch, account_failed,
):
    master, root_key = "tenant-ov-test-master", "fixture-openviking-root-key"
    monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", master)
    configs = ConfigRepository(pg_env.tx)

    async def persist(conn):
        saved = await SecretRepository(pg_env.tx, SecretBox(master)).store(
            conn, tenant_id="default", plaintext=root_key, purpose="openviking",
        )
        current = await configs.read_global(conn)
        await configs.write_global(conn, expected_revision=current.revision, config={"sharing": {
            "endpoint": "https://configured-openviking.test", "deployment": "cloud",
            "account": "control-account", "team_user": "configured-team", "secret_ref": saved.ref,
        }})
        return saved.ref

    reference = _run(pg_env, "default", persist)
    startup = TeamEvolverConfig(sharing_viking_endpoint="https://stale-startup.test", sharing_viking_api_key="")
    registry = TenantRegistry(runtime=pg_env.tx.pool)
    services = SimpleNamespace(tx=pg_env.tx, extras={
        "config": startup, "config_repo": configs, "tenant_registry": registry,
    })
    captured, reads = [], []
    original_read = configs.read_global

    async def read_global(conn):
        reads.append(True)
        return await original_read(conn)

    monkeypatch.setattr(configs, "read_global", read_global)
    request_thread = []

    def account(config, account_id, admin_user):
        captured.append(config)
        assert threading.get_ident() != request_thread[0]
        assert config.sharing_viking_endpoint == "https://configured-openviking.test"
        assert config.sharing_viking_api_key == root_key
        assert config.sharing_viking_user == "configured-team"
        assert account_id == "ov-provision-test" and admin_user == "team"

        async def update(conn):
            current = await original_read(conn)
            await configs.write_global(conn, expected_revision=current.revision, config={"sharing": {
                "endpoint": "https://changed-after-account.test", "secret_ref": reference,
            }})
        _run(pg_env, "default", update)
        return {"account_id": account_id, "admin_user": admin_user,
                "action": "failed" if account_failed else "created",
                **({"error": "fixture transport failure"} if account_failed else {})}

    def directories(config, *, account_id, extra_users):
        assert threading.get_ident() != request_thread[0]
        assert config is captured[0]
        assert config.sharing_viking_endpoint == "https://configured-openviking.test"
        assert config.sharing_viking_api_key == root_key
        assert account_id == "ov-provision-test" and extra_users == ["team"]
        return {"action": "ok", "account_id": account_id, "created": ["team", "knowledge"], "existing": []}

    monkeypatch.setattr("teamEvolver.admin.openviking_accounts.ensure_openviking_account", account)
    monkeypatch.setattr("teamEvolver.admin.openviking_dirs.ensure_openviking_dirs", directories)
    app = FastAPI()

    @app.middleware("http")
    async def platform_identity(request, call_next):
        request_thread.append(threading.get_ident())
        request.state.console_user = {"id": "operator", "platform_role": "platform_admin"}
        return await call_next(request)

    app.include_router(create_router(services))
    with TestClient(app) as client:
        response = client.post("/api/tenants", json={"account_id": "ov-provision-test", "display_name": "OV test"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["openviking_account"]["action"] == ("failed" if account_failed else "created")
    assert value["openviking_dirs"]["action"] == "ok" and value["openviking_dirs"]["created"] == 2
    assert len(reads) == 1
    assert registry.get("ov-provision-test").config_overrides["sharing_viking_account"] == "ov-provision-test"
    assert startup.sharing_viking_api_key == "" and startup.sharing_viking_endpoint == "https://stale-startup.test"
    assert root_key not in response.text and reference not in response.text
    persisted = _run(pg_env, "default", original_read)
    assert "secret_ref" in persisted.config["sharing"] and "api_key" not in persisted.config["sharing"]
    assert root_key not in json.dumps(persisted.config)
