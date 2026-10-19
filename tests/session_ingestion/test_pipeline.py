"""test pipeline."""

from __future__ import annotations

import pytest

from session_ingestion.adapters.repository import AdapterRepository
from session_ingestion.handlers import (
    KIND_ANALYZE,
    KIND_ARCHIVE,
    KIND_NORMALIZE,
    KIND_PULL,
    KIND_SPLIT,
)
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.schemas import (
    PAYLOAD_SCHEMA_VERSION,
    IngestionTaskPayload,
    session_payload_hash,
)
from teamEvolver.runtime import WorkerTaskRepository, process_once
from tests.support.ingestion import _accept_push, _AdapterSource, _drain, _StageRunner, _worker_services


@pytest.mark.asyncio
async def test_202_then_restart_completes_and_persists_business_and_ingest_time(tx, db, payloads):
    # API accepted (202) and enqueued; now a *fresh* services container runs.
    iid, _ = await _accept_push(tx, payloads, business_time="2026-10-01T12:00:00+00:00")
    services = _worker_services(tx, payloads)  # built after acceptance == "restart"
    await _drain(services)

    repo = SessionIngestionRepository(tx)
    async with tx.tenant_tx("default") as conn:
        done = await repo.succeeded_stages(conn, ingestion_id=iid)
        ingestion = await repo.get_ingestion(conn, ingestion_id=iid)
    assert done == {"normalize", "split", "analyze", "trace_analyze", "archive"}
    assert ingestion["status"] == "archived"
    # Formal Session carries BOTH the business occurrence time and the ingest time.
    session = db.sessions[("default", f"session_{iid}")]
    assert session["business_time"].isoformat() == "2026-10-01T12:00:00+00:00"
    assert session["ingested_at"] is not None  # DB-clock ingest time stamped on write
    assert session["decision"] == "valuable"


@pytest.mark.asyncio
async def test_mandatory_analysis_failure_produces_no_formal_session(tx, db, payloads):
    iid, _ = await _accept_push(tx, payloads)
    services = _worker_services(tx, payloads, runner=_StageRunner(fail_on="analyze"))
    await _drain(services)

    # No formal Session, no archive stage: a failed mandatory stage yields nothing.
    assert ("default", f"session_{iid}") not in db.sessions
    repo = SessionIngestionRepository(tx)
    async with tx.tenant_tx("default") as conn:
        done = await repo.succeeded_stages(conn, ingestion_id=iid)
    assert "archive" not in done and "analyze" not in done
    # The analyze task exhausted its retries and failed (no silent pass).
    assert db.tasks[("default", f"{iid}:{KIND_ANALYZE}")]["status"] == "failed"


@pytest.mark.asyncio
async def test_semantic_split_failure_produces_no_formal_session(tx, db, payloads):
    iid, _ = await _accept_push(tx, payloads)
    services = _worker_services(tx, payloads, runner=_StageRunner(fail_on="split"))

    await _drain(services)

    assert ("default", f"session_{iid}") not in db.sessions
    repo = SessionIngestionRepository(tx)
    async with tx.tenant_tx("default") as conn:
        done = await repo.succeeded_stages(conn, ingestion_id=iid)
    assert done == {"normalize"}
    assert db.tasks[("default", f"{iid}:{KIND_SPLIT}")]["status"] == "failed"


@pytest.mark.asyncio
async def test_archive_refuses_when_mandatory_stage_missing(tx, db, payloads):
    # Enqueue an archive task directly without the preceding stages: it must fail
    # closed and write no Session (defense in depth beyond the chain ordering).
    iid, _ = await _accept_push(tx, payloads)
    tasks = WorkerTaskRepository(tx)
    payload_ref = f"ingest/{iid}.json"
    async with tx.tenant_tx("default") as conn:
        await tasks.enqueue(
            conn,
            tenant_id="default",
            task_id=f"{iid}:{KIND_ARCHIVE}",
            kind=KIND_ARCHIVE,
            subject_type="ingestion",
            subject_id=iid,
            payload_ref=payload_ref,
            payload_hash="x",
        )
    services = _worker_services(tx, payloads)
    # Claim only the archive kind so normalize/analyze never run first.
    await process_once(services, owner_id="w", tenants=("default",), kinds=(KIND_ARCHIVE,), rand=lambda: 0.0)
    assert ("default", f"session_{iid}") not in db.sessions
    assert db.tasks[("default", f"{iid}:{KIND_ARCHIVE}")]["status"] == "failed"


@pytest.mark.asyncio
async def test_running_task_pins_adapter_revision_despite_later_edit(tx, db, payloads):
    repo = AdapterRepository(tx)
    async with tx.tenant_tx("default") as conn:
        rev1 = await repo.save_source(conn, tenant_id="default", adapter_id="ad1", source="v1 code")
        await repo.bind(conn, tenant_id="default", binding_key="ad1", adapter_id="ad1", revision=rev1)
    # A running pull task was enqueued pinned to rev1; now the adapter is edited.
    async with tx.tenant_tx("default") as conn:
        rev2 = await repo.save_source(conn, tenant_id="default", adapter_id="ad1", source="v2 code")
    assert rev1 == 1 and rev2 == 2
    # The pinned payload still resolves rev1's immutable source, not rev2.
    async with tx.tenant_tx("default") as conn:
        pinned = await repo.get_version(conn, adapter_id="ad1", revision=rev1)
    assert pinned["source"] == "v1 code"
    # Idempotent save: re-saving identical content does not churn the revision.
    async with tx.tenant_tx("default") as conn:
        rev2_again = await repo.save_source(conn, tenant_id="default", adapter_id="ad1", source="v2 code")
    assert rev2_again == rev2


@pytest.mark.asyncio
async def test_pull_filters_before_fetch_and_one_bad_item_does_not_fail_batch(tx, db, payloads):
    repo = AdapterRepository(tx)
    async with tx.tenant_tx("default") as conn:
        rev = await repo.save_source(conn, tenant_id="default", adapter_id="ad1", source="code")
        await repo.bind(conn, tenant_id="default", binding_key="ad1", adapter_id="ad1", revision=rev)
    # Three fetched items: one good, one upstream error, one empty.
    items = [
        {
            "external_id": "ok",
            "status": "ok",
            "session": {
                "session_id": "ok",
                "turns": [{"prompt_text": "x"}],
                "business_time": "2026-10-02T00:00:00+00:00",
            },
        },
        {"external_id": "bad", "status": "error"},
        {"external_id": "empty", "status": "ok", "session": None},
    ]
    # Enqueue the parent pull task with a pinned payload.
    tasks = WorkerTaskRepository(tx)
    parent = "pull-1"
    parent_payload = IngestionTaskPayload(
        schema_version=PAYLOAD_SCHEMA_VERSION,
        source="pull",
        ingestion_id=parent,
        tenant_id="default",
        external_id="",
        user_id="",
        account_id="a1",
        config_revision=1,
        adapter_id="ad1",
        adapter_revision=rev,
        payload_ref=f"ingest/{parent}.json",
        payload_hash="h",
        pull={"from_timestamp": "2026-10-02T00:00:00Z"},
    )
    payloads.put_payload_json(parent_payload.payload_ref, parent_payload.to_json(), {"_pull": {}})
    async with tx.tenant_tx("default") as conn:
        await tasks.enqueue(
            conn,
            tenant_id="default",
            task_id=f"{parent}:{KIND_PULL}",
            kind=KIND_PULL,
            subject_type="ingestion",
            subject_id=parent,
            payload_ref=parent_payload.payload_ref,
            payload_hash="h",
            config_revision=1,
            adapter_revision=rev,
        )
    services = _worker_services(tx, payloads, adapter_source=_AdapterSource(items))
    await process_once(services, owner_id="w", tenants=("default",), kinds=(KIND_PULL,), rand=lambda: 0.0)

    # The pull task succeeded (bad/empty items did not fail the batch) and
    # exactly one child normalize task was enqueued for the good item.
    assert db.tasks[("default", f"{parent}:{KIND_PULL}")]["status"] == "succeeded"
    child_normalize = [k for k in db.tasks if k[1].endswith(f":{KIND_NORMALIZE}")]
    assert len(child_normalize) == 1


@pytest.mark.asyncio
async def test_push_semantic_split_fans_out_idempotent_child_ingestions(tx, db, payloads):
    iid, _ = await _accept_push(tx, payloads)
    segments = [
        {
            "session_id": "s1_s001-001",
            "turns": [{"prompt_text": "topic one"}],
            "business_time": "2026-10-01T12:00:00+00:00",
            "metadata": {
                "split_from_session": "s1",
                "split_segment": {"index": 1, "total": 2, "turn_range": [1, 1]},
            },
        },
        {
            "session_id": "s1_s002-002",
            "turns": [{"prompt_text": "topic two"}],
            "business_time": "2026-10-01T12:05:00+00:00",
            "metadata": {
                "split_from_session": "s1",
                "split_segment": {"index": 2, "total": 2, "turn_range": [2, 2]},
            },
        },
    ]
    services = _worker_services(tx, payloads, runner=_StageRunner(split_segments=segments))

    await _drain(services)

    repo = SessionIngestionRepository(tx)
    async with tx.tenant_tx("default") as conn:
        parent = await repo.get_ingestion(conn, ingestion_id=iid)
        parent_stages = await repo.succeeded_stages(conn, ingestion_id=iid)
    assert parent["status"] == "skipped"
    assert parent_stages == {"normalize", "split"}
    assert ("default", f"session_{iid}") not in db.sessions

    child_ids = [f"{iid}:split:001", f"{iid}:split:002"]
    assert len(db.ingestions) == 3
    assert len(db.sessions) == 2
    for child_id, segment in zip(child_ids, segments):
        async with tx.tenant_tx("default") as conn:
            child = await repo.get_ingestion(conn, ingestion_id=child_id)
            child_stages = await repo.succeeded_stages(conn, ingestion_id=child_id)
        assert child["status"] == "archived"
        assert child["external_id"] == segment["session_id"]
        assert child_stages == {"normalize", "split", "analyze", "trace_analyze", "archive"}
        assert ("default", f"session_{child_id}") in db.sessions

    # Replaying the parent split task reuses its persisted split plan and
    # deduplicates the deterministic child ingestion IDs.
    split_task = db.tasks[("default", f"{iid}:session.split")]
    split_task.update(status="queued", owner_id="", lease_until=None, available_at=db.now)
    await process_once(
        services,
        owner_id="w",
        tenants=("default",),
        kinds=("session.split",),
        rand=lambda: 0.0,
    )
    assert len(db.ingestions) == 3
    assert len(db.sessions) == 2


def test_session_payload_hash_serializes_set_metadata():
    """Real analysis stages inject ``set`` metadata (``_skills_referenced`` /
    ``_skills_injected``) into the session before it is hashed and persisted.

    ``json.dumps`` cannot encode a ``set``; the hash (and the identical dump in
    ``SessionPayloads._put``) must coerce sets to a stable, ordered form so a
    genuine LLM-analyzed session can be archived instead of failing with a
    ``TypeError`` on every real session.
    """
    session = {
        "turns": [{"prompt_text": "x", "response_text": "y"}],
        "_skills_referenced": {"beta", "alpha"},
        "_skills_injected": set(),
        "value_judge": {"decision": "valuable"},
    }
    first = session_payload_hash(session)
    # Deterministic: set ordering must not change the hash across calls.
    session["_skills_referenced"] = {"alpha", "beta"}
    assert session_payload_hash(session) == first
