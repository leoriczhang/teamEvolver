"""Evaluation selection preserves tenant config, CAS, and immutable accepted inputs."""
from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_replay.api.evaluation import create_router
from team_replay.commands import enqueue
from team_replay.config import load_replay_config
from team_replay.evaluation import EXTERNAL_AGENT_SCORE_POLICY
from team_replay.integration import host_for
from teamEvolver.config_runtime import ConfigRepository, TenantConfigMissing
from tests.support.config import FakeDB, FakeTxManager
from tests.support.replay import spec


def evaluation():
    return {"mode": "external_agent", "external_agent": {
        "url": "http://evaluator.example/score", "secret_ref": "", "timeout_seconds": 60,
        "rubric": "Assess task correctness and faithfulness to the customer role.",
        "min_score": 60, "min_improvement": 2,
    }}


@pytest.fixture
def settings_client(monkeypatch):
    db = FakeDB()
    tx = FakeTxManager(db)
    db.tenant_configs[("a", "replay")] = {"revision": 1, "config": json.dumps({
        "max_interactions": 3, "agent_lab": {"secret_ref": "sref_unchanged"},
    })}

    @asynccontextmanager
    async def tenant_tx(tenant_id):
        yield tx.conn_for(tenant_id)

    tx.tenant_tx = tenant_tx
    services = SimpleNamespace(tx=tx, extras={"config_repo": ConfigRepository(tx)})
    monkeypatch.setattr("team_replay.api.evaluation.current_tenant_id", lambda: "a")
    monkeypatch.setattr("teamEvolver.admin.authorization.current_tenant_id", lambda: "a")
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        admin = request.headers.get("X-Test-Role", "admin") == "admin"
        request.state.console_user = {"id": "test", "tenant_id": "a",
                                      "capabilities": ["tenant.manage"] if admin else ["team.read"]}
        return await call_next(request)

    app.include_router(create_router(services))
    with TestClient(app) as client:
        yield client, db, services


def test_settings_cas_preserves_other_config_and_requires_admin(settings_client):
    client, db, _ = settings_client
    before = client.get("/api/replay-evaluation").json()
    assert before["revision"] == 1
    assert before["evaluation"]["mode"] == "checklist_efficiency"
    value = evaluation()
    denied = client.put("/api/replay-evaluation", headers={"X-Test-Role": "member"},
                        json={"expected_revision": 1, "evaluation": value})
    assert denied.status_code == 403
    saved = client.put("/api/replay-evaluation", json={"expected_revision": 1, "evaluation": value})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    raw = json.loads(db.tenant_configs[("a", "replay")]["config"])
    assert raw["max_interactions"] == 3
    assert raw["agent_lab"] == {"secret_ref": "sref_unchanged"}
    stale = client.put("/api/replay-evaluation", json={"expected_revision": 1, "evaluation": value})
    assert stale.status_code == 409
    assert client.get("/api/replay-evaluation").json() == saved.json()


@pytest.mark.parametrize("update", [
    {"api_key": "must-not-be-saved"}, {"min_score": True}, {"min_improvement": -1},
    {"url": "http://user:password@evaluator.example/score"},
])
def test_invalid_external_settings_are_rejected_without_config_change(settings_client, update):
    client, _, _ = settings_client
    value = evaluation()
    value["external_agent"].update(update)
    response = client.put("/api/replay-evaluation", json={"expected_revision": 1, "evaluation": value})
    assert response.status_code == 400
    assert client.get("/api/replay-evaluation").json()["revision"] == 1
    assert "must-not-be-saved" not in response.text and "password@" not in response.text


@pytest.mark.asyncio
async def test_missing_replay_configuration_still_fails_closed():
    tx = FakeTxManager(FakeDB())
    with pytest.raises(TenantConfigMissing):
        await load_replay_config(ConfigRepository(tx), tx.conn_for("missing"))


@pytest.mark.asyncio
async def test_acceptance_freezes_evaluator_and_cannot_inherit_later_config(monkeypatch):
    db = FakeDB()
    tx = FakeTxManager(db)
    config = {"evaluation": evaluation()}
    db.tenant_configs[("a", "replay")] = {"revision": 1, "config": json.dumps(config)}
    captured, tasks = [], []

    async def binding(self, conn):
        return {"adapter_id": "demo", "revision": 1, "source_hash": "fixed"}

    async def accept(self, conn, **kwargs):
        captured.append(copy.deepcopy(kwargs))
        return {"input_hash": "frozen"}, True

    async def queue(conn, **kwargs):
        tasks.append(kwargs)

    monkeypatch.setattr("team_replay.commands.ReplayAdapterRepository.binding", binding)
    monkeypatch.setattr("team_replay.commands.ReplayRepository.accept", accept)
    services = SimpleNamespace(tx=tx, tasks=SimpleNamespace(enqueue=queue),
                               extras={"config_repo": ConfigRepository(tx)})
    await enqueue(tx.conn_for("a"), services, tenant_id="a", domain="skill", run_id="once",
                  subject_id="demo", request={}, spec=spec())
    frozen = captured[0]["payload"]
    assert frozen["config_revision"] == 1
    assert frozen["spec"]["policy"]["id"] == EXTERNAL_AGENT_SCORE_POLICY
    assert frozen["spec"]["evaluation"] == frozen["config"]["evaluation"]
    config["evaluation"]["external_agent"]["min_score"] = 99
    db.tenant_configs[("a", "replay")] = {"revision": 2, "config": json.dumps(config)}
    assert frozen["spec"]["evaluation"]["external_agent"]["min_score"] == 60
    assert tasks[0]["config_revision"] == 1


@pytest.mark.asyncio
async def test_worker_resolves_external_secret_without_putting_plaintext_into_factory_config(monkeypatch):
    from team_replay._util import stable_hash

    value = evaluation()
    value["external_agent"]["secret_ref"] = "sref_" + "a" * 32
    factory_configs, requests = [], []

    @asynccontextmanager
    async def tenant_tx(tenant_id):
        assert tenant_id == "a"
        yield object()

    async def source(self, conn, adapter_id, revision):
        return {"source": "frozen-source"}

    async def resolve(conn, *, secret_ref):
        assert secret_ref == "sref_" + "a" * 32
        return "synthetic-secret-for-test"

    def build(source, adapter_id, config, **kwargs):
        factory_configs.append(vars(config))
        return object()

    def external(request, config, secret):
        assert secret == "synthetic-secret-for-test"
        requests.append(copy.deepcopy(request))
        return {"baseline": {"score": 60, "reason": "Original."},
                "candidate": {"score": 80, "reason": "Improved."}}

    monkeypatch.setattr("team_replay.integration.ReplayAdapterRepository.source", source)
    monkeypatch.setattr("team_replay.integration.build_from_content", build)
    monkeypatch.setattr("team_replay.integration.evaluate_external_http", external)
    services = SimpleNamespace(tx=SimpleNamespace(tenant_tx=tenant_tx, pool=SimpleNamespace(schema="test")), extras={
        "secret_repo": SimpleNamespace(resolve=resolve), "replay_judge_harness": lambda *args: {},
    })
    host = await host_for({"adapter": {"adapter_id": "demo", "revision": 1,
                                      "source_hash": stable_hash("frozen-source")},
                           "config": {"evaluation": value}}, services, "a")
    response = host.evaluate_external({"request_id": "fixed"})
    assert response["candidate"]["score"] == 80 and requests == [{"request_id": "fixed"}]
    assert "synthetic-secret-for-test" not in json.dumps(factory_configs)
    assert factory_configs[0]["evaluation"]["external_agent"]["secret_ref"] == "sref_" + "a" * 32
