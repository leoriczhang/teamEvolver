"""A/B orchestration and source-code authorization acceptance tests."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_replay import engine
from team_replay.execution import run_branch
from team_replay.hooks import AgentObservation, ReplayUnsupported
from team_replay.host import configure_host, current_host
from team_replay.runtime_matrix import prepare_runtime_validation


@pytest.fixture
def replay_host(monkeypatch):
    calls = []
    sessions = []

    class Session:
        def __init__(self, context):
            self.context = context
            self.sent = []
            self.closed = False
            sessions.append(self)

        def send(self, message):
            self.sent.append(message)
            # Customer mutations must never reach another branch or the stored dataset.
            self.context.context_snapshot["items"].append({"private": True})
            return AgentObservation("complete", metrics={"total_tokens": 20 if self.context.treatment.branch == "baseline" else 10})

        def close(self):
            self.closed = True

    class Factory:
        def open(self, context):
            calls.append(copy.deepcopy(context))
            return Session(context)

    class Host:
        resolutions = 0

        def resolve_replay_factory(self, runtime_type, source_session):
            self.resolutions += 1
            return Factory()

        def validate_skill_treatment(self, skill): return {"passed": True}
        def load_source_session(self, session_id): return None
        def judge_harness(self): return {}

    host = Host()
    previous = current_host(required=False)
    configure_host(host)

    def judge(**kwargs):
        return {"judge": "model", "all_satisfied": True,
                "items": [{**item, "satisfied": True, "evidence": "complete"} for item in kwargs["checklist"]]}

    monkeypatch.setattr(engine, "run_branch", lambda *a, **kw: run_branch(*a, **kw, judge=judge))
    yield host, calls, sessions
    configure_host(previous)


def job():
    return {"candidate_skill": {"name": "candidate"}, "current_skill": {"name": "published"}, "replay_cases": [{
        "query": "Produce the report", "checklist": [{"id": "R01", "text": "Hidden evaluation goal"}],
        "source_runtime": {"type": "unregistered"}, "source_runtime_context": {"user_id": "alice"},
        "context_snapshot": {"items": [{"content": "same context"}]},
        "materials": [{"path": "report.txt", "content_b64": "YQ=="}],
    }]}


def test_engine_uses_one_factory_and_isolated_sessions_for_all_cases(replay_host):
    host, calls, sessions = replay_host
    data = job()
    data["replay_cases"].append(copy.deepcopy(data["replay_cases"][0]))
    original = copy.deepcopy(data)
    result = engine.evaluate_job("test", job=data)
    assert result["status"] == "evaluated" and result["accepted"]
    assert result["case_count"] == 2
    assert host.resolutions == 1 and len(sessions) == 4
    assert all(session.sent == ["Produce the report"] and session.closed for session in sessions)
    for a, b in zip(calls[::2], calls[1::2]):
        assert a.request_id != b.request_id
        assert replace(a, request_id=b.request_id, treatment=b.treatment) == b
        assert "Hidden evaluation goal" not in repr(a)
        assert a.treatment.skill["name"] == "published" and b.treatment.skill["name"] == "candidate"
    assert data == original
    assert result["efficiency"]["dimensions"]["api_calls"]["winner"] == "unavailable"


def test_engine_rejects_shared_session_and_closes_it(replay_host):
    host, _, sessions = replay_host
    factory = host.resolve_replay_factory("custom", {})
    ctx = engine.make_context("baseline", None, job()["replay_cases"][0], {"runtime": {"type": "custom"}}, 30)
    session = factory.open(ctx)
    factory.open = lambda _: session
    results = engine.execute_pair(factory, {"baseline": ctx, "candidate": replace(ctx, request_id="different")},
                                  job()["replay_cases"][0], harness={}, max_interactions=2)
    assert all(result["status"] == "unsupported" for result in results.values())
    assert session.closed and not session.sent


def test_runtime_matrix_uses_configured_names_and_neutral_context():
    case = job()["replay_cases"][0]
    prepared = prepare_runtime_validation(skill={"portable": True}, sessions=[], replay_cases=[case],
                                           validation_runtimes=["unregistered", "another"])
    assert prepared["policy"]["available_replay_runtimes"] == ["another", "unregistered"]
    neutral = prepared["replay_cases"][1]
    assert neutral["source_runtime"] == {"type": "another"}
    assert neutral["context_snapshot"] == {} and neutral["source_runtime_context"] == {}


def test_case_selects_an_explicit_subset_from_peer_skills():
    treatment = {
        "kind": "skill_set",
        "skills": [
            {"name": "skill-a"},
            {"name": "skill-b"},
            {"name": "skill-c"},
        ],
    }

    selected = engine.treatment_for_case(
        treatment,
        {"skill_ids": ["skill-a", "skill-c"]},
    )

    assert [item["name"] for item in selected["skills"]] == [
        "skill-a",
        "skill-c",
    ]
    with pytest.raises(ReplayUnsupported, match="skill-missing"):
        engine.treatment_for_case(
            treatment,
            {"skill_ids": ["skill-missing"]},
        )


CODE = '''
from team_replay.hooks import AgentObservation
REPLAY_ADAPTER = {"label": "test", "enabled": True}
class Session:
    def send(self, query): return AgentObservation(query)
    def close(self): pass
class Factory:
    def open(self, context): return Session()
def build_replay_adapter(config): return Factory()
'''


def test_source_read_write_and_test_require_root(tmp_path):
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.proxy.replay_routes import register_replay_adapter_routes
    from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
    app = FastAPI()
    owner = SimpleNamespace(config=TeamEvolverConfig(replay_adapters_dir=str(tmp_path)))
    register_replay_adapter_routes(owner, app)

    @app.middleware("http")
    async def claims(request, call_next):
        request.state.console_user = {"role": "admin"}
        request.state.service_root_authenticated = request.headers.get("x-test-identity") == "root"
        token = set_current_tenant(TenantContext())
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token)

    client = TestClient(app)
    payload = {"file": "example.py", "code": CODE, "expected_revision": None}
    for method, path, body in [
        ("get", "/api/replay-adapter/code?file=example.py", None),
        ("put", "/api/replay-adapter/code", payload),
        ("post", "/api/replay-adapter/code/test", {}),
    ]:
        assert client.request(method, path, json=body).status_code == 403
    headers = {"x-test-identity": "root"}
    assert client.put("/api/replay-adapter/code", json=payload, headers=headers).status_code == 200
    assert client.get("/api/replay-adapter/code?file=example.py", headers=headers).json()["code"] == CODE
    result = client.post("/api/replay-adapter/code/test", headers=headers, json={
        "file": "example.py", "code": CODE, "query": "explicit test", "context": {"runtime_type": "unregistered"},
    })
    assert result.status_code == 200 and result.json()["observation"]["response"] == "explicit test"
    assert client.put("/api/replay-adapter/code", json=payload, headers=headers).status_code == 409
