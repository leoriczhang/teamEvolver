"""Persisted A/B jobs retain their evaluator and use a real local HTTP scoring hop."""
from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from team_replay.handlers import register_handlers
from team_replay.hooks import AgentObservation
from team_replay.integration import FixedHost
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.replay import bundle, document, judge

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="TEAMEVOLVER_TEST_PG_DSN is not configured",
)]


@contextmanager
def scoring_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(copy.deepcopy(request))
            response = {"request_id": request["request_id"],
                        "baseline": {"score": 70, "reason": "Original behavior."},
                        "candidate": {"score": 85, "reason": "Improved task correctness."}}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/score", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Factory:
    revision = "fixed-runtime-v1"

    def open(self, context):
        class Session:
            def send(self, message):
                candidate = context.treatment.branch == "candidate"
                return AgentObservation("Improved answer" if candidate else "Original answer", metrics={
                    "tool_call_count": 0, "total_tokens": 30 if candidate else 20,
                })

            def close(self):
                pass

        return Session()


def drive(env):
    def host(payload, tenant_id):
        return FixedHost(Factory(), {}, evaluation=payload["config"]["evaluation"])

    services = build_services(role="worker", tx=env.tx, handlers=register_handlers(), extras={
        "config_repo": ConfigRepository(env.tx), "replay_host_factory": host, "replay_judge": judge,
    })
    return env.tx.pool.run(process_once(services, owner_id="evaluation-test-worker"))


def test_external_and_default_modes_coexist_with_frozen_jobs(pg_env, tmp_path, monkeypatch):
    with scoring_server() as (url, calls), _pod(pg_env, tmp_path, monkeypatch, pod="evaluation") as client:
        _login(client, bootstrap=True)
        for name in ("demo", "helper"):
            saved = client.post("/api/skills", headers=H,
                                json={"name": name, "description": "Use for work", "body": "Work"})
            assert saved.status_code == 200, saved.text
        saved = client.post("/api/datasets", headers=H, json=document())
        assert saved.status_code == 201, saved.text
        original = client.get("/api/replay-evaluation", headers=H).json()
        assert original["evaluation"]["mode"] == "checklist_efficiency"
        external = {"mode": "external_agent", "external_agent": {
            "url": url, "rubric": "Score task correctness.", "min_score": 80, "min_improvement": 5,
        }}
        configured = client.put("/api/replay-evaluation", headers=H,
                                json={"expected_revision": original["revision"], "evaluation": external})
        assert configured.status_code == 200, configured.text
        request = {"skill_id": "demo", "dataset_id": "ds-test",
                   "candidate_skill_md": bundle("demo", "Better")['SKILL.md'].decode()}
        started = client.post("/api/replay-lab/runs", headers={**H, "Idempotency-Key": "external"}, json=request)
        assert started.status_code == 202, started.text
        external_id = started.json()["run_id"]
        # Changing current settings before the Worker starts must not rescore this job.
        reset = client.put("/api/replay-evaluation", headers=H, json={
            "expected_revision": configured.json()["revision"], "evaluation": {"mode": "checklist_efficiency"},
        })
        assert reset.status_code == 200, reset.text
        drive(pg_env)
        external_run = client.get(f"/api/replay-lab/runs/{external_id}", headers=H).json()
        assert external_run["status"] == "succeeded", external_run
        result = external_run["result"]
        assert result["verdict"] == "accept"
        assert result["decision_policy"]["policy"] == "external-agent-score"
        assert result["external_evaluation"]["candidate"]["score"] == 85
        assert len(calls) == 1
        assert calls[0]["branches"]["candidate"]["final_response"] == "Improved answer"
        assert "min_score" not in calls[0] and "secret_ref" not in json.dumps(calls[0])
        frozen = client.get(f"/api/replays/skill/{external_id}/input", headers=H).json()
        assert frozen["spec"]["evaluation"]["external_agent"]["url"] == url
        # The same A/B execution still fails the untouched default efficiency policy.
        started = client.post("/api/replay-lab/runs", headers={**H, "Idempotency-Key": "default"}, json=request)
        assert started.status_code == 202, started.text
        default_id = started.json()["run_id"]
        drive(pg_env)
        default_run = client.get(f"/api/replay-lab/runs/{default_id}", headers=H).json()
        assert default_run["status"] == "succeeded", default_run
        assert default_run["result"]["verdict"] == "reject"
        assert default_run["result"]["decision_policy"]["policy"] == "progressive-checklist-then-turn-priority"
        assert len(calls) == 1
        assert client.get(f"/api/replay-lab/runs/{external_id}", headers=H).json() == external_run
