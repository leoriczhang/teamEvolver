"""OpenViking settings persist Secret Refs without claiming a connection probe."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teamEvolver.admin.api.settings import create_router
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.security import SecretBox, SecretRepository
from tests.support.config import FakeDB, FakeTxManager


@pytest.fixture
def settings(monkeypatch):
    master = "settings-offline-master"
    monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", master)
    db = FakeDB()
    tx = FakeTxManager(db)

    @asynccontextmanager
    async def admin_conn():
        yield tx.conn_for()

    @asynccontextmanager
    async def tenant_tx(tenant_id):
        yield tx.conn_for(tenant_id)

    monkeypatch.setattr(tx.pool, "admin_conn", admin_conn, raising=False)
    monkeypatch.setattr(tx, "tenant_tx", tenant_tx, raising=False)
    services = SimpleNamespace(tx=tx, extras={
        "config": TeamEvolverConfig(sharing_viking_api_key=""),
        "config_repo": ConfigRepository(tx),
    })
    app = FastAPI()

    @app.middleware("http")
    async def console_identity(request, call_next):
        request.state.console_user = {
            "id": "operator", "platform_role": "platform_admin", "service_root": True,
        }
        return await call_next(request)

    app.include_router(create_router(services))
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, db=db, tx=tx,
            secrets=SecretRepository(tx, SecretBox(master)),
        )


def _persisted_sharing(settings):
    raw = settings.db.global_config["config"]
    return (json.loads(raw) if isinstance(raw, str) else raw)["sharing"]


@pytest.mark.parametrize("endpoint_field", ["endpoint", "endpoint_override"])
def test_save_openviking_settings_encrypts_key_and_does_not_claim_reachability(settings, endpoint_field):
    root_key = "offline-openviking-settings-root-key"
    response = settings.client.put("/api/sharing-config", json={
        "deployment": "local", endpoint_field: "https://saved-openviking.test",
        "account": "saved-account", "team_user": "saved-team", "api_key": root_key,
    })

    assert response.status_code == 200, response.text
    expected = {
        "deployment": "local", "endpoint": "https://saved-openviking.test",
        "account": "saved-account", "team_user": "saved-team", "api_key_present": True,
    }
    assert response.json() == expected
    assert "reachable" not in response.json()
    assert root_key not in response.text
    persisted = _persisted_sharing(settings)
    assert persisted["endpoint"] == expected["endpoint"]
    assert "api_key" not in persisted
    assert root_key not in str(settings.db.global_config)
    ref = persisted["secret_ref"]
    assert ref.startswith("sref_")
    assert set(settings.db.secrets) == {("default", ref)}
    assert root_key not in settings.db.secrets[("default", ref)]["ciphertext"]
    assert asyncio.run(settings.secrets.resolve(settings.tx.conn_for("default"), secret_ref=ref)) == root_key
    read = settings.client.get("/api/sharing-config")
    assert read.status_code == 200
    assert read.json() == expected


@pytest.mark.parametrize("key_update", [{}, {"api_key": ""}], ids=["omitted-key", "blank-key"])
def test_endpoint_update_preserves_existing_secret_ref(settings, key_update):
    root_key = "offline-openviking-original-root-key"
    initial = settings.client.put("/api/sharing-config", json={
        "endpoint": "https://original-openviking.test", "api_key": root_key,
    })
    assert initial.status_code == 200, initial.text
    original_ref = _persisted_sharing(settings)["secret_ref"]
    response = settings.client.put("/api/sharing-config", json={
        "endpoint_override": "https://updated-openviking.test", **key_update,
    })

    assert response.status_code == 200, response.text
    assert response.json()["endpoint"] == "https://updated-openviking.test"
    assert response.json()["api_key_present"] is True
    assert "reachable" not in response.json()
    assert "api_key" not in response.json()
    assert root_key not in response.text
    assert _persisted_sharing(settings)["secret_ref"] == original_ref
    assert set(settings.db.secrets) == {("default", original_ref)}
    read = settings.client.get("/api/sharing-config")
    assert read.status_code == 200
    assert read.json()["api_key_present"] is True
    assert read.json()["endpoint"] == "https://updated-openviking.test"
    assert asyncio.run(
        settings.secrets.resolve(settings.tx.conn_for("default"), secret_ref=original_ref),
    ) == root_key
