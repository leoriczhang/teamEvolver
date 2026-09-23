"""Dataset snapshots, tenant isolation, exports and bounded replay lifecycle."""
import asyncio
import copy
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_replay.datasets.batch import BatchCapacityError, DatasetBatchRunner, replay_snapshot
from team_replay.datasets.collections import DatasetCollectionStore, DatasetConflict, DatasetNotFound, snapshot_item
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.dataset_routes import register_dataset_routes
from teamEvolver.session_store import SessionStore
from teamEvolver.storage.local import LocalObjectStore
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant


def sample(sid="s1"):
    return {"session_id": sid, "title": "原任务", "runtime": {"type": "test"},
            "timestamp": "2026-09-15T12:00:00Z", "ingested_at": "2026-09-16T12:00:00Z",
            "meta": {"trace_id": "business-trace"},
            "turns": [{"turn_num": 1, "prompt_text": "生成报告", "response_text": "旧结果"}]}


def collection(tmp_path, count=2):
    store = DatasetCollectionStore(LocalObjectStore(tmp_path))
    items = [snapshot_item(sample(f"s{i}"), {}) for i in range(count)]
    dataset = store.create("测试 / 数据集", "说明", items, {"kind": "sessions"})
    return store, dataset


def test_snapshot_export_and_edits_do_not_mutate_source(tmp_path):
    source = sample()
    item = snapshot_item(source, {})
    store = DatasetCollectionStore(LocalObjectStore(tmp_path))
    dataset = store.create("集合", "说明", [item], {})
    source["turns"][0]["prompt_text"] = "changed"
    store.change_item(dataset["dataset_id"], item["item_id"], {"query": "新的 Query", "requirements": ["新要求"]})
    stored = store.load_document(dataset["dataset_id"])
    assert stored["schema_version"] == "team-replay.dataset.v2"
    assert "session_snapshot" not in stored["cases"][0]["provenance"]
    with zipfile.ZipFile(store.export_zip(dataset["dataset_id"])) as archive:
        case = json.loads(archive.read("cases.jsonl"))
        saved = json.loads(archive.read(f"snapshots/{item['item_id']}.json"))
        assert case["query"] == "新的 Query"
        assert case["checks"] == [{"id": "R01", "kind": "output", "text": "新要求"}]
        assert saved["turns"][0]["prompt_text"] == "生成报告"
        assert case["provenance"]["timestamp"] != case["provenance"]["ingested_at"]
        assert case["provenance"]["trace_id"] == "business-trace"
        assert "session_snapshot" not in case["provenance"]
        assert case["provenance"]["snapshot_ref"] == f"snapshots/{item['item_id']}.json"
        exported = json.loads(archive.read("dataset.json"))
        assert exported["schema_version"] == "team-replay.dataset.v2"
        assert exported["metadata"]["item_count"] == 1
    # New store instance reads the same durable snapshot.
    assert DatasetCollectionStore(LocalObjectStore(tmp_path)).metadata(dataset["dataset_id"])["name"] == "集合"


def test_missing_and_invalid_selection_never_writes_partial_dataset(tmp_path):
    store, dataset = collection(tmp_path)
    with pytest.raises(ValueError):
        store.load("../other")
    with pytest.raises(DatasetNotFound):
        store.load("ds_missing")
    with pytest.raises(ValueError):
        store.create("", "", [], {})
    with pytest.raises(ValueError):
        store.change_item(dataset["dataset_id"], store.load(dataset["dataset_id"])["items"][0]["item_id"],
                          {"query": "Query", "requirements": [""]})
    assert len(store.list()) == 1


def test_active_batch_freezes_input_and_recovery_preserves_completed_results(tmp_path):
    store, dataset = collection(tmp_path)
    did = dataset["dataset_id"]
    run, items = store.create_run(did, {"concurrency": 1}, "old-process")
    with pytest.raises(DatasetConflict):
        store.delete(did)
    with pytest.raises(DatasetConflict):
        store.update(did, {"name": "renamed"})
    with pytest.raises(DatasetConflict):
        store.create_run(did, {}, "other")
    store.item_running(did, run["run_id"], items[0]["item_id"])
    store.finish_item(did, run["run_id"], items[0]["item_id"], {"ok": True, "completed": False})
    store.recover("new-process")
    recovered = store.run(did, run["run_id"])
    assert recovered["status"] == "interrupted"
    assert recovered["completed"] == recovered["failed"] == 1
    assert recovered["items"][0]["success"] is False
    assert recovered["items"][1]["success"] is None
    assert recovered["items"][1]["status"] == "interrupted"
    store.change_item(did, items[0]["item_id"], None)
    frozen = store.read(store.run_key(did, run["run_id"], "input.json"))
    assert frozen["schema_version"] == "team-replay.dataset.v2"
    assert len(frozen["cases"]) == 2
    historical = store.present_document(did, frozen, run_id=run["run_id"])
    assert historical["items"][0]["session"]["session_id"] == "s0"


def test_bounded_batch_keeps_per_item_failure_and_tenant_context(tmp_path):
    async def exercise():
        store, dataset = collection(tmp_path)
        seen = []

        def evaluate(item, options):
            from teamEvolver.tenants.registry import current_tenant_id
            seen.append(current_tenant_id())
            if item["session_id"] == "s0":
                raise RuntimeError("one item failed")
            return {"ok": True, "completed": True, "request_id": "replay-test"}

        runner = DatasetBatchRunner(evaluate)
        runner.reserve()
        runner.reserve()
        with pytest.raises(BatchCapacityError):
            runner.reserve()
        runner.release()
        token = set_current_tenant(TenantContext(tenant_id="tenant-a"))
        try:
            run, items = store.create_run(dataset["dataset_id"], {"concurrency": 2}, runner.owner_id)
            await runner.execute(store, run, items)
        finally:
            reset_current_tenant(token)
            runner.stop()
        result = store.run(dataset["dataset_id"], run["run_id"])
        assert result["status"] == "completed"
        assert result["completed"] == 2 and result["failed"] == result["succeeded"] == 1
        assert result["items"][0]["success"] is None
        assert set(seen) == {"tenant-a"}
        assert runner.active == 0
    asyncio.run(exercise())


def test_cancel_stops_pending_items_after_current_item(tmp_path):
    async def exercise():
        store, dataset = collection(tmp_path, 3)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def evaluate(item, options):
            calls.append(item["item_id"])
            entered.set()
            assert release.wait(5)
            return {"ok": True, "completed": True}

        runner = DatasetBatchRunner(evaluate)
        runner.reserve()
        run, items = store.create_run(dataset["dataset_id"], {"concurrency": 1}, runner.owner_id)
        task = asyncio.create_task(runner.execute(store, run, items))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            store.request_cancel(dataset["dataset_id"], run["run_id"])
            release.set()
            await task
        finally:
            release.set()
            runner.stop()
        result = store.run(dataset["dataset_id"], run["run_id"])
        assert len(calls) == 1
        assert result["status"] == "cancelled" and result["completed"] == 1
        assert [row["status"] for row in result["items"]] == ["completed", "cancelled", "cancelled"]
    asyncio.run(exercise())


def test_replay_uses_saved_query_and_real_factory_contract(monkeypatch):
    from team_replay import execution
    from team_replay.hooks import AgentObservation
    from team_replay.host import configure_host, current_host
    original = current_host(required=False)
    sent, contexts, closed = [], [], []

    class Session:
        def send(self, message):
            sent.append(message)
            return AgentObservation("报告已生成", metrics={"total_tokens": 12})
        def close(self):
            closed.append(True)

    class Factory:
        def open(self, context):
            contexts.append(context)
            return Session()

    host = SimpleNamespace(resolve_replay_factory=lambda *args: Factory(), judge_harness=lambda: {})
    configure_host(host)
    run_branch = execution.run_branch
    monkeypatch.setattr(execution, "run_branch", lambda *a, **kw: run_branch(
        *a, **kw, judge=lambda **_: {"judge": "model", "all_satisfied": True},
    ))
    try:
        item = snapshot_item(sample(), {})
        before = copy.deepcopy(item)
        result = replay_snapshot(item, {"timeout_seconds": 30, "max_interactions": 1})
        assert result["ok"] and result["completed"]
        assert sent == ["生成报告"] and closed == [True]
        assert contexts[0].treatment.skill is None
        assert item == before
    finally:
        configure_host(original)


@pytest.fixture
def dataset_api(tmp_path, monkeypatch):
    from teamEvolver.tenants.registry import current_tenant_id
    buckets = {}

    def sessions(config, tenant_id="default"):
        tid = current_tenant_id()
        bucket = buckets.setdefault(tid, LocalObjectStore(tmp_path / tid))
        return SessionStore(bucket)

    monkeypatch.setattr(SessionStore, "from_config", sessions)
    config = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"))
    owner = SimpleNamespace(config=config, _safe_create_task=lambda coro: asyncio.create_task(coro))
    app = FastAPI()

    @app.middleware("http")
    async def tenant(request, call_next):
        token = set_current_tenant(TenantContext(tenant_id=request.headers.get("x-test-tenant", "a")))
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token)

    register_dataset_routes(owner, app)
    token = set_current_tenant(TenantContext(tenant_id="a"))
    try:
        bucket = sessions(config)._bucket
        source = sample()
        bucket.put_object("session_archive/s1.json", json.dumps(source).encode())
    finally:
        reset_current_tenant(token)
    with TestClient(app) as client:
        yield client, owner, buckets
    owner._dataset_batch_runner.stop()


def test_routes_crud_isolation_zip_and_validation(dataset_api):
    client, _, buckets = dataset_api
    response = client.post("/api/datasets", json={"name": "集合", "session_ids": ["s1"]})
    assert response.status_code == 201, response.text
    did = response.json()["dataset_id"]
    detail = client.get(f"/api/datasets/{did}").json()
    assert detail["item_count"] == 1 and "session" not in detail["items"][0]
    assert client.get(f"/api/datasets/{did}", headers={"x-test-tenant": "b"}).status_code == 404
    assert client.get("/api/datasets", headers={"x-test-tenant": "b"}).json()["total"] == 0
    assert client.post("/api/datasets", json={"name": "other", "session_ids": ["s1"]},
                       headers={"x-test-tenant": "b"}).status_code == 404
    assert client.patch(f"/api/datasets/{did}", json={"name": "新集合"}).status_code == 200
    assert client.get(f"/api/datasets/{did}?search=no-match").json()["total"] == 0
    assert client.post("/api/datasets", json={"name": "invalid"}).status_code == 400
    assert client.post(f"/api/datasets/{did}/runs", json={"concurrency": 3}).status_code == 422
    assert client.post("/api/datasets", json={"name": "invalid", "session_ids": ["../../other"]}).status_code == 400
    buckets["a"].delete_object("session_archive/s1.json")
    exported = client.get(f"/api/datasets/{did}/export")
    assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
    assert "filename*=UTF-8''" in exported.headers["content-disposition"]
    assert client.delete(f"/api/datasets/{did}").json()["deleted"] is True
    assert client.get(f"/api/datasets/{did}").status_code == 404


def test_routes_accept_batch_and_preserve_historical_source_after_removal(dataset_api):
    import time

    client, owner, _ = dataset_api
    owner._dataset_batch_runner.evaluate = lambda item, options: {
        "ok": True, "completed": True, "request_id": "replay-isolated-test", "final_response": item["query"],
    }
    dataset = client.post("/api/datasets", json={"name": "批次集合", "session_ids": ["s1"]}).json()
    path = f"/api/datasets/{dataset['dataset_id']}"
    response = client.post(f"{path}/runs", json={"concurrency": 1})
    assert response.status_code == 202, response.text
    rid = response.json()["run_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        run = client.get(f"{path}/runs/{rid}").json()
        if run["status"] == "completed":
            break
        time.sleep(0.01)
    assert run["status"] == "completed" and run["succeeded"] == 1
    iid = run["items"][0]["item_id"]
    assert client.get(f"{path}/runs/{rid}/results/{iid}").json()["final_response"] == "生成报告"
    assert client.delete(f"{path}/items/{iid}").status_code == 200
    assert client.get(f"{path}/items/{iid}").status_code == 404
    historical = client.get(f"{path}/runs/{rid}/items/{iid}")
    assert historical.status_code == 200 and historical.json()["query"] == "生成报告"
    assert client.get(f"{path}/runs/{rid}", headers={"x-test-tenant": "b"}).status_code == 404
