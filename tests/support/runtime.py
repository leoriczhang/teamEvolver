"""Controllable-clock, tenant-scoped runtime SQL double."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from teamEvolver.runtime import (
    build_services,
)

_OP_RE = re.compile(r"--\s*te:op=([a-z_]+)")

_ACTIVE_FOR_RESULT = {"running", "reconciling", "waiting_external"}

_TRANSITIONS = {
    "succeed": _ACTIVE_FOR_RESULT,
    "fail": _ACTIVE_FOR_RESULT,
    "mark_cancelled": _ACTIVE_FOR_RESULT,
    "retry": {"running", "reconciling", "waiting_external"},
    "wait_external": {"running"},
    "reconcile": {"waiting_external", "running"},
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
class _Lease:
    tenant_id: str
    resource_key: str
    owner_id: str = ""
    fence: int = 0
    lease_until: float | None = None


@dataclass
class _DB:
    clock: _Clock
    tasks: dict[tuple[str, str], _Row] = field(default_factory=dict)
    leases: dict[tuple[str, str], _Lease] = field(default_factory=dict)
    usage: dict[tuple[str, str, str], int] = field(default_factory=dict)


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Tenant-scoped fake: statements only touch rows for ``tenant_id`` (RLS)."""

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

    # -- claim candidate selection ---------------------------------------- #

    def _claimable(self, row: _Row, kinds) -> bool:
        if row.tenant_id != self._tenant:
            return False
        if row.available_at > self._now():
            return False
        if kinds is not None and row.kind not in kinds:
            return False
        if row.status in ("queued", "retry_wait"):
            return True
        if row.status in ("running", "reconciling", "waiting_external"):
            return row.lease_until is not None and row.lease_until < self._now()
        return False

    async def fetchrow(self, sql: str, *args):
        op = self._op(sql)
        if op == "enqueue_dedupe_lookup":
            kind, dedupe = args
            for row in self._db.tasks.values():
                if (
                    row.tenant_id == self._tenant
                    and row.kind == kind
                    and row.dedupe_key == dedupe
                    and row.status not in ("succeeded", "failed", "cancelled")
                ):
                    return {"task_id": row.task_id}
            return None
        if op == "claim":
            owner_id, lease_seconds, kinds = args
            candidates = [r for r in self._db.tasks.values() if self._claimable(r, kinds)]
            candidates.sort(key=lambda r: (r.available_at, r.created_at))
            if not candidates:
                return None
            row = candidates[0]
            row.status = "running"
            row.owner_id = owner_id
            row.attempt += 1
            row.fence += 1
            row.lease_until = self._now() + lease_seconds
            return {
                "tenant_id": row.tenant_id,
                "task_id": row.task_id,
                "kind": row.kind,
                "attempt": row.attempt,
                "fence": row.fence,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "payload_ref": row.payload_ref,
                "payload_hash": row.payload_hash,
                "config_revision": row.config_revision,
                "adapter_revision": row.adapter_revision,
                "trace_id": row.trace_id,
                "upstream_task_id": row.upstream_task_id,
                "cancel_requested": row.cancel_requested,
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
        if op == "lease_acquire":
            tenant_id, resource_key, owner_id, lease_seconds = args
            key = (tenant_id, resource_key)
            lease = self._db.leases.get(key)
            if lease is None:
                lease = _Lease(
                    tenant_id=tenant_id,
                    resource_key=resource_key,
                    owner_id=owner_id,
                    fence=1,
                    lease_until=self._now() + lease_seconds,
                )
                self._db.leases[key] = lease
                return {"fence": lease.fence}
            grantable = lease.owner_id == owner_id or lease.lease_until is None or lease.lease_until < self._now()
            if not grantable:
                return None
            lease.owner_id = owner_id
            lease.fence += 1
            lease.lease_until = self._now() + lease_seconds
            return {"fence": lease.fence}
        raise AssertionError(f"unhandled fetchrow op: {op}")

    async def fetchval(self, sql: str, *args):
        op = self._op(sql)
        if op == "heartbeat":
            task_id, owner_id, fence, lease_seconds = args
            row = self._db.tasks.get((self._tenant, task_id))
            if row and row.owner_id == owner_id and row.fence == fence and row.status in _ACTIVE_FOR_RESULT:
                row.lease_until = self._now() + lease_seconds
                return task_id
            return None
        if op in _TRANSITIONS:
            return self._apply_transition(op, args)
        if op == "lease_heartbeat":
            tenant_id, resource_key, owner_id, fence, lease_seconds = args
            lease = self._db.leases.get((tenant_id, resource_key))
            if lease and lease.owner_id == owner_id and lease.fence == fence:
                lease.lease_until = self._now() + lease_seconds
                return lease.fence
            return None
        if op == "lease_release":
            tenant_id, resource_key, owner_id, fence = args
            lease = self._db.leases.get((tenant_id, resource_key))
            if lease and lease.owner_id == owner_id and lease.fence == fence:
                lease.owner_id = ""
                lease.lease_until = None
                return fence
            return None
        raise AssertionError(f"unhandled fetchval op: {op}")

    def _apply_transition(self, op: str, args):
        task_id, owner_id, fence, *extra = args
        row = self._db.tasks.get((self._tenant, task_id))
        if row is None or row.owner_id != owner_id or row.fence != fence or row.status not in _TRANSITIONS[op]:
            return None
        if op == "succeed":
            row.status, row.result_ref, row.error_code = "succeeded", extra[0], ""
        elif op == "fail":
            row.status, row.error_code = "failed", extra[0]
        elif op == "mark_cancelled":
            row.status = "cancelled"
        elif op == "wait_external":
            row.status, row.upstream_task_id = "waiting_external", extra[0]
        elif op == "reconcile":
            row.status = "reconciling"
        elif op == "retry":
            backoff, error_code = extra
            row.status, row.owner_id, row.lease_until = "retry_wait", "", None
            row.error_code = error_code
            row.available_at = self._now() + backoff
        return task_id

    async def execute(self, sql: str, *args):
        op = self._op(sql)
        if op == "enqueue":
            (
                tenant_id,
                task_id,
                kind,
                subject_type,
                subject_id,
                _schema_version,
                dedupe_key,
                payload_ref,
                payload_hash,
                config_revision,
                adapter_revision,
                trace_id,
                available_in_seconds,
            ) = args
            self._db.tasks[(tenant_id, task_id)] = _Row(
                tenant_id=tenant_id,
                task_id=task_id,
                kind=kind,
                subject_type=subject_type,
                subject_id=subject_id,
                dedupe_key=dedupe_key,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                config_revision=config_revision,
                adapter_revision=adapter_revision,
                trace_id=trace_id,
                available_at=self._now() + available_in_seconds,
                created_at=self._now(),
            )
            return "INSERT 0 1"
        if op == "charge_usage":
            tenant_id, metric, window_key, amount = args
            key = (tenant_id, metric, window_key)
            self._db.usage[key] = self._db.usage.get(key, 0) + amount
            return "OK"
        raise AssertionError(f"unhandled execute op: {op}")


class _FakePool:
    schema = "teamevolver"


class _FakeTx:
    """Stand-in for TransactionManager with tenant-scoped connections."""

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


def _services(tx: _FakeTx, handlers: dict):
    return build_services(role="worker", tx=tx, handlers=handlers)


class _FlakyTx(_FakeTx):
    """Connections raise until ``outage`` checkouts have failed (PG unavailable)."""

    def __init__(self, db: _DB, outage: int) -> None:
        super().__init__(db)
        self.outage = outage

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: str):
        if self.outage > 0:
            self.outage -= 1
            raise TimeoutError("database unavailable")
        yield _FakeConn(self._db, tenant_id)
