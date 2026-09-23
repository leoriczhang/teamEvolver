"""Cross-repository contract and real PostgreSQL invariants (explicit opt-in DSN)."""

import asyncio
import os
import secrets
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException

DSN = os.environ.get("ONTOLOGY_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set isolated ONTOLOGY_TEST_DSN for PostgreSQL integration")
OV = Path(os.environ.get("ONTOLOGY_OV_REPO", Path(__file__).resolve().parents[2] / "OpenViking"))
if OV.exists():
    sys.path.insert(0, str(OV))
contracts = pytest.importorskip("openviking.ontology.contracts")
pytest.importorskip("asyncpg")
from openviking.ontology.contracts import (  # noqa: E402
    Approval,
    Assertion,
    CandidateBundle,
    Commit,
    ContextRequest,
    Entity,
    Evidence,
    Manifest,
    Observation,
    Prepare,
    Schema,
    Source,
    Submission,
    TypedQuery,
    digest,
)
from openviking.ontology.service import OntologyService  # noqa: E402
from openviking.ontology.store import Principal, Store  # noqa: E402


@pytest_asyncio.fixture
async def domain():
    store = Store(DSN)
    await store.start()
    tenant = "test_" + secrets.token_hex(8)
    service = OntologyService(store, "test-signing-secret-" + "a" * 32)
    async with store.pool.acquire() as conn:
        await conn.execute("INSERT INTO ov_semantic.tenants(tenant,enabled) VALUES($1,true)", tenant)
        for user, perms in [
            ("owner", ["read", "build", "approve", "publish", "observe", "feedback"]),
            ("reader", ["read"]),
            ("outsider", ["read"]),
        ]:
            await conn.execute(
                "INSERT INTO ov_semantic.principals(tenant,subject,permissions) VALUES($1,$2,$3)", tenant, user, perms
            )
    owner = Principal(tenant, "owner")
    await service.schema(
        owner,
        Schema(
            revision="v1",
            entity_types=["Shipment"],
            predicates={"status": {"subject_type": "Shipment"}, "receipt_method": {"subject_type": "Shipment"}},
            rules=[{"rule_id": "signed", "predicate": "status", "equals": "signed"}],
            issue_pack_revision="receipt-v1",
            evidence_slots=["status", "receipt_method"],
        ),
    )
    try:
        yield service, owner
    finally:
        async with store.pool.acquire() as conn:
            for table in (
                "objects",
                "source_status",
                "submissions",
                "prepared",
                "grants",
                "commits",
                "generations",
                "facts",
                "entities",
                "events",
                "source_cursors",
                "projections",
                "principals",
            ):
                await conn.execute(f"DELETE FROM ov_semantic.{table} WHERE tenant=$1", tenant)
            await conn.execute("DELETE FROM ov_semantic.tenants WHERE tenant=$1", tenant)
            for table in ("ov_task_work", "ov_task_outbox", "ov_tasks"):
                await conn.execute(f"DELETE FROM {table} WHERE account_id=$1", tenant)
        await store.close()


async def candidate(service, owner, *, key="job1", generation=0, value="signed", readers=None, schema="v1"):
    import time

    source = Source(source_id=key, revision="r1", text=value, readers=readers or ["owner", "reader"])
    ref = await service.source(owner, source)
    manifest = Manifest(schema_revision=schema, sources=[ref], expected_generation=generation, extractor="fixture")
    frozen = await service.manifest(owner, manifest)
    submission = Submission(submission_key=key, manifest_ref=frozen["id"], manifest_digest=frozen["digest"])
    task = await service.submit(owner, submission)
    cap = service.sign(
        "runtime",
        {
            "tenant": owner.tenant,
            "subject": owner.subject,
            "task_id": task["task_id"],
            "epoch": 1,
            "expires_at": int(time.time()) + 120,
        },
    )
    bundle = CandidateBundle(
        manifest_digest=digest(manifest.model_dump(mode="json")),
        entities=[
            Entity(
                entity_id="s1", authority_namespace="demo", entity_type="Shipment", external_id="1", label="Shipment 1"
            )
        ],
        assertions=[
            Assertion(
                assertion_id="a1",
                subject="s1",
                predicate="status",
                value=value,
                valid_from="2026-01-01T00:00:00Z",
                support_sets=[["e1"]],
            )
        ],
        evidence=[Evidence(evidence_id="e1", **ref, start=0, end=len(value), quote=value)],
    )
    result = await service.accept_candidate(cap, bundle)
    return task, bundle, result, cap, submission


async def publication(service, owner, **kwargs):
    task, bundle, result, cap, submission = await candidate(service, owner, **kwargs)
    prepared = await service.prepare(owner, Prepare(task_id=task["task_id"], candidate_digest=result["digest"]))
    approved = await service.approve(
        owner, Approval(prepared_id=prepared["prepared_id"], note="Verified fixture evidence")
    )
    request = Commit(prepared_id=prepared["prepared_id"], commit_key=kwargs.get("key", "job1"), grant=approved["grant"])
    return request, task, bundle, cap, submission


@pytest.mark.asyncio
async def test_publish_read_and_receipt_replay(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    receipt = await svc.commit(owner, request)
    assert receipt == await svc.commit(owner, request) == await svc.receipt(owner, request.commit_key)
    packet = await svc.compose(Principal(owner.tenant, "reader"), ContextRequest(entity_ids=["s1"]))
    assert packet["evaluations"][0]["truth"] == "true"
    assert packet["missing_evidence"] == ["receipt_method"]
    assert packet["facts"][0]["proofs"][0][0]["quote"] == "signed"
    # A routine restart must not rerun unchanged table DDL while a writer is active.
    restarted = Store(DSN)
    try:
        async with svc.store.pool.acquire() as conn, conn.transaction():
            await conn.execute("LOCK TABLE ov_semantic.facts IN ROW EXCLUSIVE MODE")
            await asyncio.wait_for(restarted.start(), timeout=3)
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_context_keeps_entities_separate(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    packet = await svc.compose(owner, ContextRequest(entity_ids=["s1", "unknown-shipment"]))
    results = {row["entity_id"]: row["truth"] for row in packet["evaluations"]}
    assert results == {"s1": "true", "unknown-shipment": "unknown"}
    assert "status" in packet["missing_evidence"]


@pytest.mark.asyncio
async def test_enterprise_error_contract_does_not_echo_input(domain):
    import httpx
    from fastapi import FastAPI
    from openviking.ontology.api import router

    svc, owner = domain
    app = FastAPI()
    app.state.ontology = svc
    app.include_router(router(lambda: owner))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/enterprise/ontology/query", json={"sql": "sensitive input"})
        assert response.status_code == 422
        assert response.json()["contract"] == "sf.ontology.error.v1"
        assert "sensitive input" not in response.text


@pytest.mark.asyncio
async def test_source_sequence_gap_does_not_delay_withdrawal(domain):
    svc, owner = domain
    await svc.source(owner, Source(source_id="first", revision="r1", text="a", readers=["owner"]))
    await svc.source(owner, Source(source_id="second", revision="r1", text="b", readers=["owner"]))
    late = {"source_id": "second", "revision": "r1", "partition": "wiki", "sequence": 2, "status": "revoked"}
    result = await svc.event(owner, "late", late)
    assert result["sequence_gap"]
    assert (await svc.event(owner, "late", late))["replayed"]
    with pytest.raises(HTTPException, match="SOURCE_SEQUENCE_CONFLICT"):
        await svc.event(owner, "duplicate-sequence", {**late, "source_id": "first"})
    await svc.event(owner, "first", {**late, "source_id": "first", "sequence": 1, "status": "active"})
    async with svc.store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT sequence FROM ov_semantic.source_cursors WHERE tenant=$1 AND partition='wiki'", owner.tenant
            )
            == 2
        )


@pytest.mark.asyncio
async def test_submission_concurrency_and_conflict(domain):
    svc, owner = domain
    task, _, _, _, request = await candidate(svc, owner)
    results = await asyncio.gather(*(svc.submit(owner, request) for _ in range(8)))
    assert {r["task_id"] for r in results} == {task["task_id"]}
    with pytest.raises(HTTPException, match="IDEMPOTENCY_CONFLICT"):
        await svc.submit(owner, request.model_copy(update={"manifest_digest": "bad"}))


@pytest.mark.asyncio
async def test_competing_commit_only_one_head(domain):
    svc, owner = domain
    first, *_ = await publication(svc, owner, key="one")
    second, *_ = await publication(svc, owner, key="two")
    results = await asyncio.gather(svc.commit(owner, first), svc.commit(owner, second), return_exceptions=True)
    assert len([x for x in results if isinstance(x, dict)]) == 1
    assert len([x for x in results if isinstance(x, HTTPException) and x.status_code == 409]) == 1


@pytest.mark.asyncio
async def test_candidate_not_readable_before_publication(domain):
    svc, owner = domain
    await publication(svc, owner)
    assert not (await svc.query(owner, TypedQuery()))["facts"]


@pytest.mark.asyncio
async def test_hidden_sources_and_cross_tenant(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    assert not (await svc.query(Principal(owner.tenant, "outsider"), TypedQuery()))["facts"]
    assert not (await svc.resolve(Principal(owner.tenant, "outsider"), "Shipment 1", TypedQuery()))["candidates"]
    with pytest.raises(HTTPException):
        await svc.query(Principal("different", "owner"), TypedQuery())


@pytest.mark.asyncio
async def test_revoke_blocks_read_and_pending_grant(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    pending, *_ = await publication(svc, owner, key="later", generation=1)
    event = {"source_id": "job1", "revision": "r1", "status": "revoked"}
    result = await svc.event(owner, "event1", event)
    assert result["affected_assertions"] == ["a1"]
    assert (await svc.event(owner, "event1", event))["replayed"]
    assert not (await svc.query(owner, TypedQuery()))["facts"]
    with pytest.raises(HTTPException, match="POLICY_CHANGED"):
        await svc.commit(owner, pending)


@pytest.mark.asyncio
async def test_source_unavailable_not_deleted(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    await svc.event(owner, "outage", {"source_id": "job1", "revision": "r1", "status": "unavailable"})
    result = await svc.query(owner, TypedQuery())
    assert result["degraded"] and not result["facts"]
    await svc.event(owner, "recovery", {"source_id": "job1", "revision": "r1", "status": "active"})
    assert (await svc.query(owner, TypedQuery()))["facts"]


@pytest.mark.asyncio
async def test_cancel_fences_late_upload_and_commit(domain):
    svc, owner = domain
    request, task, bundle, cap, _ = await publication(svc, owner)
    await svc.cancel(owner, task["task_id"])
    with pytest.raises(HTTPException, match="TASK_FENCED"):
        await svc.accept_candidate(cap, bundle)
    with pytest.raises(HTTPException, match="TASK_FENCED"):
        await svc.commit(owner, request)


@pytest.mark.asyncio
async def test_candidate_immutable_after_approval(domain):
    svc, owner = domain
    _, _, bundle, cap, _ = await publication(svc, owner)
    changed = bundle.model_copy(deep=True)
    changed.assertions[0].value = "forged"
    with pytest.raises(HTTPException, match="IMMUTABLE_REVISION_CONFLICT"):
        await svc.accept_candidate(cap, changed)


@pytest.mark.asyncio
async def test_bitemporal_and_atomic_budget(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    assert not (await svc.query(owner, TypedQuery(valid_at="2025-01-01T00:00:00Z")))["facts"]
    assert not (await svc.query(owner, TypedQuery(known_at="2025-01-01T00:00:00Z")))["facts"]
    packet = await svc.compose(owner, ContextRequest(token_budget=512))
    assert packet["truncated"] and packet["facts"] == []
    assert packet["evaluations"][0]["truth"] == "unknown"


@pytest.mark.asyncio
async def test_read_only_cannot_approve(domain):
    svc, owner = domain
    with pytest.raises(HTTPException, match="FORBIDDEN"):
        await svc.approve(Principal(owner.tenant, "reader"), Approval(prepared_id="x", note="forged"))


@pytest.mark.asyncio
async def test_observation_subject_task_audience_bound(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    await svc.source(
        owner,
        Source(
            source_id="connector",
            revision="1",
            text="signed",
            readers=["owner", "reader"],
            provenance="trusted_connector",
        ),
    )
    handle = (
        await svc.observe(
            owner,
            Observation(
                task_ref="case-1",
                subject="s1",
                predicate="status",
                value="signed",
                source_id="connector",
                revision="1",
                audience="reader",
            ),
        )
    )["observation_handle"]
    packet = await svc.compose(
        Principal(owner.tenant, "reader"),
        ContextRequest(entity_ids=["s1"], task_ref="case-1", observation_handles=[handle]),
    )
    assert packet["observations"]
    with pytest.raises(HTTPException, match="OBSERVATION_SCOPE_MISMATCH"):
        await svc.compose(
            Principal(owner.tenant, "reader"),
            ContextRequest(entity_ids=["s1"], task_ref="case-2", observation_handles=[handle]),
        )


def test_timezone_and_unsafe_query_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TypedQuery(known_at="2026-01-01T00:00:00")
    with pytest.raises(ValidationError):
        TypedQuery(max_hops=200)
    with pytest.raises(ValidationError):
        TypedQuery(sql="SELECT *")


@pytest.mark.asyncio
async def test_rollback_requires_new_approval_and_current_sources(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    first = await svc.commit(owner, request)
    rollback = await svc.rollback_candidate(owner, first["generation"], "rollback1")
    assert (await svc.capabilities(owner))["generation"] == first["generation"]
    prepared = await svc.prepare(
        owner, Prepare(task_id=rollback["task_id"], candidate_digest=rollback["candidate_digest"])
    )
    grant = await svc.approve(owner, Approval(prepared_id=prepared["prepared_id"], note="Revalidated rollback"))
    receipt = await svc.commit(
        owner, Commit(prepared_id=prepared["prepared_id"], grant=grant["grant"], commit_key="rollback1")
    )
    assert receipt["generation"] > first["generation"]
    await svc.event(owner, "withdraw", {"source_id": "job1", "revision": "r1", "status": "revoked"})
    with pytest.raises(HTTPException, match="EVIDENCE_INVALID"):
        await svc.rollback_candidate(owner, first["generation"], "rollback2")


@pytest.mark.asyncio
async def test_rebase_freezes_current_generation(domain):
    svc, owner = domain
    stale, task, *_ = await publication(svc, owner, key="stale")
    winner, *_ = await publication(svc, owner, key="winner")
    receipt = await svc.commit(owner, winner)
    rebased = await svc.rebase(owner, task["task_id"], "rebased")
    assert rebased["task_id"] != task["task_id"]
    detail = await svc.task(owner, task_id=rebased["task_id"])
    assert detail["manifest"]["expected_generation"] == receipt["generation"]


@pytest.mark.asyncio
async def test_principal_revocation_checked_on_every_read(domain):
    svc, owner = domain
    request, *_ = await publication(svc, owner)
    await svc.commit(owner, request)
    await svc.revoke_principal(owner, "reader")
    with pytest.raises(HTTPException, match="FORBIDDEN"):
        await svc.query(Principal(owner.tenant, "reader"), TypedQuery())
