"""Directory bootstrap uses persisted OpenViking settings and service credentials."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from team_skills import handlers
from teamEvolver.admin import openviking_dirs
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime import HandlerOutcome
from teamEvolver.security.secrets import SecretBox
from tests.support.config import FakeDB, FakeTxManager


@pytest.fixture
def bootstrap(monkeypatch):
    master = "bootstrap-offline-master"
    monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", master)
    db = FakeDB()
    db.global_config["config"] = {"sharing": {
        "endpoint": "https://persisted-openviking.test",
        "deployment": "local",
        "account": "persisted-account",
        "team_user": "persisted-team",
        "secret_ref": "sref_bootstrap_offline",
    }}
    db.secrets[("default", "sref_bootstrap_offline")] = {
        "ciphertext": SecretBox(master).encrypt("persisted-root-key"),
    }
    tx = FakeTxManager(db)
    transactions = []

    @asynccontextmanager
    async def admin_conn():
        transactions.append(("admin", ""))
        yield tx.conn_for()

    @asynccontextmanager
    async def tenant_tx(tenant_id):
        transactions.append(("tenant", tenant_id))
        yield tx.conn_for(tenant_id)

    monkeypatch.setattr(tx.pool, "admin_conn", admin_conn, raising=False)
    monkeypatch.setattr(tx, "tenant_tx", tenant_tx, raising=False)
    config = TeamEvolverConfig(
        sharing_viking_endpoint="http://127.0.0.1:1933",
        sharing_viking_api_key="",
        sharing_viking_account="bootstrap-account",
        sharing_viking_user="bootstrap-team",
    )
    services = SimpleNamespace(
        tx=tx,
        extras={"config": config, "config_repo": ConfigRepository(tx)},
        tasks=SimpleNamespace(require_current=AsyncMock()),
    )
    task = SimpleNamespace(task_id="bootstrap-dirs", tenant_id="tenant-secondary")
    command = {"payload": {"operation": "ov_bootstrap", "account": "", "config": {}}, "result": None}
    monkeypatch.setattr(handlers, "load_command", AsyncMock(return_value=command))
    finish = AsyncMock()
    monkeypatch.setattr(handlers, "EvolutionRepository", lambda _tx: SimpleNamespace(finish=finish))
    monkeypatch.setattr(openviking_dirs, "registered_personal_users", lambda _config: [])
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "result": {}})

    real_client = httpx.Client
    monkeypatch.setattr(
        openviking_dirs.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    return SimpleNamespace(
        services=services, task=task, db=db, command=command,
        finish=finish, requests=requests, transactions=transactions,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_override", ["", "requested-account"], ids=["pg-account", "explicit-account"])
async def test_bootstrap_uses_pg_endpoint_and_decrypted_root_key(bootstrap, account_override):
    bootstrap.command["payload"]["account"] = account_override
    outcome = await handlers.evolve(bootstrap.task, bootstrap.services)

    assert outcome.outcome == HandlerOutcome.SUCCEED
    assert outcome.result_ref == bootstrap.task.task_id
    assert bootstrap.requests
    for request in bootstrap.requests:
        assert str(request.url) == "https://persisted-openviking.test/api/v1/fs/mkdir"
        assert request.headers["x-api-key"] == "persisted-root-key"
        assert request.headers["authorization"] == "Bearer persisted-root-key"
        assert request.headers["x-openviking-account"] == (account_override or "persisted-account")
    resource_request = next(req for req in bootstrap.requests if b"viking://resources/team\"" in req.content)
    assert resource_request.headers["x-openviking-user"] == "persisted-team"
    bootstrap.services.tasks.require_current.assert_awaited_once()
    bootstrap.finish.assert_awaited_once()
    report = bootstrap.finish.call_args.args[2]["openviking_dirs"]
    assert report["action"] == "ok"
    assert report["account_id"] == (account_override or "persisted-account")
    assert report["endpoint"] == "https://persisted-openviking.test"
    assert report["checked"] == len(bootstrap.requests)
    assert report["errors"] == []
    assert "persisted-root-key" not in str(report)
    assert bootstrap.transactions == [("admin", ""), ("tenant", "default"), ("tenant", "tenant-secondary")]
    assert bootstrap.services.extras["config"].sharing_viking_api_key == ""
    assert bootstrap.services.extras["config"].sharing_viking_endpoint == "http://127.0.0.1:1933"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config-unavailable", "unknown-ref", "wrong-master"])
async def test_resolution_failure_does_not_fall_back_or_contact_openviking(bootstrap, monkeypatch, failure):
    # A usable old credential must never hide a failed read of the authoritative configuration.
    bootstrap.services.extras["config"].sharing_viking_api_key = "stale-bootstrap-key"
    if failure == "config-unavailable":
        monkeypatch.setattr(
            bootstrap.services.extras["config_repo"], "read_global",
            AsyncMock(side_effect=RuntimeError("configuration unavailable")),
        )
        expected_error, message = RuntimeError, "configuration unavailable"
    elif failure == "unknown-ref":
        bootstrap.db.secrets.clear()
        expected_error, message = KeyError, "unknown secret_ref"
    else:
        monkeypatch.setenv("TEAMEVOLVER_SECRET_MASTER_KEY", "wrong-offline-master")
        expected_error, message = ValueError, "secret decryption failed"

    with pytest.raises(expected_error, match=message):
        await handlers.evolve(bootstrap.task, bootstrap.services)

    assert bootstrap.requests == []
    bootstrap.finish.assert_not_awaited()
    bootstrap.services.tasks.require_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_bootstrap_command_does_not_resolve_settings_or_run_again(bootstrap, monkeypatch):
    bootstrap.command["result"] = {"openviking_dirs": {"action": "ok"}}
    read_global = AsyncMock(side_effect=RuntimeError("must not read configuration again"))
    monkeypatch.setattr(bootstrap.services.extras["config_repo"], "read_global", read_global)

    outcome = await handlers.evolve(bootstrap.task, bootstrap.services)

    assert outcome.outcome == HandlerOutcome.SUCCEED
    assert outcome.result_ref == bootstrap.task.task_id
    read_global.assert_not_awaited()
    assert bootstrap.transactions == []
    assert bootstrap.requests == []
    bootstrap.finish.assert_not_awaited()
