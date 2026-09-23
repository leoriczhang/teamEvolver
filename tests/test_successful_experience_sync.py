import asyncio
import json
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from team_skills.library.experience_sync import (
    LOCK,
    ROOT,
    ExperienceSync,
    OVWriter,
    Settings,
    SyncError,
    Target,
    canonical,
    digest,
    documents,
    resource_directory,
)
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.experience_sync import SyncRuntime, install
from teamEvolver.storage import InMemoryObjectStore
from teamEvolver.tenants.registry import TenantContext


class Store(InMemoryObjectStore):
    def __init__(self, tenant="a"):
        super().__init__()
        self.tenant_id = tenant
        self.times = {}
        self.now = 1_800_000_000.0
        self.locked = False

    def stamp(self):
        return datetime.fromtimestamp(self.now, timezone.utc).isoformat()

    def put_object(self, key, data):
        super().put_object(key, data)
        self.times[key] = self.stamp()

    def database_time(self):
        return self.stamp()

    def changed_objects_page(self, *, pattern, after_time, after_key, until, limit):
        rows = [{"key": k, "content": self.get_object(k).read(), "updated_at": at}
                for k, at in self.times.items() if re.search(pattern, k)
                and (at, k) > (after_time, after_key) and at <= until]
        return sorted(rows, key=lambda r: (r["updated_at"], r["key"]))[:limit]

    def object_page(self, *, prefix, after_key="", limit=100):
        return [{"key": o.key, "content": self.get_object(o.key).read()}
                for o in sorted(self.iter_objects(prefix=prefix), key=lambda o: o.key)
                if o.key > after_key][:limit]

    def try_background_lock(self, namespace):
        assert namespace == LOCK
        if self.locked:
            return False
        self.locked = True
        return True

    def check_background_lock(self, namespace):
        assert self.locked and namespace == LOCK

    def release_background_lock(self, namespace):
        self.locked = False

    def batch_write(self, objects, *, preconditions):
        for key, condition in preconditions.items():
            if condition["base_hash"] != "sha256:" + digest(self.get_object(key).read()):
                raise RuntimeError("PRECONDITION_FAILED")
        for key, content in objects.items():
            self.put_object(key, content)


class Remote:
    def __init__(self):
        self.content = {}
        self.writes = 0
        self.mode = "ok"
        self.before_return = None

    def matches(self, uri, content_digest):
        return digest(self.content.get(uri, b"")) == content_digest

    def write(self, uri, content):
        self.writes += 1
        self.content[uri] = content
        if self.mode == "lost_response":
            raise SyncError("OV_TIMEOUT")
        if self.before_return:
            self.before_return()
        return {"content_written": True, "semantic_status": "complete",
                "vector_status": "complete" if self.mode == "ok" else "failed"}


def source(description="复用成功方法", **extra):
    return {"skill_name": "review", "experiences": [
        {"id": "e1", "kind": "exemplary", "experience_key": "verify", "description": description, **extra},
        {"id": "bad", "kind": "defect", "experience_key": "bad", "description": "do not export"},
    ]}


def worker(store=None, remote=None, **settings):
    store, remote = store or Store(), remote or Remote()
    target = Target("http://ov", "account-a", "team", "test-key")
    return ExperienceSync(store, target, Settings(enabled=True, **settings), remote, clock=lambda: store.now)


def test_backfill_counts_only_changes_new_content_and_removal():
    w = worker()
    key = "tenant-prefix/experience_library/review.json"
    w.store.put_object(key, canonical(source()))
    for excluded in ["experience_library/sessions/s.json", "experience_library/.marker.json", "other/review.json"]:
        w.store.put_object(excluded, canonical(source()))
    w.run_once()
    assert w.writer.writes == 1
    uri = next(iter(w.writer.content))
    assert uri.startswith(ROOT + "/")
    assert not uri.startswith(ROOT + "/a/")
    for counter in range(3):
        w.store.now += 1
        w.store.put_object(key, canonical(source(occurrence_count=counter, latest_score=counter,
                                               user_aliases=["private"], session_ids=[str(counter)],
                                               last_observed_at=str(counter))))
        w.run_once()
    assert w.writer.writes == 1
    doc = json.loads(w.writer.content[uri])
    assert "session_ids" not in doc and "occurrence_count" not in doc
    w.store.put_object(key, canonical(source("new content")))
    w.run_once()
    assert w.writer.writes == 2 and list(w.writer.content) == [uri]
    w.store.put_object(key, canonical({"skill_name": "review", "experiences": []}))
    w.run_once()
    assert uri in w.writer.content and w.writer.writes == 2
    assert w.status()["counts"] == {"pending": 0, "synced": 1, "retry": 0}


def test_pagination_equal_timestamps_and_individual_addition():
    w = worker(batch_size=2)
    for n in range(5):
        w.store.put_object(f"experience_library/{n}.json", canonical(source()))
    for _ in range(5):
        w.run_once()
    assert len(w.writer.content) == 5 and w.writer.writes == 5
    content = source()
    content["experiences"].append({**content["experiences"][0], "id": "e2", "experience_key": "new"})
    w.store.put_object("experience_library/1.json", canonical(content))
    for _ in range(5):
        w.run_once()
    assert len(w.writer.content) == 6 and w.writer.writes == 6


def test_lost_response_restarts_and_index_failure_is_not_success():
    w = worker()
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.writer.mode = "lost_response"
    w.run_once()
    assert w.status()["counts"]["retry"] == 1
    writes = w.writer.writes
    w.run_once()
    assert w.writer.writes == writes  # backoff survives the pass
    w.store.now += 10
    w = worker(w.store, w.writer)
    w.writer.mode = "index_failed"
    w.run_once()
    state = json.loads(w.store.object_page(prefix=w.items)[0]["content"])
    assert state["content_written"] is True and state["state"] == "retry"
    assert state["last_error"] == "OV_INDEX_NOT_READY"
    w.store.now += 30
    w.writer.mode = "ok"
    w.run_once()
    assert w.status()["counts"]["synced"] == 1


def test_changed_desired_during_write_cannot_be_acknowledged():
    w = worker()
    source_key = "experience_library/a.json"
    w.store.put_object(source_key, canonical(source()))

    def concurrent_change():
        w.store.put_object(source_key, canonical(source("new desired")))
        w.discover({"key": source_key, "content": canonical(source("new desired"))})

    w.writer.before_return = concurrent_change
    with pytest.raises(RuntimeError, match="PRECONDITION_FAILED"):
        w.run_once()
    assert not w.store.locked
    state = json.loads(w.store.object_page(prefix=w.items)[0]["content"])
    assert state["state"] == "pending" and state["document"]["description"] == "new desired"
    w.writer.before_return = None
    w.run_once()
    assert w.status()["counts"]["synced"] == 1
    assert json.loads(next(iter(w.writer.content.values())))["description"] == "new desired"


def test_disabled_and_cross_replica_lock_no_upload():
    w = worker()
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.settings = Settings(enabled=False)
    w.run_once()
    w.settings = Settings(enabled=True)
    w.store.locked = True
    w.run_once()
    assert not w.writer.content


def test_daily_scan_repairs_late_commit_older_than_watermark():
    w = worker(full_scan_interval_seconds=60)
    w.run_once()
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.store.times["experience_library/a.json"] = "2020-01-01T00:00:00+00:00"
    w.run_once()
    assert not w.writer.content
    w.store.now += 61
    w.run_once()
    assert w.writer.writes == 1


def test_daily_readback_does_not_rewrite_unless_remote_content_drifted():
    w = worker(full_scan_interval_seconds=60)
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.run_once()
    w.store.now += 61
    w.run_once()
    assert w.writer.writes == 1
    uri = next(iter(w.writer.content))
    w.writer.content[uri] = canonical(source("late outdated request"))
    w.store.now += 61
    w.run_once()
    assert w.writer.writes == 2
    assert json.loads(w.writer.content[uri])["description"] == "复用成功方法"


def test_stop_mid_source_leaves_cursor_for_restart(monkeypatch):
    w = worker()
    content = source()
    content["experiences"].append({**content["experiences"][0], "id": "e2"})
    w.store.put_object("experience_library/a.json", canonical(content))
    original = w.save

    def save(key, value):
        original(key, value)
        if key.startswith(w.items):
            w.stop.set()

    monkeypatch.setattr(w, "save", save)
    w.run_once()
    assert w.load(w.meta_key)["scan"]["after_key"] == ""
    restored = worker(w.store, w.writer)
    restored.run_once()
    assert restored.status()["counts"]["synced"] == 2


def test_invalid_document_recovers_without_blocking_valid_source():
    w = worker()
    w.store.put_object("experience_library/a.json", b"not JSON")
    w.store.put_object("experience_library/b.json", canonical(source()))
    w.run_once()
    assert w.status()["last_error"] == "INVALID_EXPERIENCE_DOCUMENT"
    assert w.writer.writes == 1
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.run_once()
    assert w.status()["last_error"] is None
    assert w.writer.writes == 2


def test_one_remote_failure_does_not_block_other_experiences():
    w = worker()
    data = source()
    data["experiences"].append({**data["experiences"][0], "id": "e2"})
    w.store.put_object("experience_library/a.json", canonical(data))
    original = w.writer.write

    def write(uri, body):
        if uri.endswith("/e1.json"):
            raise SyncError("FORBIDDEN", 403)
        return original(uri, body)

    w.writer.write = write
    w.run_once()
    assert w.status()["counts"] == {"synced": 1, "retry": 1, "pending": 0}


def test_discovery_failure_does_not_advance_watermark(monkeypatch):
    w = worker()
    w.store.put_object("experience_library/a.json", canonical(source()))
    original = w.store.put_object

    def fail(key, body):
        if key.startswith(w.items):
            raise RuntimeError("PG unavailable")
        original(key, body)

    monkeypatch.setattr(w.store, "put_object", fail)
    with pytest.raises(RuntimeError):
        w.run_once()
    assert w.load(w.meta_key)["scan"]["after_key"] == ""
    monkeypatch.setattr(w.store, "put_object", original)
    w.run_once()
    assert w.writer.writes == 1


def test_flat_destination_shares_uri_but_keeps_tenant_and_account_state_separate():
    remote = Remote()
    a, b = worker(Store("a"), remote), worker(Store("b"), remote)
    for w in (a, b):
        w.store.put_object("experience_library/a.json", canonical(source()))
        w.run_once()
    assert len(remote.content) == 1  # Same account/source/ID now deliberately share the destination.
    assert a.status()["counts"]["synced"] == b.status()["counts"]["synced"] == 1
    assert a.store is not b.store
    target = replace(a.target, account="account-b")
    c = ExperienceSync(a.store, target, a.settings, Remote(), clock=lambda: a.store.now)
    c.run_once()
    assert c.writer.writes == 1 and c.items != a.items


def test_configurable_directory_routes_uploads_and_resets_checkpoint():
    config = TeamEvolverConfig(storage_pg_enabled=True, sharing_enabled=True,
                              sharing_viking_api_key="key", sharing_viking_account="acme",
                              experience_sync={"enabled": True, "target_directory": "viking://resources/custom/"})
    settings, target = Settings.from_config(config), Target.from_config(config)
    assert settings.target_directory == target.target_directory == "viking://resources/custom"
    w = worker()
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.run_once()
    assert all(uri.startswith(ROOT + "/") and not uri.startswith(ROOT + "/a/") for uri in w.writer.content)
    moved = ExperienceSync(w.store, replace(w.target, target_directory=target.target_directory),
                           settings, w.writer, clock=lambda: w.store.now)
    assert moved.base != w.base
    moved.run_once()
    assert w.writer.writes == 2 and len(w.writer.content) == 2  # old file remains
    assert moved.status()["target_directory"] == "viking://resources/custom"
    moved.run_once()
    assert w.writer.writes == 2
    assert replace(target, target_directory="viking://resources/custom/").identity == target.identity


@pytest.mark.parametrize("value", ["", None, "viking://resources", "viking://resources/",
                                  "viking://user/private", "http://resources/path", "viking://resources/a/../b",
                                  "viking://resources/a//b", "viking://resources/%2e%2e/b",
                                  "viking://resources/a?x=1", "viking://resources/a#x", "viking://resources/a\\b"])
def test_invalid_target_directories_are_rejected(value):
    with pytest.raises(SyncError, match="INVALID_SYNC_TARGET_DIRECTORY"):
        resource_directory(value)


def test_canonical_newlines_ids_and_conflicting_duplicates():
    a = list(documents("experience_library/a.json", canonical(source(" hi\r\nthere "))))
    b = list(documents("experience_library/a.json", canonical(source("hi\nthere"))))
    assert a == b
    doc = source()
    del doc["experiences"][0]["id"]
    assert list(documents("experience_library/a.json", canonical(doc))) == list(
        documents("experience_library/a.json", canonical(doc)))
    doc["experiences"].append({**doc["experiences"][0], "description": "conflict"})
    with pytest.raises(SyncError, match="INVALID_EXPERIENCE_DOCUMENT"):
        list(documents("experience_library/a.json", canonical(doc)))


@pytest.mark.parametrize("status,code", [(401, "UNAUTHENTICATED"), (403, "FORBIDDEN"),
                                         (503, "UNAVAILABLE"), (404, "MISSING_ROUTE")])
def test_http_errors_do_not_fallback_create(status, code):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("mkdir"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(status, json={"error": {"code": code, "message": "secret business body"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    writer = OVWriter(Target("http://ov", "acme", "team", "key"), client)
    with pytest.raises(SyncError) as error:
        writer.write(ROOT + "/a/x/e.json", b"{}")
    assert "secret" not in str(error.value)
    writes = [r for r in requests if r.url.path.endswith("write")]
    assert len(writes) == 1 and json.loads(writes[0].content)["mode"] == "replace"
    assert writes[0].headers["X-OpenViking-Account"] == "acme"


def test_native_write_contract_create_race_and_readback():
    modes = []
    uri = ROOT + "/a/x/e.json"

    def handler(request):
        if request.url.path.endswith("stat"):
            target_uri = request.url.params["uri"]
            return httpx.Response(200, json={"status": "ok", "result": {
                "uri": target_uri, "isDir": target_uri != uri}})
        if request.url.path.endswith("download"):
            return httpx.Response(200, content=b'{"kind":"exemplary"}')
        if request.url.path.endswith("mkdir"):
            return httpx.Response(409, json={"error": {"code": "ALREADY_EXISTS"}})
        body = json.loads(request.content)
        assert body["wait"] is True and body["timeout"] == 25
        modes.append(body["mode"])
        if len(modes) == 1:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if len(modes) == 2:
            return httpx.Response(409, json={"error": {"code": "ALREADY_EXISTS"}})
        return httpx.Response(200, json={"status": "ok", "result": {
            "uri": uri, "content_updated": True, "semantic_status": "complete", "vector_status": "complete"}})

    writer = OVWriter(Target("http://ov", "a", "team", "key"), httpx.Client(transport=httpx.MockTransport(handler)))
    assert writer.write(uri, b"{}")["vector_status"] == "complete"
    assert modes == ["replace", "create", "replace"]
    assert writer.matches(uri, digest(canonical({"kind": "exemplary"})))


def test_timeout_and_unknown_success_response():
    for mode in ["timeout", "unknown"]:
        def handler(request):
            if request.url.path.endswith("mkdir"):
                return httpx.Response(200)
            if mode == "timeout":
                raise httpx.ReadTimeout("body must not leak")
            return httpx.Response(200, json={"result": {}})
        writer = OVWriter(Target("http://ov", "a", "team", "key"), httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(SyncError, match="OV_TIMEOUT|OV_UNCONFIRMED_WRITE"):
            writer.write(ROOT + "/a/x/e.json", b"{}")


def test_status_admin_guard_and_no_body_leak():
    app = FastAPI()
    owner = SimpleNamespace(config=TeamEvolverConfig())
    registry = SimpleNamespace(get=lambda tid: TenantContext(tenant_id=tid))

    def guard(user):
        if (user or {}).get("role") != "admin":
            raise HTTPException(403)

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.console_user = {"role": request.headers.get("test-role", "user")}
        return await call_next(request)

    install(app, owner, lambda _: registry, guard)
    with TestClient(app) as client:
        assert client.get("/api/experience-sync/status").status_code == 403
        response = client.get("/api/experience-sync/status", headers={"test-role": "admin"})
        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert "document" not in response.text and "api_key" not in response.text


@pytest.mark.asyncio
async def test_runtime_bounds_parallelism_and_drains_on_stop(monkeypatch):
    config = TeamEvolverConfig(storage_pg_enabled=True, experience_sync={"enabled": True})
    contexts = [TenantContext(tenant_id=str(i)) for i in range(5)]
    registry = SimpleNamespace(list_tenants=lambda: contexts)
    runtime = SyncRuntime(SimpleNamespace(config=config), lambda _: registry)
    entered = []
    lock = threading.Lock()

    def run(ctx):
        with lock:
            entered.append(ctx.tenant_id)
        runtime.stop_event.wait(3)
        return 30

    monkeypatch.setattr(runtime, "run_tenant", run)
    runtime.start()
    for _ in range(100):
        if len(entered) == 2:
            break
        await asyncio.sleep(.01)
    assert len(entered) == 2
    await runtime.stop()
    assert len(entered) == 2


@pytest.mark.asyncio
async def test_invalid_tenant_config_does_not_stop_other_tenants(monkeypatch):
    config = TeamEvolverConfig(storage_pg_enabled=True, experience_sync={"enabled": True})
    contexts = [TenantContext(tenant_id="bad", config_overrides={"experience_sync": {"enabled": "bad"}}),
                TenantContext(tenant_id="good")]
    registry = SimpleNamespace(list_tenants=lambda: contexts)
    runtime = SyncRuntime(SimpleNamespace(config=config), lambda _: registry)
    entered = []
    monkeypatch.setattr(runtime, "run_tenant", lambda ctx: entered.append(ctx.tenant_id) or 30)
    runtime.start()
    for _ in range(100):
        if entered:
            break
        await asyncio.sleep(.01)
    await runtime.stop()
    assert entered == ["good"]
    assert runtime.failures["bad"][0] == "INVALID_SYNC_CONFIG"


def test_configuration_templates_and_validation():
    from pathlib import Path

    import yaml

    from teamEvolver.config_store import ConfigStore

    for env, enabled in [("sit", True), ("prd", False)]:
        path = Path(f"config/config_{env}.yaml")
        data = yaml.safe_load(path.read_text())
        assert data["experience_sync"]["enabled"] is enabled
        assert Settings.from_config(ConfigStore(config_file=path).to_config()).enabled is enabled
        assert data["experience_sync"]["target_directory"] == ROOT
    with pytest.raises(SyncError, match="INVALID_SYNC_CONFIG"):
        Settings.from_config(SimpleNamespace(experience_sync={"enabled": "false"}))
    with pytest.raises(SyncError, match="PG_REQUIRED"):
        Target.from_config(TeamEvolverConfig())


def test_manual_run_is_durable_coalesced_and_resumes_paginated_full_scan():
    w = worker(batch_size=2)
    for n in range(5):
        w.store.put_object(f"experience_library/{n}.json", canonical(source()))
    receipt = w.request_run()
    assert receipt["state"] == "queued" and w.writer.writes == 0
    assert w.request_run()["request_id"] == receipt["request_id"]
    w.run_once()
    assert w.status()["manual"]["state"] == "running"
    restored = worker(w.store, w.writer, batch_size=2)
    for _ in range(15):
        if not restored.run_once():
            break
    else:
        pytest.fail("manual pass did not finish")
    assert restored.status()["manual"]["state"] == "completed"
    assert restored.status()["counts"] == {"pending": 0, "synced": 5, "retry": 0}
    assert restored.writer.writes == 5
    # Full manual readback must not turn count-only edits into uploads.
    w.store.put_object("experience_library/0.json", canonical(source(occurrence_count=999)))
    assert restored.request_run()["request_id"] != receipt["request_id"]
    for _ in range(15):
        if not restored.run_once():
            break
    assert restored.writer.writes == 5


def test_manual_full_scan_finds_old_rows_and_preserves_retry_backoff():
    w = worker()
    w.run_once()
    w.store.put_object("experience_library/old.json", canonical(source()))
    w.store.times["experience_library/old.json"] = "2020-01-01T00:00:00+00:00"
    w.run_once()
    assert w.writer.writes == 0
    w.writer.mode = "index_failed"
    w.request_run()
    w.run_once()
    assert w.writer.writes == 1
    assert w.status()["manual"]["state"] == "completed"
    assert w.status()["counts"]["retry"] == 1
    w.request_run()
    w.run_once()
    assert w.writer.writes == 1  # Manual trigger does not bypass retry backoff.
    assert w.status()["last_error"] == "OV_INDEX_NOT_READY"


def test_manual_disabled_or_locked_does_not_persist_or_write():
    w = worker()
    w.settings = replace(w.settings, enabled=False)
    with pytest.raises(SyncError, match="SYNC_DISABLED"):
        w.request_run()
    w.settings = replace(w.settings, enabled=True)
    w.store.locked = True
    with pytest.raises(SyncError, match="SYNC_ALREADY_RUNNING"):
        w.request_run()
    assert w.load(w.meta_key) == {} and w.writer.writes == 0


@pytest.mark.parametrize("route", ["trigger", "import"])
def test_manual_trigger_http_auth_tenant_and_safe_failures(monkeypatch, route):
    import teamEvolver.proxy.experience_sync as module

    app = FastAPI()
    config = TeamEvolverConfig(experience_sync={"enabled": True}, storage_pg_enabled=True,
                              sharing_enabled=True, sharing_viking_deployment="local",
                              sharing_viking_endpoint="http://ov", sharing_viking_account="a",
                              sharing_viking_user="team", sharing_viking_api_key="private-key")
    ctx = TenantContext(tenant_id="authenticated")
    owner = SimpleNamespace(config=config)
    registry = SimpleNamespace(get=lambda tid: ctx if tid == ctx.tenant_id else None)
    store = Store(ctx.tenant_id)
    monkeypatch.setattr(module, "build_store", lambda cfg, tenant: store if tenant == ctx.tenant_id else None)
    monkeypatch.setattr(module, "current_tenant_id", lambda: ctx.tenant_id)
    monkeypatch.setattr(SyncRuntime, "start", lambda self: None)

    async def stop(self):
        pass

    monkeypatch.setattr(SyncRuntime, "stop", stop)

    def guard(user):
        if (user or {}).get("role") != "admin":
            raise HTTPException(403)

    @app.middleware("http")
    async def auth(request, call_next):
        request.state.console_user = {"role": request.headers.get("test-role", "user")}
        return await call_next(request)

    install(app, owner, lambda _: registry, guard)
    runtime = app.state.experience_sync
    with TestClient(app) as client:
        headers = {"test-role": "admin"}
        url = f"/api/experience-sync/{route}"
        assert client.post(url).status_code == 403
        assert client.post(url, headers=headers).json()["detail"] == "SYNC_NOT_RUNNING"
        runtime.task = SimpleNamespace(done=lambda: False)
        response = client.post(url, headers=headers, json={"tenant_id": "victim", "account": "victim"})
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["tenant_id"] == "authenticated"
        assert receipt["manual"]["state"] == "queued"
        assert client.post(url, headers=headers).json()["manual"] == receipt["manual"]
        assert "private-key" not in response.text
        assert runtime.wake.is_set()
        store.locked = True
        busy = client.post(url, headers=headers)
        assert busy.status_code == 409 and busy.json()["detail"] == "SYNC_ALREADY_RUNNING"
        store.locked = False
        config.experience_sync = {"enabled": False}
        assert client.post(url, headers=headers).json()["detail"] == "SYNC_DISABLED"
        ctx = replace(ctx, status="disabled")
        assert client.post(url, headers=headers).status_code == 403
        runtime.task = None


@pytest.mark.asyncio
async def test_manual_wakes_scheduler_without_new_upload_executor(monkeypatch):
    config = TeamEvolverConfig(storage_pg_enabled=True, experience_sync={"enabled": True})
    ctx = TenantContext(tenant_id="a")
    registry = SimpleNamespace(list_tenants=lambda: [ctx])
    runtime = SyncRuntime(SimpleNamespace(config=config), lambda _: registry)
    calls = []
    monkeypatch.setattr(runtime, "run_tenant", lambda ctx: calls.append(ctx.tenant_id) or 3600)
    monkeypatch.setattr(runtime, "register_run", lambda ctx, operation: {"request_id": "test", "state": "queued"})
    runtime.start()
    try:
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(.01)
        assert calls == ["a"]
        await runtime.trigger(ctx)
        for _ in range(200):
            if len(calls) == 2:
                break
            await asyncio.sleep(.01)
        assert calls == ["a", "a"]
        runtime.requested = {str(i) for i in range(32)}
        with pytest.raises(SyncError, match="SYNC_BUSY"):
            await runtime.trigger(ctx)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_disconnected_trigger_keeps_registration_bounded_and_wakes_after_persistence(monkeypatch):
    config = TeamEvolverConfig(storage_pg_enabled=True, experience_sync={"enabled": True})
    ctx = TenantContext(tenant_id="a")
    runtime = SyncRuntime(SimpleNamespace(config=config), lambda _: None)
    runtime.task = SimpleNamespace(done=lambda: False)
    entered, release = threading.Event(), threading.Event()

    def register(ctx, operation):
        entered.set()
        assert release.wait(3)
        return {"request_id": "durable", "state": "queued"}

    monkeypatch.setattr(runtime, "register_run", register)
    request = asyncio.create_task(runtime.trigger(ctx))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        assert "a" not in runtime.requested  # Don't wake the worker before persistence.
        request.cancel()
        await asyncio.sleep(.01)
        assert not request.done()
        with pytest.raises(SyncError, match="SYNC_BUSY"):
            await runtime.trigger(ctx)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert runtime.requested == {"a"}
    assert runtime.registering == set()
    assert runtime.wake.is_set()


@pytest.mark.parametrize("code", ["CONFLICT", "ALREADY_EXISTS"])
def test_existing_directory_conflict_is_confirmed_and_cached_per_writer(code):
    calls = []
    uri = ROOT + "/tenant/source/e.json"

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("mkdir"):
            return httpx.Response(409, json={"error": {"code": code}})
        if request.url.path.endswith("stat"):
            return httpx.Response(200, json={"status": "ok", "result": {
                "uri": request.url.params["uri"], "isDir": True}})
        return httpx.Response(200, json={"status": "ok", "result": {
            "uri": uri, "content_updated": True, "semantic_status": "complete", "vector_status": "complete"}})

    writer = OVWriter(Target("http://ov", "a", "team", "key"), httpx.Client(transport=httpx.MockTransport(handler)))
    writer.write(uri, b"{}")
    directories = len([r for r in calls if r.url.path.endswith("mkdir")])
    assert directories > 0 and len([r for r in calls if r.url.path.endswith("stat")]) == directories
    writer.write(uri, b"{}")
    assert len([r for r in calls if r.url.path.endswith("mkdir")]) == directories
    assert len([r for r in calls if r.url.path.endswith("write")]) == 2


@pytest.mark.parametrize("mode,expected", [
    ("file", "OV_PATH_TYPE_CONFLICT"), ("missing", "NOT_FOUND"), ("denied", "FORBIDDEN"),
    ("timeout", "OV_TIMEOUT"), ("wrong_uri", "OV_INVALID_STAT"), ("bad_response", "OV_INVALID_STAT"),
])
def test_directory_conflict_does_not_hide_missing_permissions_or_wrong_type(mode, expected):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("mkdir"):
            return httpx.Response(409, json={"error": {"code": "CONFLICT"}})
        assert request.url.path.endswith("stat")
        if mode == "missing":
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if mode == "denied":
            return httpx.Response(403, json={"error": {"code": "FORBIDDEN"}})
        if mode == "timeout":
            raise httpx.ReadTimeout("secret should not leak")
        return httpx.Response(200, json={"status": "ok", "result": {
            "uri": "wrong" if mode == "wrong_uri" else request.url.params["uri"],
            "isDir": "true" if mode == "bad_response" else mode != "file"}})

    writer = OVWriter(Target("http://ov", "a", "team", "key"), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SyncError, match=expected):
        writer.write(ROOT + "/a/x/e.json", b"{}")
    assert not any(path.endswith("write") for path in calls)
    assert writer.directories == set()


@pytest.mark.parametrize("directory", [False, True])
def test_create_conflict_checks_file_before_one_replace_retry(directory):
    uri, modes = ROOT + "/a/x/e.json", []

    def handler(request):
        if request.url.path.endswith("mkdir"):
            return httpx.Response(200)
        if request.url.path.endswith("stat"):
            return httpx.Response(200, json={"status": "ok", "result": {"uri": uri, "isDir": directory}})
        body = json.loads(request.content)
        modes.append(body["mode"])
        return httpx.Response(404 if len(modes) == 1 else 409, json={"error": {
            "code": "NOT_FOUND" if len(modes) == 1 else "CONFLICT"}})

    writer = OVWriter(Target("http://ov", "a", "team", "key"), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SyncError, match="OV_PATH_TYPE_CONFLICT" if directory else "CONFLICT"):
        writer.write(uri, b"{}")
    assert modes == (["replace", "create"] if directory else ["replace", "create", "replace"])


def test_index_pending_visibility_distinguishes_written_content():
    w = worker()
    w.writer.mode = "index_failed"
    w.store.put_object("experience_library/a.json", canonical(source()))
    w.run_once()
    assert w.status()["index_pending"] == 1
    w.writer.mode = "ok"
    w.store.now += 60
    w.run_once()
    assert w.status()["index_pending"] == 0


def test_deferred_retry_reports_heartbeat_without_resetting_backoff(caplog):
    w = worker()
    w.writer.mode = "lost_response"
    w.store.put_object("experience_library/review.json", canonical(source()))
    w.run_once()
    retry = w.status()["retry"]
    assert retry["deferred"] == 1 and retry["due"] == 0
    assert retry["next_attempt_at"] == w.store.now + 5
    # Re-discovery of the same content must not bypass durable retry timing.
    w.request_run()
    with caplog.at_level("INFO", logger="team_skills.library.experience_sync"):
        w.run_once()
        w.run_once()
    assert w.writer.writes == 1
    status = w.status()
    assert status["last_delivery_pass"]["attempted"] == 0
    assert status["last_delivery_pass"]["deferred"] == 1
    assert status["retry"]["last_attempt_at"] == w.store.now
    reports = [r for r in caplog.records if r.msg == "experience_sync.delivery_pass"]
    assert len(reports) == 1
    w.store.now += 60
    w.writer.mode = "ok"
    assert w.status()["retry"]["due"] == 1
    w.run_once()
    assert w.status()["counts"]["synced"] == 1
    assert w.status()["retry"]["samples"] == []


def test_persisted_failure_reports_exact_ov_stage_without_response_body():
    def handle(request):
        if request.url.path.endswith("mkdir"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(409, headers={"x-request-id": "ov-123"}, json={
            "error": {"code": "CONFLICT", "message": "SECRET remote content"}})
    remote = OVWriter(Target("http://ov", "a", "team", "SECRET-key"),
                      client=httpx.Client(transport=httpx.MockTransport(handle)))
    w = worker(remote=remote)
    w.store.put_object("experience_library/review.json", canonical(source()))
    try:
        w.run_once()
        status = w.status()
        failure = status["retry"]["samples"][0]["failure"]
        assert failure["stage"] == "write"
        assert failure["path"] == "/api/v1/content/write"
        assert failure["status"] == 409 and failure["ov_request_id"] == "ov-123"
        assert status["retry"]["error_counts"] == {"CONFLICT": 1}
        assert status["last_error_at"] == w.store.now
        assert "SECRET" not in json.dumps(status)
    finally:
        remote.close()


def test_legacy_retry_status_is_bounded_and_retains_unknown_timestamps():
    w = worker()
    for i in range(8):
        doc = source()["experiences"][0]
        doc = {**doc, "id": str(i)}
        [(path, document)] = documents("experience_library/review.json", canonical({
            "skill_name": "review", "experiences": [doc]}))
        assert w.store.try_background_lock(LOCK)
        w.enqueue(path, document)
        value = w.load(w.items + path)
        value.update(state="retry", last_error="CONFLICT", next_attempt=w.store.now + 900, attempts=10)
        w.save(w.items + path, value)
        w.store.release_background_lock(LOCK)
    status = w.status()
    assert status["retry"]["deferred"] == 8
    assert len(status["retry"]["samples"]) == 5
    assert status["retry"]["last_attempt_at"] is None
    assert status["retry"]["samples"][0]["failure"] is None
    w.store.locked = True
    assert w.run_once() is False
    assert w.outcome == "lock_busy"


@pytest.mark.parametrize("read_status", [200, 404, 403])
def test_write_timeout_reconciles_same_uri_without_claiming_indexes_complete(read_status):
    content = {}
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith("mkdir"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("write"):
            payload = json.loads(request.content)
            content[payload["uri"]] = payload["content"]
            return httpx.Response(504, headers={"x-request-id": "ov-timeout"},
                                  json={"error": {"code": "DEADLINE_EXCEEDED"}})
        assert request.url.params["uri"] in content
        if read_status == 200:
            return httpx.Response(200, text=content[request.url.params["uri"]])
        return httpx.Response(read_status, json={"error": {
            "code": "NOT_FOUND" if read_status == 404 else "FORBIDDEN"}})

    remote = OVWriter(Target("http://ov", "a", "team", "key"),
                      client=httpx.Client(transport=httpx.MockTransport(handle)))
    w = worker(remote=remote)
    w.store.put_object("experience_library/review.json", canonical(source()))
    try:
        w.run_once()
        status = w.status()
        assert status["counts"] == {"pending": 0, "synced": 0, "retry": 1}
        assert status["index_pending"] == (1 if read_status == 200 else 0)
        failure = status["retry"]["samples"][0]["failure"]
        assert failure["stage"] == "write" and failure["status"] == 504
        assert failure["ov_request_id"] == "ov-timeout"
        if read_status == 403:
            assert failure["reconciliation_error"] == "FORBIDDEN"
        else:
            assert failure["content_confirmed"] is (read_status == 200)
        assert paths[-1] == "/api/v1/content/download"
    finally:
        remote.close()


@pytest.mark.parametrize("old_state", ["synced", "retry"])
def test_legacy_tenant_directory_migrates_existing_records_once_without_deleting(old_state):
    w = worker()
    w.store.put_object("experience_library/review.json", canonical(source()))
    w.run_once()
    [(key, old)] = [(o.key, w.load(o.key)) for o in w.store.iter_objects(prefix=w.items)]
    new_uri = old["uri"]
    old_uri = new_uri.replace(ROOT + "/", ROOT + "/a/", 1)
    legacy = {**old, "uri": old_uri, "state": old_state, "attempts": 10,
              "next_attempt": w.store.now + 900, "last_error": "CONFLICT" if old_state == "retry" else None}
    w.writer.content[old_uri] = w.writer.content.pop(new_uri)
    w.store.put_object(key, canonical(legacy))
    doc_before = canonical(legacy["document"])
    w.run_once()
    updated = w.load(key)
    assert updated["uri"] == new_uri and updated["state"] == "synced"
    assert updated["previous_destination"]["uri"] == old_uri
    assert updated["previous_destination"]["attempts"] == 10
    assert canonical(updated["document"]) == doc_before
    assert old_uri in w.writer.content and new_uri in w.writer.content
    writes = w.writer.writes
    w.run_once()
    assert w.writer.writes == writes
    assert w.status()["target_directory"] == ROOT


def test_new_destination_keeps_tenant_mutex_and_stable_document_ids():
    w = worker()
    w.store.put_object("experience_library/review.json", canonical(source()))
    w.store.locked = True
    assert not w.run_once() and w.writer.writes == 0
    w.store.locked = False
    w.run_once()
    uri = next(iter(w.writer.content))
    assert uri == ROOT + "/" + digest(b"experience_library/review.json") + "/e1.json"
