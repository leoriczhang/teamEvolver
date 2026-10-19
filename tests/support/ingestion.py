"""Tenant-scoped ingestion SQL, payload and scripted stage doubles."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from session_ingestion.handlers import (
    KIND_NORMALIZE,
    SessionIngestionDeps,
    register_handlers,
)
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.schemas import (
    PAYLOAD_SCHEMA_VERSION,
    IngestionTaskPayload,
    session_payload_hash,
)
from teamEvolver.runtime import WorkerStats, WorkerTaskRepository, build_services, process_once

_OP_RE = re.compile(r"--\s*te:op=([a-z_]+)")


@dataclass
class _Row:
    data: dict[str, Any]


@dataclass
class _DB:
    now: float = 1_000_000.0
    tasks: dict[tuple[str, str], dict] = field(default_factory=dict)
    ingestions: dict[tuple[str, str], dict] = field(default_factory=dict)
    stages: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    sessions: dict[tuple[str, str], dict] = field(default_factory=dict)
    adapters: dict[tuple[str, str], dict] = field(default_factory=dict)
    adapter_versions: dict[tuple[str, str, int], dict] = field(default_factory=dict)
    adapter_bindings: dict[tuple[str, str], dict] = field(default_factory=dict)


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, db: _DB, tenant: str) -> None:
        self._db = db
        self._t = tenant

    def transaction(self):
        return _Txn()

    @staticmethod
    def _op(sql: str) -> str:
        m = _OP_RE.search(sql)
        assert m, f"missing te:op marker: {sql[:70]!r}"
        return m.group(1)

    # ---- fetchrow ---- #
    async def fetchrow(self, sql: str, *a):
        op = self._op(sql)
        t = self._t
        if op == "enqueue_dedupe_lookup":
            kind, dedupe = a
            for row in self._db.tasks.values():
                if (
                    row["tenant_id"] == t
                    and row["kind"] == kind
                    and row["dedupe_key"] == dedupe
                    and row["status"] not in ("succeeded", "failed", "cancelled")
                ):
                    return {"task_id": row["task_id"]}
            return None
        if op == "claim":
            owner_id, lease_seconds, kinds = a
            cands = [
                r
                for r in self._db.tasks.values()
                if r["tenant_id"] == t
                and r["available_at"] <= self._db.now
                and (kinds is None or r["kind"] in kinds)
                and (
                    r["status"] in ("queued", "retry_wait")
                    or (
                        r["status"] in ("running", "reconciling", "waiting_external")
                        and r["lease_until"] is not None
                        and r["lease_until"] < self._db.now
                    )
                )
            ]
            cands.sort(key=lambda r: (r["available_at"], r["created_at"]))
            if not cands:
                return None
            row = cands[0]
            row.update(
                status="running",
                owner_id=owner_id,
                attempt=row["attempt"] + 1,
                fence=row["fence"] + 1,
                lease_until=self._db.now + lease_seconds,
            )
            return {
                k: row[k]
                for k in (
                    "tenant_id",
                    "task_id",
                    "kind",
                    "attempt",
                    "fence",
                    "subject_type",
                    "subject_id",
                    "payload_ref",
                    "payload_hash",
                    "config_revision",
                    "adapter_revision",
                    "trace_id",
                    "upstream_task_id",
                    "cancel_requested",
                )
            }
        if op == "get":
            (task_id,) = a
            row = self._db.tasks.get((t, task_id))
            return dict(row) if row else None
        if op == "ingest_dedupe_lookup":
            (dedupe,) = a
            for row in self._db.ingestions.values():
                if row["tenant_id"] == t and row["dedupe_key"] == dedupe:
                    return {"ingestion_id": row["ingestion_id"], "task_id": row["task_id"]}
            return None
        if op == "ingest_get":
            (iid,) = a
            row = self._db.ingestions.get((t, iid))
            return dict(row) if row else None
        if op == "adapter_head":
            (adapter_id,) = a
            versions = [v for (tt, aid, _rev), v in self._db.adapter_versions.items() if tt == t and aid == adapter_id]
            if not versions:
                return None
            top = max(versions, key=lambda v: v["revision"])
            return {"revision": top["revision"], "source_hash": top["source_hash"]}
        if op == "adapter_version_get":
            adapter_id, rev = a
            row = self._db.adapter_versions.get((t, adapter_id, int(rev)))
            return dict(row) if row else None
        if op == "adapter_binding_get":
            (key,) = a
            row = self._db.adapter_bindings.get((t, key))
            return dict(row) if row else None
        if op == "session_get":
            (sid,) = a
            row = self._db.sessions.get((t, sid))
            return dict(row) if row else None
        raise AssertionError(f"unhandled fetchrow op: {op}")

    # ---- fetch (plural) ---- #
    async def fetch(self, sql: str, *a):
        op = self._op(sql)
        t = self._t
        if op == "stage_list":
            (iid,) = a
            rows = [v for (tt, ii, _s), v in self._db.stages.items() if tt == t and ii == iid]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"])]
        if op == "stage_succeeded":
            (iid,) = a
            return [
                {"stage": v["stage"]}
                for (tt, ii, _s), v in self._db.stages.items()
                if tt == t and ii == iid and v["status"] == "succeeded"
            ]
        if op == "session_list":
            before, limit = a
            rows = [v for (tt, _s), v in self._db.sessions.items() if tt == t]
            if before is not None:
                rows = [r for r in rows if r.get("business_time") and r["business_time"] < before]
            rows.sort(key=lambda r: (r.get("business_time") or "", r["session_id"]), reverse=True)
            return [dict(r) for r in rows[:limit]]
        raise AssertionError(f"unhandled fetch op: {op}")

    # ---- fetchval ---- #
    async def fetchval(self, sql: str, *a):
        op = self._op(sql)
        t = self._t
        if op == "ingest_set_status":
            iid, status = a
            row = self._db.ingestions.get((t, iid))
            if not row:
                return None
            row["status"] = status
            return iid
        if op == "adapter_latest_rev":
            (adapter_id,) = a
            revs = [rev for (tt, aid, rev) in self._db.adapter_versions if tt == t and aid == adapter_id]
            return max(revs) if revs else None
        if op == "adapter_version_insert":
            tenant_id, adapter_id, rev, digest, source = a
            key = (tenant_id, adapter_id, int(rev))
            if key in self._db.adapter_versions:
                return None
            self._db.adapter_versions[key] = {
                "tenant_id": tenant_id,
                "adapter_id": adapter_id,
                "revision": int(rev),
                "source_hash": digest,
                "source": source,
                "created_at": self._db.now,
            }
            return int(rev)
        if op in ("succeed", "fail", "retry", "mark_cancelled", "wait_external", "reconcile"):
            return self._transition(op, a)
        if op == "heartbeat":
            task_id, owner_id, fence, lease = a
            row = self._db.tasks.get((t, task_id))
            if (
                row
                and row["owner_id"] == owner_id
                and row["fence"] == fence
                and row["status"] in ("running", "reconciling", "waiting_external")
            ):
                row["lease_until"] = self._db.now + lease
                return task_id
            return None
        raise AssertionError(f"unhandled fetchval op: {op}")

    def _transition(self, op, a):
        task_id, owner_id, fence, *extra = a
        row = self._db.tasks.get((self._t, task_id))
        active = ("running", "reconciling", "waiting_external")
        if not row or row["owner_id"] != owner_id or row["fence"] != fence:
            return None
        if op in ("succeed", "fail", "mark_cancelled") and row["status"] not in active:
            return None
        if op == "succeed":
            row.update(status="succeeded", result_ref=extra[0], error_code="")
        elif op == "fail":
            row.update(status="failed", error_code=extra[0])
        elif op == "retry":
            if row["status"] not in active:
                return None
            row.update(
                status="retry_wait",
                owner_id="",
                lease_until=None,
                error_code=extra[1],
                available_at=self._db.now + extra[0],
            )
        return task_id

    # ---- execute ---- #
    async def execute(self, sql: str, *a):
        op = self._op(sql)
        if op == "ingest_lock":
            return None
        if op == "enqueue":
            (
                tenant_id,
                task_id,
                kind,
                subject_type,
                subject_id,
                schema_version,
                dedupe_key,
                payload_ref,
                payload_hash,
                config_revision,
                adapter_revision,
                trace_id,
                avail,
            ) = a
            self._db.tasks[(tenant_id, task_id)] = dict(
                tenant_id=tenant_id,
                task_id=task_id,
                kind=kind,
                subject_type=subject_type,
                subject_id=subject_id,
                schema_version=schema_version,
                dedupe_key=dedupe_key,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                config_revision=config_revision,
                adapter_revision=adapter_revision,
                trace_id=trace_id,
                status="queued",
                attempt=0,
                owner_id="",
                fence=0,
                lease_until=None,
                available_at=self._db.now + avail,
                cancel_requested=False,
                upstream_task_id="",
                result_ref="",
                error_code="",
                created_at=self._db.now,
                updated_at=self._db.now,
            )
            return "INSERT 0 1"
        if op == "ingest_accept":
            (
                tenant_id,
                iid,
                source,
                external_id,
                user_id,
                account_id,
                dedupe_key,
                task_id,
                adapter_id,
                adapter_revision,
                config_revision,
                payload_ref,
                payload_hash,
                business_time,
            ) = a
            self._db.ingestions[(tenant_id, iid)] = dict(
                tenant_id=tenant_id,
                ingestion_id=iid,
                source=source,
                external_id=external_id,
                user_id=user_id,
                account_id=account_id,
                dedupe_key=dedupe_key,
                task_id=task_id,
                adapter_id=adapter_id,
                adapter_revision=adapter_revision,
                config_revision=config_revision,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                status="accepted",
                business_time=business_time,
                received_at=self._db.now,
                updated_at=self._db.now,
            )
            return "INSERT 0 1"
        if op == "stage_record":
            import json

            (tenant_id, iid, stage, status, result_ref, detail, error_code) = a
            self._db.stages[(tenant_id, iid, stage)] = dict(
                tenant_id=tenant_id,
                ingestion_id=iid,
                stage=stage,
                status=status,
                result_ref=result_ref,
                detail=json.loads(detail),
                error_code=error_code,
                created_at=self._db.now,
                updated_at=self._db.now,
            )
            return "INSERT 0 1"
        if op == "session_write":
            import json

            (
                tenant_id,
                sid,
                iid,
                source,
                external_id,
                user_id,
                account_id,
                decision,
                content_ref,
                content_hash,
                judge,
                filter_audit,
                consumption,
                meta,
                business_time,
            ) = a
            self._db.sessions[(tenant_id, sid)] = dict(
                tenant_id=tenant_id,
                session_id=sid,
                ingestion_id=iid,
                source=source,
                external_id=external_id,
                user_id=user_id,
                account_id=account_id,
                decision=decision,
                content_ref=content_ref,
                content_hash=content_hash,
                judge=json.loads(judge),
                filter_audit=json.loads(filter_audit),
                consumption=json.loads(consumption),
                meta=json.loads(meta),
                business_time=business_time,
                ingested_at=self._db.now,
            )
            return "INSERT 0 1"
        if op == "adapter_ensure":
            tenant_id, adapter_id, kind = a
            self._db.adapters.setdefault(
                (tenant_id, adapter_id), {"tenant_id": tenant_id, "adapter_id": adapter_id, "kind": kind}
            )
            return "INSERT 0 1"
        if op == "adapter_bind":
            tenant_id, key, adapter_id, rev = a
            self._db.adapter_bindings[(tenant_id, key)] = {
                "tenant_id": tenant_id,
                "binding_key": key,
                "adapter_id": adapter_id,
                "revision": int(rev),
            }
            return "INSERT 0 1"
        raise AssertionError(f"unhandled execute op: {op}")


class _Pool:
    schema = "teamevolver"


class _Tx:
    def __init__(self, db: _DB) -> None:
        self._db = db
        self.pool = _Pool()

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: str):
        yield _Conn(self._db, tenant_id)

    @asynccontextmanager
    async def admin_tx(self):
        yield _Conn(self._db, "default")


class _Payloads:
    """Stands in for the raw-payload ObjectStore (survives a 'restart')."""

    def __init__(self) -> None:
        self.payload_json: dict[str, str] = {}
        self.sessions: dict[str, dict] = {}
        self.stage_outputs: dict[str, dict] = {}

    def put_payload_json(self, ref: str, payload_json: str, session: dict) -> None:
        self.payload_json[ref] = payload_json
        self.sessions[ref] = session

    def get_payload_json(self, ref: str) -> str:
        return self.payload_json[ref]

    def get_session(self, ref: str) -> dict:
        return self.sessions[ref]

    def put_session(self, ingestion_id: str, session: dict, *, stage: str):
        key = f"stage/{ingestion_id}/{stage}.json"
        self.stage_outputs[key] = session
        self.sessions[key] = session
        return key, session_payload_hash(session)


class _StageRunner:
    """Scripted stage work. ``fail_on`` forces a given stage to raise."""

    def __init__(
        self,
        *,
        decision: str = "valuable",
        fail_on: str = "",
        business_time: str = "2026-10-01T12:00:00+00:00",
        split_segments: list[dict] | None = None,
    ) -> None:
        self.decision = decision
        self.fail_on = fail_on
        self.business_time = business_time
        self.split_segments = split_segments

    async def normalize(self, session, *, payload):
        if self.fail_on == "normalize":
            raise RuntimeError("normalize boom")
        return {**session, "normalized": True}

    async def split(self, session, *, payload):
        if self.fail_on == "split":
            raise RuntimeError("split boom")
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        if metadata.get("split_from_session") or self.split_segments is None:
            return [session]
        return self.split_segments

    async def analyze(self, session, *, payload):
        if self.fail_on == "analyze":
            raise RuntimeError("analyze boom")
        return {"decision": self.decision}

    async def trace_analyze(self, session, *, payload):
        if self.fail_on == "trace_analyze":
            raise RuntimeError("trace boom")
        return None

    async def archive(self, session, *, payload):
        if self.fail_on == "archive":
            raise RuntimeError("archive boom")
        return {
            "decision": self.decision,
            "session": session,
            "judge": {"overall_score": 8},
            "business_time": self.business_time,
            "meta": {"source": payload.source},
        }


class _AdapterSource:
    def __init__(self, items):
        self._items = items

    def load(self, *, adapter_id, revision, source):
        items = self._items

        class _Adapter:
            async def iter_sessions(self, pull):
                for it in items:
                    yield it

        return _Adapter()


@pytest.fixture
def db() -> _DB:
    return _DB()


@pytest.fixture
def tx(db: _DB) -> _Tx:
    return _Tx(db)


@pytest.fixture
def payloads() -> _Payloads:
    return _Payloads()


def _worker_services(tx, payloads, *, runner=None, adapter_source=None):
    deps = SessionIngestionDeps(
        payloads=payloads,
        stage_runner=runner or _StageRunner(),
        adapter_source=adapter_source,
    )
    return build_services(role="worker", tx=tx, handlers=register_handlers(), extras={"session_ingestion": deps})


async def _accept_push(
    tx, payloads, *, tenant="default", external_id="s1", business_time="2026-10-01T12:00:00+00:00", config_revision=1
):
    """Mimic the router's atomic acceptance for a push session."""
    repo = SessionIngestionRepository(tx)
    tasks = WorkerTaskRepository(tx)
    iid = f"ing-{external_id}"
    session = {"session_id": external_id, "turns": [{"prompt_text": "hi"}], "business_time": business_time}
    payload = IngestionTaskPayload(
        schema_version=PAYLOAD_SCHEMA_VERSION,
        source="push",
        ingestion_id=iid,
        tenant_id=tenant,
        external_id=external_id,
        user_id="u1",
        account_id="a1",
        config_revision=config_revision,
        payload_ref=f"ingest/{iid}.json",
        payload_hash=session_payload_hash(session),
    )
    payloads.put_payload_json(payload.payload_ref, payload.to_json(), session)
    task_id = f"{iid}:{KIND_NORMALIZE}"
    async with tx.tenant_tx(tenant) as conn:
        await repo.accept(
            conn,
            tenant_id=tenant,
            ingestion_id=iid,
            source="push",
            external_id=external_id,
            user_id="u1",
            account_id="a1",
            dedupe_key=f"push:{external_id}",
            task_id=task_id,
            config_revision=config_revision,
            payload_ref=payload.payload_ref,
            payload_hash=payload.payload_hash,
            business_time=business_time,
        )
        await tasks.enqueue(
            conn,
            tenant_id=tenant,
            task_id=task_id,
            kind=KIND_NORMALIZE,
            subject_type="ingestion",
            subject_id=iid,
            dedupe_key=task_id,
            payload_ref=payload.payload_ref,
            payload_hash=payload.payload_hash,
            config_revision=config_revision,
        )
    return iid, task_id


async def _drain(services, *, tenant="default", limit=50):
    """Run the worker loop to quiescence, advancing the fake clock past retry
    backoffs so a retrying task reaches its terminal state deterministically."""
    stats = WorkerStats()
    db = services.tx._db  # noqa: SLF001 - test fake access
    for _ in range(limit):
        claimed = await process_once(services, owner_id="w", tenants=(tenant,), stats=stats, rand=lambda: 0.0)
        if claimed is None:
            # Nothing ready now; jump past any retry backoff and try once more.
            db.now += 1000.0
            claimed = await process_once(services, owner_id="w", tenants=(tenant,), stats=stats, rand=lambda: 0.0)
            if claimed is None:
                break
    return stats
