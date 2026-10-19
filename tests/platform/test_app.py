"""Application composition, liveness, readiness and tenant-scoped task operations."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from teamEvolver.app import create_app
from teamEvolver.runtime import build_services
from tests.support.paths import REPO_ROOT

_OP_RE = re.compile(r"--\s*te:op=([a-z_]+)")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0


@dataclass
class _Row:
    tenant_id: str
    task_id: str
    kind: str
    status: str = "queued"
    attempt: int = 0
    fence: int = 0
    owner_id: str = ""
    lease_until: float | None = None
    available_at: float = 0.0
    cancel_requested: bool = False
    upstream_task_id: str = ""
    result_ref: str = ""
    error_code: str = ""
    subject_type: str = ""
    subject_id: str = ""
    payload_ref: str = ""
    payload_hash: str = ""
    config_revision: int = 0
    adapter_revision: int = 0
    trace_id: str = ""
    dedupe_key: str = ""
    created_at: float = 0.0


@dataclass
class _DB:
    clock: _Clock
    tasks: dict[tuple[str, str], _Row] = field(default_factory=dict)


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, db: _DB, tenant_id: str) -> None:
        self._db = db
        self._tenant = tenant_id

    def transaction(self):
        return _FakeTxn()

    @staticmethod
    def _op(sql: str) -> str:
        m = _OP_RE.search(sql)
        assert m, f"statement missing te:op marker: {sql[:60]!r}"
        return m.group(1)

    def _now(self) -> float:
        return self._db.clock.now

    async def fetchrow(self, sql: str, *args):
        op = self._op(sql)
        if op == "get":
            (task_id,) = args
            row = self._db.tasks.get((self._tenant, task_id))
            if row is None:
                return None
            return {
                "tenant_id": row.tenant_id,
                "task_id": row.task_id,
                "kind": row.kind,
                "status": row.status,
                "attempt": row.attempt,
                "fence": row.fence,
                "owner_id": row.owner_id,
                "available_at": row.available_at,
                "lease_until": row.lease_until,
                "cancel_requested": row.cancel_requested,
                "upstream_task_id": row.upstream_task_id,
                "result_ref": row.result_ref,
                "error_code": row.error_code,
                "created_at": row.created_at,
                "updated_at": self._now(),
            }
        if op == "request_cancel":
            (task_id,) = args
            row = self._db.tasks.get((self._tenant, task_id))
            if row is None or row.status in ("succeeded", "failed", "cancelled"):
                return None
            row.cancel_requested = True
            if row.status in ("queued", "retry_wait"):
                row.status = "cancelled"
            return {"status": row.status}
        if op == "requeue":
            (task_id,) = args
            row = self._db.tasks.get((self._tenant, task_id))
            if row is None or row.status != "failed":
                return None
            row.status = "queued"
            row.owner_id = ""
            row.lease_until = None
            row.cancel_requested = False
            row.error_code = ""
            row.available_at = self._now()
            return {"status": row.status}
        raise AssertionError(f"unhandled fetchrow op: {op}")

    async def execute(self, sql: str, *args):
        op = self._op(sql)
        if op == "enqueue":
            (tenant_id, task_id, kind, *_rest) = args
            self._db.tasks[(tenant_id, task_id)] = _Row(
                tenant_id=tenant_id,
                task_id=task_id,
                kind=kind,
                created_at=self._now(),
            )
            return "INSERT 0 1"
        raise AssertionError(f"unhandled execute op: {op}")


class _FakePool:
    schema = "teamevolver"


class _FakeTx:
    def __init__(self, db: _DB) -> None:
        self._db = db
        self.pool = _FakePool()

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: str):
        yield _FakeConn(self._db, tenant_id)

    @asynccontextmanager
    async def admin_tx(self):
        yield _FakeConn(self._db, "default")


@pytest.fixture
def db() -> _DB:
    return _DB(clock=_Clock())


@pytest.fixture
def tx(db: _DB) -> _FakeTx:
    return _FakeTx(db)


@pytest.fixture
def services(tx: _FakeTx):
    # The api role never registers handlers ; build_services
    # wires the runtime repositories onto the fake transaction manager.
    return build_services(role="api", tx=tx)


@pytest.fixture
def client(services) -> TestClient:
    app = create_app(services=services)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Composition root
# --------------------------------------------------------------------------- #


def _route_pairs(app) -> list[tuple[str, str]]:
    """Flatten (method, path) pairs across the app, including included routers.

    This FastAPI version mounts sub-routers behind a lazy ``_IncludedRouter``
    wrapper rather than flattening on ``include_router``; follow ``original_router``
    so the composed surface is fully visible to the manifest checks.
    """
    pairs: list[tuple[str, str]] = []

    def _walk(routes):
        for route in routes:
            sub = getattr(route, "original_router", None)
            if sub is not None:
                _walk(sub.routes)
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            for method in getattr(route, "methods", None) or set():
                pairs.append((method, path))

    _walk(app.routes)
    return pairs


def test_create_app_builds_without_a_worker(services):
    app = create_app(services=services)
    assert services.runs_worker is False  # api role has no worker loop
    paths = {path for _method, path in _route_pairs(app)}
    for required in ("/healthz", "/readyz", "/status", "/api/logging/status", "/api/jobs/{job_id}"):
        assert required in paths, f"composed app missing platform route {required}"


def test_composed_app_has_no_duplicate_path_method(services):
    app = create_app(services=services)
    seen: set[tuple[str, str]] = set()
    for key in _route_pairs(app):
        assert key not in seen, f"duplicate route {key[0]} {key[1]} in composed app"
        seen.add(key)


def test_app_py_declares_no_business_endpoint():
    """app.py is a composition root only — it must not decorate any route of its
    own ."""
    import ast

    src = REPO_ROOT / "teamEvolver" / "app.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    http_methods = {"get", "post", "put", "delete", "patch", "head", "options"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if dec.func.attr.lower() in http_methods:
                    offenders.append(node.name)
    assert not offenders, f"app.py declares endpoint(s): {offenders}"


# --------------------------------------------------------------------------- #
# Health / observability surface
# --------------------------------------------------------------------------- #


def test_healthz_is_live_and_readyz_reflects_lifecycle(services):
    app = create_app(services=services, lifecycle=None)
    client = TestClient(app)
    assert client.get("/healthz").json()["ok"] is True
    # No lifecycle wired -> not ready yet.
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["ready"] is False


# --------------------------------------------------------------------------- #
# Unified jobs interface
# --------------------------------------------------------------------------- #


def test_get_unknown_job_is_404(client: TestClient):
    assert client.get("/api/jobs/nope").status_code == 404


def test_get_job_returns_public_view(client: TestClient, db: _DB):
    db.tasks[("default", "t1")] = _Row(tenant_id="default", task_id="t1", kind="session.pull")
    body = client.get("/api/jobs/t1").json()
    assert body["job_id"] == "t1" and body["kind"] == "session.pull" and body["status"] == "queued"
    assert "owner_id" not in body and "fence" not in body  # internals not leaked


def test_cancel_queued_job_is_immediate(client: TestClient, db: _DB):
    db.tasks[("default", "t1")] = _Row(tenant_id="default", task_id="t1", kind="session.pull")
    body = client.post("/api/jobs/t1/cancel").json()
    assert body["status"] == "cancelled" and body["cancel_requested"] is True


def test_cancel_running_job_is_cooperative(client: TestClient, db: _DB):
    db.tasks[("default", "t1")] = _Row(
        tenant_id="default",
        task_id="t1",
        kind="session.pull",
        status="running",
        owner_id="w-a",
        fence=1,
        lease_until=2_000_000.0,
    )
    body = client.post("/api/jobs/t1/cancel").json()
    # Running task is not abandoned midway: only the request flag is set.
    assert body["cancel_requested"] is True and body["status"] == "running"


def test_retry_only_allowed_for_failed_job(client: TestClient, db: _DB):
    db.tasks[("default", "ok")] = _Row(tenant_id="default", task_id="ok", kind="session.pull", status="succeeded")
    assert client.post("/api/jobs/ok/retry").status_code == 409

    db.tasks[("default", "bad")] = _Row(
        tenant_id="default",
        task_id="bad",
        kind="session.pull",
        status="failed",
        error_code="boom",
    )
    body = client.post("/api/jobs/bad/retry").json()
    assert body["status"] == "queued"
    assert db.tasks[("default", "bad")].error_code == ""  # cleared on requeue
