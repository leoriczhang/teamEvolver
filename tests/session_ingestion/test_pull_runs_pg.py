"""Real pull/analysis tasks in isolated PG schemas; no provider or deployment IO."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from session_ingestion.adapters.repository import AdapterRepository
from session_ingestion.api import create_router
from session_ingestion.handlers import SessionIngestionDeps, register_handlers
from session_ingestion.payloads import SessionPayloads
from session_ingestion.pull_progress import progress
from session_ingestion.pull_runs import PullRunRepository
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.schemas import IngestionTaskPayload
from teamEvolver.runtime import build_services, process_once
from teamEvolver.storage.postgres import PostgresObjectStore
from tests.support.app import H, _login, _pod
from tests.support.ingestion import _StageRunner
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def observed(identifier="upstream", count=1):
    return {"external_id": identifier, "session": {
        "session_id": identifier, "user_id": "actor", "business_time": "2026-11-20T00:00:00Z",
        "source_identity": {"source_instance_id": "source", "upstream_namespace": "project",
                            "identity_verified": True},
        "turns": [{"trace_id": f"trace-{i}", "prompt_text": f"question {i}"} for i in range(count)],
    }}


class Source:
    def __init__(self, items):
        self.items, self.fail_after_first = items, False

    def load(self, **_):
        owner = self

        class Adapter:
            async def iter_sessions(self, filters):
                for item in owner.items:
                    yield copy.deepcopy(item)
                    if owner.fail_after_first:
                        owner.fail_after_first = False
                        raise ConnectionError("scripted interrupted source")

        return Adapter()


def worker(env, items, *, runner=None):
    payloads = SessionPayloads(env.tx)
    source = Source(items)
    deps = SessionIngestionDeps(payloads=payloads, stage_runner=runner or _StageRunner(), adapter_source=source)
    return build_services(role="worker", tx=env.tx, handlers=register_handlers(),
                          extras={"session_ingestion": deps, "session_payloads": payloads}), source


def accept(env, services, run, *, tenant="default", tracked=True):
    async def seed(conn):
        adapters = AdapterRepository(env.tx)
        revision = await adapters.save_source(conn, tenant_id=tenant, adapter_id="source.py", source="scripted")
        payload = IngestionTaskPayload(
            schema_version="session-ingestion.v1", source="pull", ingestion_id=run, tenant_id=tenant,
            external_id="", user_id="", account_id="", adapter_id="source.py", adapter_revision=revision,
            payload_ref=f"ingest/{tenant}/{run}.json", payload_hash="hash", pull={"filters": {}},
        )
        await PostgresObjectStore(env.tx, tenant).put(conn, payload.payload_ref, json.dumps({
            "payload": payload.to_json(), "session": {"_pull": payload.pull},
        }).encode())
        await SessionIngestionRepository(env.tx).accept(
            conn, tenant_id=tenant, ingestion_id=run, source="pull", task_id=f"{run}:session.pull",
            adapter_id=payload.adapter_id, adapter_revision=revision, payload_ref=payload.payload_ref,
        )
        if tracked:
            await PullRunRepository(env.tx).start(conn, tenant_id=tenant, run_id=run)
        await services.tasks.enqueue(conn, tenant_id=tenant, task_id=f"{run}:session.pull", kind="session.pull",
                                     subject_type="ingestion", subject_id=run, payload_ref=payload.payload_ref)
    _run(env, tenant, seed)


def step(env, services, *, kinds=None, tenant="default"):
    return env.tx.pool.run(process_once(services, owner_id="worker", tenants=(tenant,), kinds=kinds,
                                         max_attempts=1, rand=lambda: 0))


def drain(env, services):
    for _ in range(60):
        if step(env, services) is None:
            return
    raise AssertionError("scripted tasks did not drain")


def view(env, services, run, *, tenant="default", **page):
    async def read(conn):
        job = await services.tasks.get(conn, task_id=f"{run}:session.pull")
        return await progress(services, conn, job=job, run_id=run, **page)
    return _run(env, tenant, read)


def set_task(env, identifier, status, *, tenant="default"):
    _run(env, tenant, lambda conn: conn.execute(
        f"UPDATE {env.schema}.worker_tasks SET status=$2,error_code=$3 WHERE task_id=$1",
        identifier, status, "scripted_failure" if status == "failed" else "",
    ))


def test_pull_complete_is_not_archive_and_repull_reuses_exact_ingestion(pg_env):
    services, source = worker(pg_env, [observed()])
    accept(pg_env, services, "first")
    initial = view(pg_env, services, "first")
    assert initial["tracking_available"] and initial["status"] == "running"
    assert not initial["complete"] and initial["members"] == []
    step(pg_env, services, kinds=["session.pull"])
    pending = view(pg_env, services, "first")
    assert pending["pull_complete"] and not pending["analysis_complete"]
    assert pending["summary"]["pending"] == pending["summary"]["leaf"]["pending"] == 1
    assert pending["counts"] == {"fetched": 1, "enqueued": 1, "duplicate": 0, "empty": 0, "error": 0}
    drain(pg_env, services)
    done = view(pg_env, services, "first")
    assert done["status"] == "completed" and done["complete"]
    assert done["summary"]["archived"] == 1 and done["members"][0]["session_ids"]
    # An unrelated active batch must never extend the first batch's watch.
    source.items = [observed("unrelated-session")]
    accept(pg_env, services, "unrelated")
    step(pg_env, services, kinds=["session.pull"])
    assert view(pg_env, services, "unrelated")["summary"]["pending"] == 1
    source.items = [observed()]
    accept(pg_env, services, "second")
    step(pg_env, services, kinds=["session.pull"])
    reused = view(pg_env, services, "second")
    assert reused["status"] == "completed"
    assert reused["counts"]["duplicate"] == 1 and reused["counts"]["enqueued"] == 0
    assert reused["members"][0]["ingestion_id"] == done["members"][0]["ingestion_id"]
    assert _run(pg_env, "default", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.sessions")) == 1


def test_retry_keeps_first_snapshot_and_original_enqueued_count(pg_env):
    services, source = worker(pg_env, [observed()])
    source.fail_after_first = True
    accept(pg_env, services, "interrupted")
    step(pg_env, services, kinds=["session.pull"])
    failed = view(pg_env, services, "interrupted")
    assert failed["status"] == "failed" and failed["counts"]["enqueued"] == 1
    snapshot = failed["members"][0]["snapshot_revision"]
    source.items = [observed(count=2)]  # source grew after this run had already accepted its first snapshot
    _run(pg_env, "default", lambda c: services.tasks.requeue(c, task_id="interrupted:session.pull"))
    step(pg_env, services, kinds=["session.pull"])
    drain(pg_env, services)
    done = view(pg_env, services, "interrupted")
    assert done["status"] == "completed"
    assert done["counts"]["fetched"] == done["counts"]["enqueued"] == 1
    assert done["members"][0]["snapshot_revision"] == snapshot
    assert _run(pg_env, "default", lambda c: c.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.session_source_snapshots")) == 1


def test_split_parent_skipped_waits_for_every_leaf_and_retry_status_is_authoritative(pg_env):
    segments = [{"session_id": f"part{i}", "turns": [{"trace_id": f"trace-{i}"}],
                 "metadata": {"split_from_session": "upstream"}} for i in (1, 2)]
    services, _ = worker(pg_env, [observed(count=2)], runner=_StageRunner(split_segments=segments))
    accept(pg_env, services, "split")
    step(pg_env, services, kinds=["session.pull"])
    step(pg_env, services, kinds=["session.normalize"])
    step(pg_env, services, kinds=["session.split"])
    pending = view(pg_env, services, "split")
    assert pending["status"] == "running" and pending["summary"]["leaf"]["pending"] == 2
    root = pending["members"][0]["ingestion_id"]
    assert pending["members"][0]["descendants"][0]["status"] == "expanded"
    step(pg_env, services, kinds=["session.normalize"])
    step(pg_env, services, kinds=["session.split"])
    child_job = f"{root}:split:001:session.analyze"
    set_task(pg_env, child_job, "failed")
    assert view(pg_env, services, "split")["status"] == "failed"
    set_task(pg_env, child_job, "retry_wait")
    assert view(pg_env, services, "split")["status"] == "running"
    set_task(pg_env, child_job, "cancelled")
    cancelled = view(pg_env, services, "split")
    assert cancelled["status"] == "cancelled" and not cancelled["complete"]
    set_task(pg_env, child_job, "queued")
    drain(pg_env, services)
    done = view(pg_env, services, "split")
    assert done["status"] == "completed" and done["summary"]["leaf"]["archived"] == 2
    # Other workers may finish children while the parent's fan-out checkpoint
    # is still pending/retrying. Finished leaves cannot confirm that fan-out.
    _run(pg_env, "default", lambda c: c.execute(
        f"UPDATE {pg_env.schema}.session_stage_results SET status='pending' "
        "WHERE ingestion_id=$1 AND stage='split'", root,
    ))
    assert view(pg_env, services, "split")["status"] == "running"


def test_fetch_error_and_empty_are_visible_and_reused_failed_snapshot_does_not_auto_retry(pg_env):
    services, source = worker(pg_env, [observed(), {"external_id": "bad", "status": "error"},
                                          {"external_id": "nothing", "session": None}],
                              runner=_StageRunner(fail_on="analyze"))
    accept(pg_env, services, "partial")
    drain(pg_env, services)
    failed = view(pg_env, services, "partial")
    assert failed["status"] == "failed" and failed["complete"]
    assert failed["summary"]["error"] == failed["summary"]["empty"] == failed["summary"]["failed"] == 1
    source.items = [observed()]
    accept(pg_env, services, "reuse-failure")
    step(pg_env, services, kinds=["session.pull"])
    again = view(pg_env, services, "reuse-failure")
    assert again["status"] == "failed" and again["counts"]["duplicate"] == 1
    assert again["members"][0]["error_code"] == "retries_exhausted"


def test_concurrent_runs_share_snapshot_but_keep_both_memberships(pg_env):
    services, _ = worker(pg_env, [observed()])
    for run in ("one", "two"):
        accept(pg_env, services, run)

    async def compete():
        return await asyncio.gather(*(process_once(services, owner_id=owner, tenants=("default",),
                                                  kinds=["session.pull"]) for owner in ("left", "right")))
    pg_env.tx.pool.run(compete())
    views = [view(pg_env, services, run) for run in ("one", "two")]
    assert sorted(v["counts"]["enqueued"] for v in views) == [0, 1]
    assert views[0]["members"][0]["ingestion_id"] == views[1]["members"][0]["ingestion_id"]
    assert _run(pg_env, "default", lambda c: c.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.session_pull_members")) == 2
    assert _run(pg_env, "alpha", lambda c: c.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.session_pull_members")) == 0


def test_progress_paginates_members_but_aggregates_the_whole_batch_and_old_runs_are_unknown(pg_env):
    services, _ = worker(pg_env, [])
    accept(pg_env, services, "many")
    async def seed(conn):
        runs = PullRunRepository(pg_env.tx)
        for i in range(205):
            await runs.record(conn, tenant_id="default", run_id="many", external_id=f"item-{i:03}",
                              acceptance="empty")
    _run(pg_env, "default", seed)
    set_task(pg_env, "many:session.pull", "succeeded")
    page = view(pg_env, services, "many", limit=3, offset=201)
    assert page["status"] == "completed" and len(page["members"]) == 3 and page["has_more"]
    assert page["counts"]["empty"] == page["summary"]["total"] == 205
    assert page["members"][0]["external_id"] == "item-201"
    accept(pg_env, services, "old", tracked=False)
    set_task(pg_env, "old:session.pull", "succeeded")
    old = view(pg_env, services, "old")
    assert not old["tracking_available"] and old["status"] == "incomplete" and not old["complete"]


def test_progress_api_is_tenant_scoped_and_preserves_old_counts_endpoint(pg_env, tmp_path, monkeypatch):
    services, _ = worker(pg_env, [observed()])
    accept(pg_env, services, "visible")
    drain(pg_env, services)
    async def bind(conn):
        await AdapterRepository(pg_env.tx).bind(conn, tenant_id="default", binding_key="datasource",
                                               adapter_id="source.py", revision=1)
    _run(pg_env, "default", bind)
    client = _pod(pg_env, tmp_path, monkeypatch, pod="pull-progress")
    api_services = client.app.state.services
    api_services.extras["session_payloads"] = SessionPayloads(pg_env.tx)
    client.app.include_router(create_router(api_services))
    with client:
        _login(client, bootstrap=True)
        path = "/api/datasource/jobs/visible:session.pull"
        response = client.get(path + "/progress", headers=H)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert client.get(path + "/result", headers=H).json()["result"] == response.json()["counts"]
        assert client.get(path + "/progress?limit=201", headers=H).status_code == 400
        assert client.get(path + "/progress", headers={"X-Tenant-ID": "alpha"}).status_code == 404
        assert "prompt_text" not in response.text and "payload_ref" not in response.text
        body = {"filters": {"session_id": "new-upstream"}}
        accepted = client.post("/api/datasource/pull", json=body, headers={**H, "Idempotency-Key": "request"})
        assert accepted.status_code == 202, accepted.text
        ack = accepted.json()
        queued = client.get(f"/api/datasource/jobs/{ack['job_id']}/progress", headers=H)
        assert queued.json()["tracking_available"] and queued.json()["status"] == "running"
        repeated = client.post("/api/datasource/pull", json=body, headers={**H, "Idempotency-Key": "request"})
        assert repeated.json()["job_id"] == ack["job_id"] and repeated.json()["dedupe"]
        assert _run(pg_env, "default", lambda c: c.fetchval(
            f"SELECT count(*) FROM {pg_env.schema}.session_pull_runs")) == 2


def test_only_ingestion_tasks_control_progress_and_committed_archive_wins_confirmation_failure(pg_env):
    services, _ = worker(pg_env, [observed()])
    accept(pg_env, services, "scope")
    step(pg_env, services, kinds=["session.pull"])
    pending = view(pg_env, services, "scope")
    ingestion = pending["members"][0]["ingestion_id"]
    async def unrelated(conn):
        await services.tasks.enqueue(conn, tenant_id="default", task_id="not-an-ingestion", kind="session.archive",
                                     subject_type="candidate", subject_id=ingestion)
        await conn.execute(f"UPDATE {pg_env.schema}.worker_tasks SET status='failed' WHERE task_id=$1",
                           "not-an-ingestion")
    _run(pg_env, "default", unrelated)
    assert view(pg_env, services, "scope")["status"] == "running"
    drain(pg_env, services)
    set_task(pg_env, f"{ingestion}:session.archive", "failed")
    done = view(pg_env, services, "scope")
    assert done["status"] == "completed" and done["summary"]["archived"] == 1
