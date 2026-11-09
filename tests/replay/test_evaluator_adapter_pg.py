"""PG adapter selection, immutable policy versions and genuine HTTP scoring dispatch."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.commands import enqueue
from team_replay.evaluator_adapters import EvaluatorAdapterRepository
from team_replay.handlers import register_handlers
from team_replay.repository import ReplayRepository
from teamEvolver.app import create_app
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from teamEvolver.tenants.registry import TenantContext
from tests.support.postgres import _run
from tests.support.replay import spec

pytestmark = pytest.mark.postgres

REPLAY_SOURCE = '''from team_replay.hooks import AgentObservation
REPLAY_ADAPTER = {"label": "Local execution fixture", "enabled": True}
def build_replay_adapter(config):
    class Factory:
        def open(self, context):
            class Session:
                def send(self, message):
                    return AgentObservation("actual-" + context.treatment.branch, metrics={"total_tokens": 10})
                def close(self): pass
            return Session()
    return Factory()
'''
EVALUATOR_SOURCE = '''import httpx
EVALUATOR_ADAPTER = {"label": "Custom HTTP contract", "enabled": True}
def evaluate(request, config, *, secret=""):
    with httpx.Client(timeout=config["timeout_seconds"]) as client:
        response = client.post(config["url"], json={
            "id": request["request_id"], "question": request["case"]["query"],
            "original": request["branches"]["baseline"]["final_response"],
            "proposal": request["branches"]["candidate"]["final_response"]})
        response.raise_for_status()
        result = response.json()
    return {"baseline": {"score": result["old"], "reason": "Original compared"},
            "candidate": {"score": result["new"], "reason": "Candidate compared"}}
'''


@contextmanager
def scorer():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"old": 0, "new": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/custom", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_python_source_requires_root_and_selection_is_tenant_scoped_cas(pg_env):
    async def authorizer(request, services):
        root = request.headers.get("x-root") == "yes"
        request.state.tenant = TenantContext(request.headers.get("x-tenant-id", "default"), "test")
        request.state.console_user = {"service_root": root, "capabilities": ["tenant.manage"],
                                      "tenant_id": request.headers.get("x-tenant-id", "default")}
        request.state.service_root_authenticated = root

    services = build_services(role="api", tx=pg_env.tx, extras={"config_repo": ConfigRepository(pg_env.tx)})
    with TestClient(create_app(services=services, authorizer=authorizer)) as client:
        request = {"file": "custom.py", "code": EVALUATOR_SOURCE, "expected_revision": None}
        assert client.put("/api/replay-evaluator-adapter/code", json=request).status_code == 403
        headers = {"x-root": "yes"}
        saved = client.put("/api/replay-evaluator-adapter/code", headers=headers, json=request)
        assert saved.status_code == 200, saved.text
        reference = {key: saved.json()[key] for key in ("file", "revision", "source_hash")}
        assert client.put("/api/replay-evaluator-adapter/code", headers=headers, json=request).status_code == 409
        public = client.get("/api/replay-evaluator-adapter").json()
        assert public["available"][0]["file"] == "custom.py" and not public["source_editable"]
        assert "code" not in public["available"][0]
        assert client.get("/api/replay-evaluator-adapter/code?file=custom.py").status_code == 403
        read = client.get("/api/replay-evaluator-adapter/code?file=custom.py&revision=1", headers=headers).json()
        assert read["code"] == EVALUATOR_SOURCE
        config = client.get("/api/replay-evaluation").json()
        value = {"mode": "external_agent", "external_agent": {"adapter": reference}}
        forged = {**reference, "source_hash": "0" * 64}
        rejected = client.put("/api/replay-evaluation", json={"expected_revision": config["revision"],
                              "evaluation": {"mode": "external_agent", "external_agent": {"adapter": forged}}})
        assert rejected.status_code == 400
        selected = client.put("/api/replay-evaluation", json={"expected_revision": config["revision"],
                              "evaluation": value})
        assert selected.status_code == 200, selected.text
        assert selected.json()["evaluation"]["external_agent"]["adapter"] == reference
        other = client.get("/api/replay-evaluation", headers={"x-tenant-id": "alpha"}).json()
        denied = client.put("/api/replay-evaluation", headers={"x-tenant-id": "alpha"},
                            json={"expected_revision": other["revision"], "evaluation": value})
        assert denied.status_code == 404
        assert client.get("/api/replay-evaluator-adapter", headers={"x-tenant-id": "alpha"}).json()["available"] == []
        # The executable example is distributed without running it on the API process.
        assert "def evaluate" in client.get("/api/replay-evaluator-adapter/example").json()["code"]


def test_worker_uses_saved_source_version_after_evaluator_and_config_are_updated(pg_env):
    repo = EvaluatorAdapterRepository(pg_env.tx)
    configs = ConfigRepository(pg_env.tx)
    with scorer() as (url, calls):
        services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
            "config_repo": configs, "replay_judge_harness": lambda *_: {},
        })

        async def accept(conn):
            execution = ReplayAdapterRepository(pg_env.tx)
            await execution.save(conn, tenant_id="default", filename="custom.py", source=REPLAY_SOURCE,
                                 expected_revision=None)
            await execution.bind(conn, tenant_id="default", filename="custom.py")
            saved = await repo.save(conn, tenant_id="default", filename="custom.py", source=EVALUATOR_SOURCE,
                                    expected_revision=None)
            reference = {key: saved[key] for key in ("file", "revision", "source_hash")}
            row = await configs.read_tenant_module(conn, "replay")
            config = {"evaluation": {"mode": "external_agent", "external_agent": {"url": url,
                      "adapter": reference, "timeout_seconds": 5}}}
            revision = await configs.write_tenant_module(conn, module="replay", expected_revision=row.revision,
                                                         config=config)
            run_spec = replace(spec(), limits={"timeout_seconds": 30, "max_interactions": 1})
            record = await enqueue(conn, services, tenant_id="default", domain="skill", run_id="versioned-grader",
                                   subject_id="demo", request={"test": True}, spec=run_spec)
            assert record["input"]["config"]["evaluation"]["external_agent"]["adapter"] == reference
            newer = EVALUATOR_SOURCE.replace('"score": result["new"]', '"score": 0')
            await repo.save(conn, tenant_id="default", filename="custom.py", source=newer, expected_revision=1)
            await configs.write_tenant_module(conn, module="replay", expected_revision=revision,
                                             config={"evaluation": {"mode": "checklist_efficiency"}})
            assert (await repo.source(conn, "custom.py"))["revision"] == 2
            return reference

        reference = _run(pg_env, "default", accept)
        pg_env.tx.pool.run(process_once(services, owner_id="adapter-grader-test"))
        result = _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).get(
            conn, "skill", "versioned-grader"))
        assert result["result"]["verdict"] == "accept", result
        assert result["result"]["external_evaluation"]["candidate"]["score"] == 1
        assert calls[0]["original"] == "actual-baseline" and calls[0]["proposal"] == "actual-candidate"
        assert len(calls) == 1
        assert result["input"]["config"]["evaluation"]["external_agent"]["adapter"] == reference
        assert reference["source_hash"] == hashlib.sha256(EVALUATOR_SOURCE.encode()).hexdigest()
