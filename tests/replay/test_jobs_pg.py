"""Real PostgreSQL acceptance: replay snapshots, RLS, restart and Worker fencing."""

from __future__ import annotations

import base64
import os

import pytest

from team_replay.datasets.repository import DatasetConflict, DatasetRepository
from team_replay.handlers import register_handlers
from team_replay.repository import ReplayRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run
from tests.support.replay import Host, bundle, document, judge

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="TEAMEVOLVER_TEST_PG_DSN is not configured"
)


def save(env, *, empty=False, files=None):
    doc = document(empty=empty)
    if not empty:
        doc["skills"] = []
        doc["cases"][0]["skill_ids"] = []
    if files:
        doc["cases"][0]["materials"] = [{"path": "input.txt"}]
    return _run(
        env,
        "default",
        lambda conn: DatasetRepository(env.tx).save(
            conn,
            tenant_id="default",
            document=doc,
            files=files,
        ),
    )


def seed_skills(client):
    for name in ("demo", "helper"):
        response = client.post(
            "/api/skills",
            headers=H,
            json={
                "name": name,
                "description": "Use for work",
                "body": "Work",
            },
        )
        assert response.status_code == 200, response.text


def drive(env, host=None):
    services = build_services(
        role="worker",
        tx=env.tx,
        handlers=register_handlers(),
        extras={"replay_host_factory": lambda payload, tenant: host or Host(), "replay_judge": judge},
    )
    return env.tx.pool.run(process_once(services, owner_id="worker-a"))


@pytest.mark.postgres
def test_api_restart_worker_and_edit_do_not_change_replay_inputs(pg_env, tmp_path, monkeypatch):
    _run(
        pg_env,
        "default",
        lambda conn: DatasetRepository(pg_env.tx).save(
            conn,
            tenant_id="default",
            document=document(),
        ),
    )
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        seed_skills(client)
        request = {
            "skill_id": "demo",
            "dataset_id": "ds-test",
            "candidate_skill_md": bundle("demo", "Better")["SKILL.md"].decode(),
            "candidate_files": {"scripts/run.py": "print('unsaved')"},
        }
        response = client.post("/api/replay-lab/runs", headers={**H, "Idempotency-Key": "once"}, json=request)
        assert response.status_code == 202, response.text
        rid = response.json()["run_id"]
    repo = DatasetRepository(pg_env.tx)
    changed = document()
    changed["cases"][0]["query"] = "Edited afterwards"
    _run(
        pg_env,
        "default",
        lambda conn: repo.save(
            conn,
            tenant_id="default",
            document=changed,
            expected_revision=1,
        ),
    )
    host = Host()
    drive(pg_env, host)
    with _pod(pg_env, tmp_path, monkeypatch, pod="b") as client:
        _login(client, bootstrap=False)
        row = client.get(f"/api/replay-lab/runs/{rid}", headers=H)
        assert row.status_code == 200, row.text
        assert row.json()["status"] == "succeeded" and row.json()["result"]["verdict"] == "accept"
        frozen = client.get(f"/api/replays/skill/{rid}/input", headers=H).json()
        assert frozen["spec"]["dataset"]["cases"][0]["query"] == "Execute the task"
        assert frozen["spec"]["metadata"]["skill_versions"] == {"demo": 1, "helper": 1}
        retry = client.post("/api/replay-lab/runs", headers={**H, "Idempotency-Key": "once"}, json=request)
        assert retry.status_code == 202 and retry.json()["run_id"] == rid
    assert len(host.contexts) == 2
    assert host.contexts[1].treatment.skill["skills"][0]["bundle"]["files"][1]["path"] == "scripts/run.py"


@pytest.mark.postgres
def test_dataset_revisions_materials_and_all_tables_are_tenant_isolated(pg_env):
    files = [{"path": "input.txt", "content_b64": base64.b64encode(b"original").decode()}]
    saved = save(pg_env, files=files)
    repo = DatasetRepository(pg_env.tx)
    fixed = _run(pg_env, "default", lambda conn: repo.freeze(conn, "ds-test"))
    assert fixed["document_hash"] == saved["document_hash"]
    material = fixed["execution_document"]["cases"][0]["materials"][0]
    assert base64.b64decode(material["content_b64"]) == b"original"
    with pytest.raises(DatasetConflict):
        _run(pg_env, "default", lambda conn: repo.save(conn, tenant_id="default", document=document()))
    for table in (
        "datasets",
        "dataset_revisions",
        "dataset_cases",
        "dataset_materials",
        "skill_replay_runs",
        "memory_replay_runs",
        "dataset_replay_runs",
    ):
        assert (
            _run(
                pg_env,
                "alpha",
                lambda conn, table=table: conn.fetchval(
                    f"SELECT count(*) FROM {pg_env.schema}.{table}",
                ),
            )
            == 0
        )
    with pytest.raises(Exception, match="row-level security"):
        _run(
            pg_env,
            "alpha",
            lambda conn: conn.execute(
                f"INSERT INTO {pg_env.schema}.dataset_materials(tenant_id,sha256,content,size) "
                "VALUES('default','forged',$1,1)",
                b"x",
            ),
        )


@pytest.mark.postgres
def test_empty_draft_and_worker_dataset_run(pg_env, tmp_path, monkeypatch):
    save(pg_env, empty=True)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        assert client.post("/api/datasets/ds-test/runs", headers=H, json={}).status_code == 400
        update = client.patch(
            "/api/datasets/ds-test",
            headers=H,
            json={
                "expected_revision": 1,
                "cases": document()["cases"],
            },
        )
        assert update.status_code == 200, update.text
        seed_skills(client)
        start = client.post("/api/datasets/ds-test/runs", headers=H, json={})
        assert start.status_code == 202, start.text
        assert start.json()["status"] == "queued"
        drive(pg_env)
        rid = start.json()["run_id"]
        result = client.get(f"/api/datasets/ds-test/runs/{rid}", headers=H).json()
        assert result["status"] == "succeeded" and result["completed"] == 1
        assert client.get(f"/api/datasets/ds-test/runs/{rid}/results/case-1", headers=H).json()["completed"]


@pytest.mark.postgres
def test_request_rolls_back_if_task_acceptance_fails(pg_env, tmp_path, monkeypatch):
    save(pg_env)
    from teamEvolver.runtime.repository import WorkerTaskRepository

    async def fail(*args, **kwargs):
        raise RuntimeError("enqueue failure")

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        monkeypatch.setattr(WorkerTaskRepository, "enqueue", fail)
        response = client.post("/api/datasets/ds-test/runs", headers=H, json={})
        assert response.status_code == 500
    assert _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).list(conn, "dataset")) == []


CODE = """
from team_replay.hooks import AgentObservation
REPLAY_ADAPTER = {"enabled": True, "label": "fake"}
class Session:
    def __init__(self, context): self.context = context
    def send(self, message):
        return AgentObservation("fixed-v1", metrics={"tool_call_count": 1, "total_tokens":
            20 if self.context.treatment.branch == "baseline" else 10})
    def close(self): pass
class Factory:
    def open(self, context): return Session(context)
def build_replay_adapter(config): return Factory()
"""


@pytest.mark.postgres
def test_adapter_revision_is_fixed_and_source_requires_root(pg_env, tmp_path, monkeypatch):
    from team_replay.adapters.repository import ReplayAdapterRepository

    adapters = ReplayAdapterRepository(pg_env.tx)

    async def seed(conn):
        await adapters.save(conn, tenant_id="default", filename="example.py", source=CODE, expected_revision=None)
        await adapters.bind(conn, tenant_id="default", filename="example.py")

    _run(pg_env, "default", seed)
    save(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        assert client.get("/api/replay-adapter/code?file=example.py", headers=H).status_code == 403
        response = client.post("/api/datasets/ds-test/runs", headers=H, json={})
        assert response.status_code == 202
        rid = response.json()["run_id"]
    _run(
        pg_env,
        "default",
        lambda conn: adapters.save(
            conn,
            tenant_id="default",
            filename="example.py",
            source=CODE.replace("fixed-v1", "changed-v2"),
            expected_revision=1,
        ),
    )
    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={"replay_judge": judge})
    pg_env.tx.pool.run(process_once(services, owner_id="a"))
    row = _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).get(conn, "dataset", rid))
    assert row["result"]["cases"]["case-1"]["final_response"] == "fixed-v1"
    assert row["input"]["adapter"]["revision"] == 1 and "source" not in row["input"]["adapter"]


@pytest.mark.postgres
def test_memory_runs_are_separate_and_inputs_survive_edits(pg_env, tmp_path, monkeypatch):
    request = {
        "subject_id": "facts",
        "dataset": document(),
        "runtime": {"type": "fake"},
        "baseline": {"revision": "v1", "uri": "viking://team/facts", "content": "Old"},
        "candidate": {"revision": "v2", "uri": "viking://team/facts", "content": "New"},
    }
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/replays/memory", headers=H, json=request)
        assert response.status_code == 202, response.text
        rid = response.json()["run_id"]
        drive(pg_env)
        result = client.get(f"/api/replays/memory/{rid}", headers=H).json()
        assert result["result"]["verdict"] == "accept"
        assert client.get(f"/api/replays/skill/{rid}", headers=H).status_code == 404


@pytest.mark.postgres
def test_worker_resume_keeps_completed_cases_and_cancel_prevents_new_work(pg_env, tmp_path, monkeypatch):
    from teamEvolver.runtime.repository import WorkerTaskRepository

    save(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/datasets/ds-test/runs", headers=H, json={})
        rid = response.json()["run_id"]
        original = ReplayRepository.finish

        async def fail_once(*args, **kwargs):
            raise RuntimeError("crash after Case checkpoint")

        monkeypatch.setattr(ReplayRepository, "finish", fail_once)
        host = Host()
        drive(pg_env, host)
        assert len(host.contexts) == 1
        monkeypatch.setattr(ReplayRepository, "finish", original)
        _run(
            pg_env,
            "default",
            lambda conn: conn.execute(
                f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE task_id=$1",
                rid,
            ),
        )
        drive(pg_env, host)
        assert len(host.contexts) == 1
        assert client.get(f"/api/datasets/ds-test/runs/{rid}", headers=H).json()["status"] == "succeeded"
        cancelled = client.post("/api/datasets/ds-test/runs", headers=H, json={}).json()["run_id"]
        client.post(f"/api/datasets/ds-test/runs/{cancelled}/cancel", headers=H)
        drive(pg_env, host)
        assert len(host.contexts) == 1
        status = _run(pg_env, "default", lambda conn: WorkerTaskRepository(pg_env.tx).get(conn, task_id=cancelled))
        assert status["status"] == "cancelled"


@pytest.mark.postgres
def test_two_workers_execute_each_case_once(pg_env, tmp_path, monkeypatch):
    import asyncio

    save(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/datasets/ds-test/runs", headers=H, json={})
        rid = response.json()["run_id"]
    host = Host()
    services = build_services(
        role="worker",
        tx=pg_env.tx,
        handlers=register_handlers(),
        extras={"replay_host_factory": lambda *_: host, "replay_judge": judge},
    )

    async def compete():
        return await asyncio.gather(process_once(services, owner_id="a"), process_once(services, owner_id="b"))

    claimed = pg_env.tx.pool.run(compete())
    assert sum(task is not None for task in claimed) == 1 and len(host.contexts) == 1
    result = _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).get(conn, "dataset", rid))
    assert result["result"]["completed"] == 1


@pytest.mark.postgres
def test_current_engine_integrates_with_candidate_validation(pg_env, tmp_path, monkeypatch):
    from team_skills.handlers import register_handlers as skill_handlers
    from tests.support.candidates import candidate, seed_candidate

    cid = seed_candidate(pg_env)
    _run(
        pg_env,
        "default",
        lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.skill_candidates SET detail=$1::jsonb WHERE candidate_id=$2",
            __import__("json").dumps({"replay_cases": [{**document()["cases"][0], "skill_ids": ["demo"]}]}),
            cid,
        ),
    )
    host = Host()
    services = build_services(
        role="worker",
        tx=pg_env.tx,
        handlers=skill_handlers(),
        extras={"replay_host_factory": lambda *_: host, "replay_judge": judge},
    )
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=H)
        assert response.status_code == 202
        pg_env.tx.pool.run(process_once(services, owner_id="candidate"))
    row = candidate(pg_env, cid)
    assert row["verdict"] == "accept" and len(host.contexts) == 2
    history = _run(
        pg_env,
        "default",
        lambda conn: ReplayRepository(pg_env.tx).get(
            conn,
            "skill",
            response.json()["job_id"],
        ),
    )
    assert history["result"]["replay"]["verdict"] == "accept"


@pytest.mark.postgres
def test_candidate_resume_retains_each_completed_case(pg_env, tmp_path, monkeypatch):
    import copy
    import json

    from team_replay.queries import present_run
    from team_skills.handlers import register_handlers as skill_handlers
    from tests.support.candidates import candidate, seed_candidate

    cid = seed_candidate(pg_env)
    cases = []
    for index in range(3):
        case = copy.deepcopy(document()["cases"][0])
        case.update(case_id=f"case-{index}", skill_ids=["demo"])
        cases.append(case)
    _run(
        pg_env, "default",
        lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.skill_candidates SET detail=$1::jsonb WHERE candidate_id=$2",
            json.dumps({"replay_cases": cases}), cid,
        ),
    )
    host = Host()
    services = build_services(
        role="worker", tx=pg_env.tx, handlers=skill_handlers(),
        extras={"replay_host_factory": lambda *_: host, "replay_judge": judge},
    )
    original = ReplayRepository.finish

    async def fail_after_checkpoints(*args, **kwargs):
        raise RuntimeError("crash after checkpointing all Cases")

    with _pod(pg_env, tmp_path, monkeypatch, pod="candidate-resume") as client:
        _login(client, bootstrap=True)
        accepted = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=H).json()
        monkeypatch.setattr(ReplayRepository, "finish", fail_after_checkpoints)
        pg_env.tx.pool.run(process_once(services, owner_id="first"))
        assert len(host.contexts) == 6

        async def progress(conn):
            record = await ReplayRepository(pg_env.tx).get(conn, "skill", accepted["job_id"])
            return await present_run(conn, services, "skill", record)

        view = _run(pg_env, "default", progress)
        assert view["completed"] == 3 and view["succeeded"] == 3
        assert len(view["cases"]) == 3 and view["result"] is None
        monkeypatch.setattr(ReplayRepository, "finish", original)
        _run(
            pg_env, "default", lambda conn: conn.execute(
                f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE task_id=$1",
                accepted["job_id"],
            ),
        )
        pg_env.tx.pool.run(process_once(services, owner_id="resumed"))
        assert len(host.contexts) == 6
        assert candidate(pg_env, cid)["verdict"] == "accept"


@pytest.mark.postgres
def test_session_selection_and_async_synthesis_use_persisted_inputs(pg_env, tmp_path, monkeypatch):
    import json

    from session_ingestion.schemas import session_payload_hash

    session = {
        "session_id": "s1",
        "runtime": {"type": "fake"},
        "used_skills": ["demo"],
        "turns": [{"turn_num": 1, "prompt_text": "Complete work", "response_text": "Done"}],
    }

    async def seed(conn):
        await conn.execute(
            f"INSERT INTO {pg_env.schema}.sessions(tenant_id,session_id,content_ref,content_hash,source) "
            "VALUES('default','s1','s1',$1,'fake')",
            session_payload_hash(session),
        )

    _run(pg_env, "default", seed)

    class Payloads:
        def get_session(self, ref):
            return session

    class Model:
        async def chat(self, *args, **kwargs):
            return json.dumps(
                {
                    "test_datasets": [
                        {
                            "query": "Complete work",
                            "source_session_ids": ["s1"],
                            "checks": [
                                {"id": f"R{i}", "kind": "output", "text": f"Grounded condition {i}"} for i in range(12)
                            ],
                        }
                    ]
                }
            )

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        client.app.state.services.extras["session_payloads"] = Payloads()
        _login(client, bootstrap=True)
        seed_skills(client)
        selected = client.post("/api/datasets", headers=H, json={"name": "selected", "session_ids": ["s1"]})
        assert selected.status_code == 201, selected.text
        response = client.post(
            "/api/replay-lab/datasets/synthesize", headers=H, json={"skill_id": "demo", "session_ids": ["s1"]}
        )
        assert response.status_code == 202, response.text
        rid = response.json()["job_id"]
        services = build_services(
            role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={"replay_llm_factory": lambda *_: Model()}
        )
        pg_env.tx.pool.run(process_once(services, owner_id="synthesis"))
        result = client.get(f"/api/replay-lab/jobs/{rid}/result", headers=H).json()["result"]
        assert result["count"] == 1
        assert result["datasets"][0]["cases"][0]["provenance"]["source_runtime"] == {"type": "fake"}


@pytest.mark.postgres
def test_root_adapter_source_test_is_async_and_never_runs_on_api(pg_env):
    from fastapi.testclient import TestClient

    from teamEvolver.app import create_app
    from teamEvolver.config_runtime import ConfigRepository
    from teamEvolver.tenants.registry import TenantContext

    async def authorizer(request, services):
        request.state.tenant = TenantContext("default", "default")
        request.state.console_user = {"service_root": request.headers.get("x-root") == "yes"}
        request.state.service_root_authenticated = request.headers.get("x-root") == "yes"

    services = build_services(role="api", tx=pg_env.tx, extras={"config_repo": ConfigRepository(pg_env.tx)})
    client = TestClient(create_app(services=services, authorizer=authorizer))
    test = {"file": "example.py", "code": CODE, "query": "hello", "context": {"runtime_type": "fake"}}
    assert client.post("/api/replay-adapter/code/test", json=test).status_code == 403
    response = client.post("/api/replay-adapter/code/test", headers={"x-root": "yes"}, json=test)
    assert response.status_code == 202, response.text
    rid = response.json()["run_id"]
    record = _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).get(conn, "skill", rid))
    assert record["result"] is None
    assert "code" not in str(record["input"])
    worker = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers())
    pg_env.tx.pool.run(process_once(worker, owner_id="root-test"))
    result = _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).get(conn, "skill", rid))
    assert result["result"]["observation"]["response"] == "fixed-v1"
