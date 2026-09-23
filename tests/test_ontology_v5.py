"""Two databases, TE authority, OV assets, and no independent runtime process."""

import asyncio
import base64
import copy
import json
import os
import secrets
import sys
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "OpenViking"))
pytest.importorskip("openviking.ontology.agents")
from openviking.ontology.agents import SearchRequest, dispatch, search
from openviking.ontology.api import router as legacy_router
from openviking.ontology.assets import AssetService
from openviking.ontology.assets import router as asset_router
from openviking.ontology.contracts import CommitV2, TypedQuery
from openviking.ontology.store import Principal, Store

from team_ontology.config import OntologyConfig
from team_ontology.service import Operations
from team_ontology.wire import Manifest

TE_DSN = os.environ.get("ONTOLOGY_V5_TE_DSN")
OV_DSN = os.environ.get("ONTOLOGY_V5_OV_DSN")
pytestmark = pytest.mark.skipif(not (TE_DSN and OV_DSN), reason="requires two isolated PostgreSQL databases")


@pytest_asyncio.fixture
async def domain(tmp_path, monkeypatch):
    tenant = "v5_" + secrets.token_hex(6)
    monkeypatch.delenv("OV_ONTOLOGY_TE_PUBLIC_KEYS", raising=False)
    monkeypatch.delenv("TE_ONTOLOGY_SIGNING_KEY_FILE", raising=False)
    store = Store(OV_DSN, native_tasks=False)
    await store.start()
    svc = AssetService(store, "observation-secret-" + "x" * 32)
    async with store.pool.acquire() as c:
        await c.execute("INSERT INTO public.ov_ontology_tenants(tenant,enabled) VALUES($1,true)", tenant)
        for name, perms in [
            ("frank", ["build", "approve", "publish", "read", "feedback"]),
            ("agent", ["read", "feedback"]),
            ("outsider", ["read"]),
        ]:
            await c.execute("INSERT INTO public.ov_ontology_principals VALUES($1,$2,$3,true)", tenant, name, perms)
    app = FastAPI()
    app.state.ontology = svc
    app.state.config = SimpleNamespace(root_api_key="backend-only", get_effective_auth_mode=lambda: "trusted")

    async def principal(request: Request):
        from openviking.server.auth.plugins.trusted import TrustedAuthPlugin
        from openviking.server.identity import RequestContext
        from openviking_cli.session.user_id import UserIdentifier
        from openviking.ontology.host import authenticated_principal
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        identity = await TrustedAuthPlugin().resolve_identity(
            request, api_key=key, x_openviking_account=request.headers.get("x-openviking-account"),
            x_openviking_user=request.headers.get("x-openviking-user"),
        )
        return authenticated_principal(app.state.config, RequestContext(
            user=UserIdentifier(identity.account_id, identity.user_id), role=identity.role, api_key=key,
        ))

    app.include_router(legacy_router(principal))
    app.include_router(asset_router(principal))

    async def resolve(p):
        assert p["tenant"] == tenant
        return {"url": "http://ov", "account": tenant, "api_key": "backend-only", "model": {}}

    cfg = OntologyConfig(
        enabled=True,
        state_dir=str(tmp_path / "work"),
        allow_fixture=True,
    )
    ops = Operations(cfg, TE_DSN, resolve, httpx.ASGITransport(app=app))
    await ops.store.start()
    p = {"tenant": tenant, "subject": "frank"}
    await ops.ov(
        p,
        "POST",
        "/schemas",
        {
            "revision": "v1",
            "entity_types": ["Shipment"],
            "predicates": {"status": {"subject_type": "Shipment"}},
            "rules": [{"rule_id": "signed", "predicate": "status", "equals": "signed"}],
            "issue_pack_revision": "v1",
            "evidence_slots": ["status"],
        },
    )
    try:
        yield ops, svc, p
    finally:
        async with ops.store.pool.acquire() as c:
            await c.execute(
                f"DELETE FROM {ops.store.outbox} WHERE job_id IN (SELECT id FROM {ops.store.jobs} WHERE tenant=$1)",
                tenant,
            )
            await c.execute(f"DELETE FROM {ops.store.jobs} WHERE tenant=$1", tenant)
            await c.execute(f"DELETE FROM {ops.store.audit_table} WHERE tenant=$1", tenant)
        await ops.close()
        async with store.pool.acquire() as c:
            for table in [
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
                "artifact_sessions",
                "principals",
            ]:
                await c.execute(f"DELETE FROM public.ov_ontology_{table} WHERE tenant=$1", tenant)
            await c.execute("DELETE FROM public.ov_ontology_tenants WHERE tenant=$1", tenant)
        await store.close()


async def build(ops, p, key="job", generation=0, entity_type="Shipment"):
    record = {
        "entity": {
            "entity_id": "shipment:1",
            "authority_namespace": "test",
            "entity_type": entity_type,
            "external_id": "1",
            "label": "运单1",
            "aliases": ["签收单1"],
        },
        "quote": "signed",
        "assertion": {
            "assertion_id": "a1",
            "subject": "shipment:1",
            "predicate": "status",
            "value": "signed",
            "valid_from": "2026-01-01T00:00:00Z",
        },
    }
    ref = await ops.ov(
        p,
        "POST",
        "/sources",
        {"source_id": key, "revision": "r1", "text": json.dumps([record]), "readers": ["frank", "agent"]},
    )
    manifest = Manifest(
        schema_revision="v1", sources=[ref], expected_generation=generation, extractor="fixture"
    ).model_dump(mode="json")
    job = await ops.store.submit(p, key, {"manifest": manifest, "sources": []})
    await ops.execute(await ops.store.claim())
    result = await ops.store.get(p, job["id"])
    assert result["state"] == "review_ready", result.get("error")
    return result


async def publish(ops, p, key="job", generation=0):
    job = await build(ops, p, key, generation)
    await ops.prepare(p, job["id"])
    await ops.approve(p, job["id"], "Evidence reviewed")
    return await ops.commit(p, job["id"], key + "-commit")


@pytest.mark.asyncio
async def test_two_database_publish_agent_and_idempotent_receipt(domain):
    ops, svc, p = domain
    job = await publish(ops, p)
    assert job["state"] == "published"
    assert (await ops.commit(p, job["id"], "job-commit"))["result"]["receipt"] == job["result"]["receipt"]
    packet = await dispatch(svc, Principal(p["tenant"], "agent"), "compose", {"entity_ids": ["shipment:1"]})
    assert packet["evaluations"][0]["truth"] == "true"
    assert packet["facts"][0]["proofs"][0][0]["quote"] == "signed"
    assert await svc.store.pool.fetchval("SELECT to_regclass('public.ov_tasks')") is None
    assert await ops.store.pool.fetchval("SELECT to_regclass('public.ov_ontology_facts')") is None
    assert await svc.store.pool.fetchval("SELECT to_regclass('teamevolver.ontology_jobs')") is None


@pytest.mark.asyncio
async def test_te_submission_concurrency_and_tenant_isolation(domain):
    ops, _, p = domain
    requests = await asyncio.gather(*(ops.store.submit(p, "same", {"manifest": {}}) for _ in range(8)))
    assert len({j["id"] for j in requests}) == 1
    with pytest.raises(HTTPException, match="IDEMPOTENCY_CONFLICT"):
        await ops.store.submit(p, "same", {"manifest": {"x": 1}})
    with pytest.raises(HTTPException, match="JOB_NOT_FOUND"):
        await ops.store.get({**p, "subject": "outsider"}, requests[0]["id"])


@pytest.mark.asyncio
async def test_edit_invalidates_previous_approval(domain):
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    approved = await ops.approve(p, job["id"], "ok")
    result = approved["result"]
    candidate = copy.deepcopy(result["candidate"])
    candidate["entities"][0]["label"] = "Revised label"
    edited = await ops.edit(p, job["id"], candidate, result["artifact"]["candidate_digest"])
    assert edited["state"] == "review_ready" and not {"grant", "approval"} & edited["result"].keys()
    with pytest.raises(HTTPException, match="TASK_FENCED"):
        await svc.commit(
            Principal(p["tenant"], "frank", trusted_publication=True),
            CommitV2(contract="sf.ontology.commit.v2",prepared_id=result["prepared"]["prepared_id"], commit_key="old", approval=result["approval"]),
        )


@pytest.mark.asyncio
async def test_cancel_fences_late_upload(domain):
    ops, _, p = domain
    job = await build(ops, p)
    await ops.cancel(p, job["id"])
    with pytest.raises(HTTPException, match="EXECUTION_FENCED"):
        await ops.ov(
            p,
            "POST",
            "/artifacts",
            {
                "job_id": job["id"],
                "attempt": job["attempt"],
                "manifest": job["result"]["manifest"],
                "candidate": job["result"]["candidate"],
            },
        )


@pytest.mark.asyncio
async def test_changed_digest_and_revoked_reviewer(domain):
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    job = await ops.approve(p, job["id"], "ok")
    r = job["result"]
    forged = {**r["approval"], "candidate_digest": "replaced"}
    with pytest.raises(HTTPException, match="CANDIDATE_CHANGED"):
        await svc.commit(
            Principal(p["tenant"], "frank", trusted_publication=True),
            CommitV2(contract="sf.ontology.commit.v2", prepared_id=r["prepared"]["prepared_id"],
                     commit_key="forged", approval=forged),
        )
    await svc.store.pool.execute(
        "UPDATE public.ov_ontology_principals SET permissions=ARRAY['read','build','publish'] WHERE tenant=$1 AND subject='frank'",
        p["tenant"],
    )
    with pytest.raises(HTTPException, match="POLICY_CHANGED"):
        await ops.commit(p, job["id"], "revoked")
    assert (await ops.store.get(p, job["id"]))["state"] == "commit_unknown"


@pytest.mark.asyncio
async def test_lost_commit_response_reconciles_without_second_version(domain):
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    await ops.approve(p, job["id"], "ok")
    real = ops.ov

    async def lost(p, method, path, body=None):
        result = await real(p, method, path, body)
        if path == "/assets/commits":
            raise HTTPException(503, "response lost")
        return result

    ops.ov = lost
    with pytest.raises(HTTPException):
        await ops.commit(p, job["id"], "lost")
    ops.ov = real
    result = await ops.commit(p, job["id"], "lost")
    assert result["state"] == "published"
    assert await svc.store.pool.fetchval("SELECT count(*) FROM public.ov_ontology_commits WHERE tenant=$1", p["tenant"]) == 1


@pytest.mark.asyncio
async def test_search_aliases_document_fallback_and_current_source_permissions(domain):
    ops, svc, p = domain
    await publish(ops, p)

    async def retriever(body, query):
        return {"query": query, "resources": []}

    principal = Principal(p["tenant"], "agent")
    result = await search(svc, principal, SearchRequest(query="签收单1状态"), retriever)
    assert result["mode"] == "ontology_enhanced" and result["context"]["facts"]
    unknown = await search(svc, principal, SearchRequest(query="weather"), retriever)
    assert unknown["mode"] == "document_only" and not unknown["context"]["facts"]
    outsider = await svc.query(Principal(p["tenant"], "outsider"), TypedQuery())
    assert not outsider["facts"]
    await svc.event(
        Principal(p["tenant"], "frank"), "withdraw", {"source_id": "job", "revision": "r1", "status": "revoked"}
    )
    assert not (await svc.query(principal, TypedQuery()))["facts"]


@pytest.mark.asyncio
async def test_restart_reclaims_expired_execution_and_fences_old_attempt(domain):
    ops, _, p = domain
    await ops.store.submit(p, "expired", {"manifest": {}})
    first = await ops.store.claim()
    await ops.ov(p, "POST", "/artifacts/sessions", {"job_id": first["id"], "attempt": first["attempt"]})
    await ops.store.pool.execute(
        f"UPDATE {ops.store.jobs} SET lease_until=now()-interval '1 second' WHERE id=$1", first["id"]
    )
    second = await ops.store.claim()
    assert second["attempt"] == first["attempt"] + 1
    await ops.ov(p, "POST", "/artifacts/sessions", {"job_id": second["id"], "attempt": second["attempt"]})
    with pytest.raises(HTTPException, match="EXECUTION_FENCED"):
        await ops.ov(p, "POST", "/artifacts/sessions", {"job_id": first["id"], "attempt": first["attempt"]})
    assert not await ops.store.finish(first, {"late": True})


@pytest.mark.asyncio
async def test_mcp_tools_use_same_read_service_and_do_not_expose_publication(domain):
    from openviking.ontology import agents
    from openviking.server import mcp_endpoint
    from openviking.server.identity import RequestContext, Role
    from openviking_cli.session.user_id import UserIdentifier
    ops, svc, p = domain
    await publish(ops, p)
    agents.bind_service(svc)
    agents.install_mcp()
    tools = await mcp_endpoint.mcp.list_tools()
    names = {tool.name for tool in tools if tool.name.startswith('ontology_')}
    assert names == {'ontology_capabilities', 'ontology_read', 'ontology_feedback'}
    token = mcp_endpoint._mcp_ctx.set(RequestContext(user=UserIdentifier(p['tenant'], 'agent'), role=Role.USER))
    try:
        result = await mcp_endpoint.mcp.call_tool('ontology_read', {'operation': 'query', 'arguments': {'entity_ids': ['shipment:1']}})
        expected = await svc.query(Principal(p['tenant'], 'agent'), TypedQuery(entity_ids=['shipment:1']))
        structured = result if isinstance(result, dict) else json.loads(result[0].text)
        assert structured == expected
    finally:
        mcp_endpoint._mcp_ctx.reset(token)


@pytest.mark.asyncio
async def test_queue_limit_and_transient_retry(domain):
    ops, _, p = domain
    ops.store.limit = 1
    first = await ops.store.submit(p, 'limit-1', {})
    with pytest.raises(HTTPException, match='ONTOLOGY_QUEUE_FULL'):
        await ops.store.submit(p, 'limit-2', {})
    claimed = await ops.store.claim()
    assert await ops.store.claim() is None
    await ops.store.finish(claimed, error='OV_UNAVAILABLE', retry=True)
    retried = await ops.store.claim()
    assert retried['id'] == first['id'] and retried['attempt'] == 2


@pytest.mark.asyncio
async def test_rollback_and_competing_base_publish(domain):
    ops, svc, p = domain
    one = await build(ops, p, 'one'); two = await build(ops, p, 'two')
    await ops.prepare(p, one['id']); await ops.approve(p, one['id'], 'one')
    await ops.prepare(p, two['id']); await ops.approve(p, two['id'], 'two')
    first = await ops.commit(p, one['id'], 'first')
    with pytest.raises(HTTPException, match='BASE_CHANGED'):
        await ops.commit(p, two['id'], 'competing')
    rebased = await ops.rebase(p, two['id'], 'rebased')
    await ops.execute(await ops.store.claim())
    assert (await ops.store.get(p, rebased['id']))['state'] == 'review_ready'
    rollback = await ops.rollback(p, first['result']['receipt']['generation'], 'rollback')
    await ops.execute(await ops.store.claim())
    await ops.prepare(p, rollback['id']); await ops.approve(p, rollback['id'], 'rollback reviewed')
    result = await ops.commit(p, rollback['id'], 'rollback-commit')
    assert result['result']['receipt']['generation'] > first['result']['receipt']['generation']


@pytest.mark.asyncio
async def test_documents_and_legacy_import_do_not_publish(domain):
    from team_ontology.review import Document, parse_document, LegacyImport
    from team_ontology.engine.provenance import AnnotatedPack
    ops, svc, p = domain
    document = parse_document(Document(filename='../../policy.txt', source_id='doc', readers=['frank'], content_base64=base64.b64encode('签收需要凭证'.encode()).decode()))
    assert document.text == '签收需要凭证'
    pack = json.loads((Path(__file__).parent/'ontology_engine/fixtures/pack_valid_quickstart.json').read_text())
    annotated = AnnotatedPack(pack=pack)
    envelope = LegacyImport(import_key='legacy', annotated=annotated).model_dump(mode='json')
    imported = await ops.store.submit(p,'legacy',envelope,imported={'requires_enrichment':True})
    assert imported['state']=='imported'
    assert await ops.store.claim() is None
    assert (await svc.capabilities(Principal(p['tenant'],'frank')))['generation']==0

@pytest.mark.asyncio
async def test_recovery_reconciles_lost_reply_and_resets_stale_prepare(domain):
    ops, _, p = domain
    job = await publish(ops, p)
    receipt = job['result']['receipt']
    result = {k:v for k,v in job['result'].items() if k != 'receipt'}
    await ops.store.change(p, job['id'], ['published'], 'publishing', result)
    await ops.store.pool.execute(f"UPDATE {ops.store.jobs} SET updated=now()-interval '121 seconds' WHERE id=$1", job['id'])
    await ops.recover()
    recovered = await ops.store.get(p, job['id'])
    assert recovered['state'] == 'published' and recovered['result']['receipt'] == receipt
    second = await build(ops, p, 'recover-prepare', 1)
    await ops.store.change(p, second['id'], ['review_ready'], 'preparing')
    await ops.store.pool.execute(f"UPDATE {ops.store.jobs} SET updated=now()-interval '121 seconds' WHERE id=$1", second['id'])
    await ops.recover()
    assert (await ops.store.get(p, second['id']))['state'] == 'review_ready'

@pytest.mark.asyncio
async def test_expired_approval_is_rejected(domain):
    import time
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job['id'])
    job = await ops.approve(p, job['id'], 'reviewed')
    result = job['result']
    expired = {**result['approval'], 'expires_at': int(time.time()) - 1}
    with pytest.raises(HTTPException, match='APPROVAL_EXPIRED'):
        await svc.commit(
            Principal(p['tenant'], 'frank', trusted_publication=True),
            CommitV2(contract='sf.ontology.commit.v2', prepared_id=result['prepared']['prepared_id'],
                     commit_key='expired', approval=expired),
        )


@pytest.mark.asyncio
async def test_native_identity_pins_agent_and_rejects_root_data_access(domain, monkeypatch):
    from types import SimpleNamespace
    from openviking.ontology.host import install_native
    from openviking.server.config import ServerConfig
    from openviking.server.identity import ResolvedIdentity, Role
    ops, svc, p = domain
    monkeypatch.setenv('OV_ONTOLOGY_ENABLED', '1')
    app = FastAPI()
    app.state.config = ServerConfig(auth_mode='api_key', root_api_key='backend-secret')
    identities = {'agent-key': (Role.USER, p['tenant'], 'agent'), 'backend-secret': (Role.ROOT, 'default', 'default')}
    app.state.api_key_manager = SimpleNamespace(resolve=lambda key: ResolvedIdentity(*identities[key]))
    from openviking.server.auth.plugins.api_key import ApiKeyAuthPlugin
    app.state.auth_plugin = ApiKeyAuthPlugin()
    app.state.ontology = svc
    install_native(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ov') as client:
        response = await client.get('/api/v1/enterprise/ontology/capabilities', headers={
            'Authorization':'Bearer agent-key', 'X-OpenViking-Account':'another', 'X-OpenViking-User':'frank'})
        assert response.status_code == 200, response.text
        assert set(response.json()['permissions']) == {'read','feedback'}
        from openviking_cli.exceptions import PermissionDeniedError
        with pytest.raises(PermissionDeniedError, match="ROOT API keys"):
            await client.get('/api/v1/enterprise/ontology/capabilities', headers={
                'Authorization':'Bearer backend-secret', 'X-OpenViking-Account':p['tenant'], 'X-OpenViking-User':'frank'})
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    job = await ops.approve(p, job["id"], "reviewed")
    approval = job["result"]["approval"]
    identities["admin-key"] = (Role.ADMIN, p["tenant"], "frank")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ov") as client:
        denied = await client.post("/api/v1/enterprise/assets/commits",
                                   headers={"Authorization": "Bearer admin-key"}, json={
            "contract": "sf.ontology.commit.v2", "prepared_id": approval["prepared_id"],
            "commit_key": "api-key-admin", "approval": approval,
        })
        assert denied.status_code == 403 and denied.json()["detail"] == "TRUSTED_ROOT_REQUIRED"
    app.state.config = ServerConfig(auth_mode='trusted')
    with pytest.raises(ValueError, match='rootless trusted/dev'):
        install_native(app)


@pytest.mark.asyncio
async def test_commit_requires_manual_approval_and_trusted_root(domain):
    ops, svc, p = domain
    job = await build(ops, p)
    with pytest.raises(HTTPException, match="APPROVAL_REQUIRED"):
        await ops.commit(p, job["id"], "unapproved")
    await ops.prepare(p, job["id"])
    approved = await ops.approve(p, job["id"], "manual review")
    approval = approved["result"]["approval"]
    assert "grant" not in approved["result"]
    body = CommitV2(contract="sf.ontology.commit.v2", prepared_id=approval["prepared_id"],
                    commit_key="root-required", approval=approval)
    with pytest.raises(HTTPException, match="TRUSTED_ROOT_REQUIRED"):
        await svc.commit(Principal(p["tenant"], "frank"), body)
    with pytest.raises(HTTPException, match="FORBIDDEN"):
        await svc.commit(Principal(p["tenant"], "agent", trusted_publication=True), body)
    wrong_tenant = body.model_copy(update={"approval": body.approval.model_copy(update={"tenant": "other"})})
    with pytest.raises(HTTPException, match="INVALID_GRANT"):
        await svc.commit(Principal(p["tenant"], "frank", trusted_publication=True), wrong_tenant)
    first = await svc.commit(Principal(p["tenant"], "frank", trusted_publication=True), body)
    assert await svc.commit(Principal(p["tenant"], "frank", trusted_publication=True), body) == first
    with pytest.raises(HTTPException, match="POLICY_CHANGED"):
        await svc.commit(Principal(p["tenant"], "frank", trusted_publication=True),
                         body.model_copy(update={"commit_key": "reused-approval"}))
    assert await svc.store.pool.fetchval("SELECT count(*) FROM public.ov_ontology_commits WHERE tenant=$1", p["tenant"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["approved", "publishing", "commit_unknown"])
async def test_legacy_approval_requires_new_review_without_resubmission(domain, state):
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    job = await ops.approve(p, job["id"], "old review")
    result = {**job["result"], "grant": "historical-signed-token", "approval": {"note": "old"}}
    if state != "approved":
        result["commit_key"] = "old-key"
    await ops.store.change(p, job["id"], ["approved"], state, result)
    await ops.store.pool.execute(f"UPDATE {ops.store.jobs} SET updated=now()-interval '121 seconds' WHERE id=$1", job["id"])
    await ops.recover()
    reset = await ops.store.get(p, job["id"])
    assert reset["state"] == "review_ready"
    assert not {"grant", "approval", "prepared", "commit_key"} & reset["result"].keys()
    audit = await ops.store.pool.fetchval(
        f"SELECT body FROM {ops.store.audit_table} WHERE tenant=$1 AND event='job.review_ready' ORDER BY id DESC LIMIT 1",
        p["tenant"],
    )
    assert json.loads(audit)["migration"]["reason"] == "PUBLICATION_REAPPROVAL_REQUIRED"
    assert await svc.store.pool.fetchval("SELECT count(*) FROM public.ov_ontology_commits WHERE tenant=$1", p["tenant"]) == 0
    await ops.prepare(p, job["id"])
    await ops.approve(p, job["id"], "new manual review")
    assert (await ops.commit(p, job["id"], "new-key"))["state"] == "published"


@pytest.mark.asyncio
async def test_legacy_uncertain_commit_reconciles_and_waits_when_ov_unavailable(domain):
    ops, svc, p = domain
    job = await publish(ops, p)
    result = {k: v for k, v in job["result"].items() if k not in {"receipt", "approval"}}
    result["grant"] = "old-signed-token"
    await ops.store.change(p, job["id"], ["published"], "commit_unknown", result)
    await ops.store.pool.execute(f"UPDATE {ops.store.jobs} SET updated=now()-interval '121 seconds' WHERE id=$1", job["id"])
    real = ops.ov
    async def unavailable(*args, **kwargs):
        raise HTTPException(503, "OV unavailable")
    ops.ov = unavailable
    await ops.recover()
    assert (await ops.store.get(p, job["id"]))["state"] == "commit_unknown"
    ops.ov = real
    await ops.store.pool.execute(f"UPDATE {ops.store.jobs} SET updated=now()-interval '121 seconds' WHERE id=$1", job["id"])
    await ops.recover()
    restored = await ops.store.get(p, job["id"])
    assert restored["state"] == "published"
    assert restored["result"]["receipt"] == job["result"]["receipt"]
    assert await svc.store.pool.fetchval("SELECT count(*) FROM public.ov_ontology_commits WHERE tenant=$1", p["tenant"]) == 1


@pytest.mark.asyncio
async def test_native_trusted_root_publication_boundary(domain, monkeypatch):
    from openviking.ontology.host import install_native
    from openviking.server.auth.plugins.trusted import TrustedAuthPlugin
    from openviking.server.config import ServerConfig
    from openviking_cli.exceptions import UnauthenticatedError, InvalidArgumentError
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    job = await ops.approve(p, job["id"], "native review")
    approval = job["result"]["approval"]
    body = {"contract": "sf.ontology.commit.v2", "prepared_id": approval["prepared_id"],
            "commit_key": "native", "approval": approval}
    monkeypatch.setenv("OV_ONTOLOGY_ENABLED", "1")
    app = FastAPI()
    app.state.config = ServerConfig(auth_mode="trusted", root_api_key="test-root")
    app.state.auth_plugin = TrustedAuthPlugin()
    app.state.ontology = svc
    install_native(app)
    headers = {"Authorization": "Bearer test-root", "X-OpenViking-Account": p["tenant"],
               "X-OpenViking-User": "frank", "X-OpenViking-Role": "admin"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ov") as client:
        for value in ("", "Bearer wrong"):
            with pytest.raises(UnauthenticatedError):
                await client.post("/api/v1/enterprise/assets/commits", json=body,
                                  headers={**headers, "Authorization": value})
        with pytest.raises(InvalidArgumentError):
            await client.post("/api/v1/enterprise/assets/commits", json=body,
                              headers={k: v for k, v in headers.items() if k != "X-OpenViking-User"})
        denied = await client.post("/api/v1/enterprise/assets/commits", json=body,
                                   headers={**headers, "X-OpenViking-User": "agent"})
        assert denied.status_code == 403
        denied = await client.post("/api/v1/enterprise/assets/commits", json=body,
                                   headers={**headers, "X-OpenViking-Account": "other"})
        assert denied.status_code == 403
        legacy = await client.post("/api/v1/enterprise/assets/commits", headers=headers,
                                   json={"prepared_id": approval["prepared_id"], "commit_key": "legacy", "grant": "old"})
        assert legacy.status_code == 422
        success = await client.post("/api/v1/enterprise/assets/commits", json=body, headers=headers)
        assert success.status_code == 200, success.text


@pytest.mark.asyncio
async def test_source_withdrawal_after_approval_blocks_publication(domain):
    ops, svc, p = domain
    job = await build(ops, p)
    await ops.prepare(p, job["id"])
    await ops.approve(p, job["id"], "reviewed")
    await svc.event(Principal(p["tenant"], "frank"), "revoke-before-commit",
                    {"source_id": "job", "revision": "r1", "status": "revoked"})
    with pytest.raises(HTTPException, match="POLICY_CHANGED"):
        await ops.commit(p, job["id"], "revoked-source")
    assert await svc.store.pool.fetchval("SELECT count(*) FROM public.ov_ontology_commits WHERE tenant=$1", p["tenant"]) == 0
