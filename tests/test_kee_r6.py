"""R6 acceptance with real temporary PG, simulated generic OV HTTP, and no business execution hooks."""

import asyncio
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

from team_ontology.config import OntologyConfig
from team_ontology.kee.api import install
from team_ontology.kee.contracts import ProcedurePack, digest, verify_evidence
from team_ontology.kee.wiki import render, validate_directory
from team_ontology.service import Operations
from teamEvolver.storage.pg_pool import init_pg_schema

DSN = os.environ.get("KEE_R6_TEST_DSN", "")


class GenericOV:
    def __init__(self):
        self.files = {"viking://resources/input/签收.md": "签收必须核验授权。未经授权不得签收。异常件应升级处理。"}
        self.dirs = {"viking://resources", "viking://resources/input"}
        self.acls, self.calls = {}, []
        self.index_ready, self.lose_write = True, False
        self.permissions = True

    def respond(self, r):
        body = json.loads(r.content) if r.content else {}
        query = parse_qs(r.url.query.decode())
        uri = body.get("uri") or query.get("uri", [""])[0]
        path = r.url.path.removeprefix("/api/v1")
        self.calls.append((r.method, path, uri))
        user = r.headers.get("x-openviking-user", "")
        if not self.permissions and uri.startswith("viking://resources/input"):
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
        if user.startswith("kee-denied-probe"):
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})

        def ok(value):
            return httpx.Response(200, json={"status": "ok", "result": value})

        if path == "/fs/stat":
            if uri in self.files or uri in self.dirs:
                return ok({"uri": uri, "isDir": uri in self.dirs})
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if path == "/fs/ls":
            offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [100])[0])
            children = [
                {"uri": key, "isDir": key in self.dirs}
                for key in sorted(self.dirs | set(self.files))
                if key.startswith(uri + "/") and "/" not in key[len(uri) + 1 :]
            ]
            return ok(children[offset : offset + limit])
        if path == "/content/download":
            return (
                httpx.Response(200, content=self.files[uri].encode())
                if uri in self.files
                else httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
            )
        if path == "/content/read":
            if uri in self.files:
                return ok(self.files[uri])
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if path == "/fs/mkdir":
            if uri in self.dirs:
                return httpx.Response(409, json={"error": {"code": "CONFLICT"}})
            self.dirs.add(uri)
            return ok({"uri": uri})
        if path == "/acl":
            if r.method == "PUT":
                self.acls[uri] = body
            return ok(self.acls[uri])
        if path == "/content/write":
            if body["mode"] == "create" and uri in self.files:
                return httpx.Response(409, json={"error": {"code": "CONFLICT"}})
            self.files[uri] = body["content"]
            if self.lose_write:
                self.lose_write = False
                return httpx.Response(504, json={"error": {"code": "DEADLINE_EXCEEDED"}})
            return ok({"uri": uri, "content_updated": True, "semantic_status": "queued", "vector_status": "queued"})
        if path == "/system/wait":
            return ok({"Semantic": {"failed": 0}, "Embedding": {"failed": 0}})
        if path == "/system/consistency":
            return ok({"ok": self.index_ready, "missing_record_count": 0 if self.index_ready else 1})
        if path == "/fs/rm":
            self.files = {key: v for key, v in self.files.items() if not key.startswith(uri + "/") and key != uri}
            self.dirs = {key for key in self.dirs if not key.startswith(uri + "/") and key != uri}
            return ok({"deleted": True})
        raise AssertionError("Unexpected OV business API: " + path)


def sample(source):
    ref = {k: source[k] for k in ("source_id", "uri", "revision", "digest")}
    ref.update(start=0, end=len(source["text"]), quote=source["text"])
    return {
        "contract": "kee.procedure-pack.v1",
        "pack_id": "handover",
        "version": "v1",
        "schema_revision": "receipt-v1",
        "schema_body": {"Authorization": {"type": "boolean"}},
        "methods": [
            {
                "method_id": "check",
                "title": "签收核验",
                "purpose": "核验授权后签收",
                "conditions": ["已取得授权"],
                "prohibitions": ["未经授权不得签收"],
                "exceptions": ["异常件升级处理"],
                "steps": ["核验授权", "核对收件人"],
                "completion": ["签收凭证可追溯"],
                "evidence": [ref],
                "capabilities": [],
                "cases": [],
                "related_methods": [],
            }
        ],
        "facts": [
            {
                "fact_id": "f1",
                "subject": "receipt-001",
                "predicate": "authorized",
                "value": True,
                "polarity": "positive",
                "evidence": [ref],
            }
        ],
        "gaps": [],
    }


@pytest_asyncio.fixture
async def lab(tmp_path):
    if not DSN:
        pytest.skip("KEE_R6_TEST_DSN requires an isolated PostgreSQL database")
    schema = "r6_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:10]
    c = await asyncpg.connect(DSN)
    await init_pg_schema(c, schema)
    await c.execute(f"INSERT INTO {schema}.tenants(tenant_id) VALUES('lab'),('other') ON CONFLICT DO NOTHING")
    ov = GenericOV()
    allowed = {"admin", "reader"}

    async def resolver(p):
        if p["subject"] not in allowed:
            raise HTTPException(403, "TENANT_DISABLED_OR_REVOKED")
        return {
            "url": "http://ov.test",
            "account": p["tenant"],
            "subject": p["subject"],
            "api_key": "fixture-key",
            "model": {},
        }

    ops = Operations(
        OntologyConfig(enabled=True, allow_fixture=True, state_dir=str(tmp_path), kee_query_enabled=True),
        DSN,
        resolver,
        httpx.MockTransport(ov.respond),
        schema=schema,
    )
    await ops.store.start()
    app = FastAPI()

    async def reader(r: Request):
        user = r.headers.get("x-user", "reader")
        if user not in allowed:
            raise HTTPException(401)
        return {"tenant": r.headers.get("x-tenant", "lab"), "subject": user}

    async def admin(r: Request):
        p = await reader(r)
        if p["subject"] != "admin":
            raise HTTPException(403, "TENANT_ADMIN_REQUIRED")
        return p

    install(app, ops, admin, reader)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kee.test", headers={"x-user": "admin"}
    )
    yield SimpleNamespace(ops=ops, ov=ov, client=client, p={"tenant": "lab", "subject": "admin"}, allowed=allowed)
    await client.aclose()
    await ops.store.close()
    await c.execute(f"DROP SCHEMA {schema} CASCADE")
    await c.close()


async def built(lab, key="one"):
    r = await lab.client.post(
        "/kee/v1/sources", json={"roots": ["viking://resources/input"], "readers": ["admin", "reader"]}
    )
    assert r.status_code == 201, r.text
    collection_id = r.json()["collection_id"]
    source = (await lab.ops.kee.sources.load(lab.p, collection_id))["sources"][0]
    body = {"submission_key": key, "collection_id": collection_id, "pack_id": "handover", "fixture": sample(source)}
    r = await lab.client.post("/kee/v1/methods/build", json=body)
    assert r.status_code == 202, r.text
    job = await lab.ops.store.claim()
    await lab.ops.execute(job)
    value = await lab.ops.store.get(lab.p, job["id"])
    assert value["state"] == "schema_review", value
    return value


async def approved(lab, key="one"):
    job = await built(lab, key)
    base = "/kee/v1/methods/jobs/" + job["id"]
    for path, body in [
        ("confirm-schema", {"expected_digest": job["result"]["candidate_digest"]}),
        ("prepare", {"target_uri": "viking://resources/wiki", "readers": ["admin", "reader"]}),
        ("approve", {"note": "核对授权、否定、例外与证据"}),
    ]:
        r = await lab.client.post(base + "/" + path, json=body)
        assert r.status_code == 200, r.text
    return r.json(), base


async def drain(lab, count=20):
    for _ in range(count):
        job = await lab.ops.store.claim()
        if not job:
            break
        await lab.ops.execute(job)


@pytest.mark.asyncio
async def test_full_flow_http_pg_wiki_read_feedback_withdraw(lab):
    job, base = await approved(lab)
    r = await lab.client.post(base + "/commit", json={"commit_key": "commit-one"})
    assert r.status_code == 200, r.text
    await drain(lab)
    job = await lab.ops.store.get(lab.p, job["id"])
    assert job["state"] == "published", job
    assert job["result"]["usage"]["calls"] == 0  # fixture separately recorded; Schema confirmation is deterministic
    root = job["result"]["release"]["receipt"]["release_uri"]
    assert "未经授权不得签收" in lab.ov.files[root + "/methods/check.md"]
    assert "异常件升级处理" in lab.ov.files[root + "/methods/check.md"]
    lab.client.headers["x-user"] = "reader"
    payload = {
        "target_uri": "viking://resources/wiki",
        "subject": "receipt-001",
        "predicate": "authorized",
        "expected": True,
    }
    assert (await lab.client.post("/kee/v1/ontology/check", json=payload)).json()["truth"] == "TRUE"
    payload["subject"] = "absent"
    assert (await lab.client.post("/kee/v1/ontology/check", json=payload)).json()["truth"] == "UNKNOWN"
    assert (await lab.client.post("/kee/v1/feedback", json={"note": "核验流程可读"})).status_code == 202
    assert (await lab.client.post(base + "/approve", json={"note": "forged"})).status_code == 403
    lab.client.headers["x-user"] = "admin"
    assert (await lab.client.post(base + "/withdraw")).status_code == 200
    await drain(lab)
    assert (await lab.ops.store.get(lab.p, job["id"]))["state"] == "withdrawn"
    assert root + "/methods/check.md" not in lab.ov.files
    assert (
        await lab.client.post("/kee/v1/ontology/query", json={"target_uri": "viking://resources/wiki"})
    ).status_code == 404
    assert not any("enterprise" in call[1] or "compile" in call[1] for call in lab.ov.calls)


@pytest.mark.asyncio
async def test_approval_binds_digest_and_expires(lab):
    job, base = await approved(lab)
    candidate = copy.deepcopy(job["result"]["candidate"])
    candidate["methods"][0]["title"] = "改名后的核验"
    assert (
        await lab.client.put(
            base + "/candidate", json={"expected_digest": job["result"]["candidate_digest"], "candidate": candidate}
        )
    ).status_code == 200
    assert (await lab.client.post(base + "/commit", json={"commit_key": "bad"})).status_code == 409
    job = await lab.ops.store.get(lab.p, job["id"])
    assert "approval" not in job["result"]


@pytest.mark.asyncio
async def test_response_loss_reconciles_no_rewrite_and_index_wait(lab):
    job, base = await approved(lab)
    await lab.client.post(base + "/commit", json={"commit_key": "stable"})
    await lab.ops.execute(await lab.ops.store.claim())  # private directory
    lab.ov.lose_write = True
    await lab.ops.execute(await lab.ops.store.claim())
    current = await lab.ops.store.get(lab.p, job["id"])
    assert current["state"] == "queued"
    result = current["result"]
    result.pop("retry_at", None)
    await lab.ops.store.change(lab.p, job["id"], ["queued"], "queued", result)
    lab.ov.index_ready = False
    await drain(lab, 3)
    current = await lab.ops.store.get(lab.p, job["id"])
    assert current["state"] != "published"
    writes = [call for call in lab.ov.calls if call[1] == "/content/write"]
    first_uri = writes[0][2]
    assert len([c for c in writes if c[2] == first_uri]) == 1
    assert (await lab.client.post(base + "/commit", json={"commit_key": "stable"})).status_code == 200
    assert (await lab.client.post(base + "/commit", json={"commit_key": "other"})).status_code == 409
    current["result"].pop("retry_at", None)
    await lab.ops.store.change(lab.p, job["id"], ["queued"], "queued", current["result"])
    lab.ov.index_ready = True
    await drain(lab)
    assert (await lab.ops.store.get(lab.p, job["id"]))["state"] == "published"


@pytest.mark.asyncio
async def test_cancel_and_cross_tenant_and_revocation(lab):
    job = await built(lab)
    base = "/kee/v1/methods/jobs/" + job["id"]
    lab.client.headers["x-tenant"] = "other"
    assert (await lab.client.get(base)).status_code == 404
    assert await lab.ops.kee.db.get("other", "sources/" + job["result"]["collection_id"]) is None
    lab.client.headers["x-tenant"] = "lab"
    assert (await lab.client.post(base + "/cancel")).status_code == 200
    assert (await lab.ops.store.get(lab.p, job["id"]))["state"] == "cancelled"
    assert (
        await lab.client.post(base + "/confirm-schema", json={"expected_digest": job["result"]["candidate_digest"]})
    ).status_code == 409


@pytest.mark.asyncio
async def test_source_acl_revocation_before_commit(lab):
    job, base = await approved(lab)
    lab.ov.permissions = False
    assert (await lab.client.post(base + "/commit", json={"commit_key": "denied"})).status_code == 403
    assert not [c for c in lab.ov.calls if c[1] == "/content/write"]


def test_contract_quotes_conditions_offline_and_tamper(tmp_path):
    text = "未经授权不得签收。"
    source = {
        "source_id": "s1",
        "uri": "upload:receipt.md",
        "revision": "r1",
        "digest": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
    }
    pack = ProcedurePack.model_validate(sample(source))
    verify_evidence(pack, [source])
    for path, content in render(pack).items():
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    assert validate_directory(tmp_path, [source])["valid"]
    (tmp_path / "methods/check.md").write_text("省略否定")
    assert not validate_directory(tmp_path, [source])["valid"]
    pack.methods[0].evidence[0].start = 1
    with pytest.raises(ValueError, match="SOURCE_EVIDENCE_MISMATCH"):
        verify_evidence(pack, [source])


@pytest.mark.asyncio
async def test_expired_approval_and_audience_widening(lab):
    job, base = await approved(lab)
    expired = copy.deepcopy(job["result"])
    expired["approval"]["expires_at"] = time.time() - 1
    await lab.ops.store.change(lab.p, job["id"], ["approved"], "approved", expired)
    assert (await lab.client.post(base + "/commit", json={"commit_key": "expired"})).status_code == 409
    response = await lab.client.post(
        base + "/prepare", json={"target_uri": "viking://resources/wiki", "readers": ["other"]}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_lease_recovery_fences_old_attempt(lab):
    job = await built(lab)
    await lab.ops.store.resume(lab.p, job["id"], ["schema_review"], job["result"])
    old = await lab.ops.store.claim()
    await lab.ops.store.checkpoint(old, old["result"], "running")
    await lab.ops.store.pool.execute(
        f"UPDATE {lab.ops.store.jobs} SET lease_until=now()-interval '1 second' WHERE id=$1", old["id"]
    )
    recovered = await lab.ops.store.claim()
    assert recovered["attempt"] == old["attempt"] + 1
    with pytest.raises(HTTPException, match="JOB_STATE_CHANGED"):
        await lab.ops.store.checkpoint(old, {}, "review_ready")
    await lab.ops.execute(recovered)
    assert (await lab.ops.store.get(lab.p, recovered["id"]))["state"] == "schema_review"


@pytest.mark.asyncio
async def test_same_baseline_competing_publish(lab):
    first, first_base = await approved(lab, "first")
    second, second_base = await approved(lab, "second")
    assert (await lab.client.post(first_base + "/commit", json={"commit_key": "first"})).status_code == 200
    assert (await lab.client.post(second_base + "/commit", json={"commit_key": "second"})).status_code == 200
    await drain(lab)
    a = await lab.ops.store.get(lab.p, first["id"])
    b = await lab.ops.store.get(lab.p, second["id"])
    assert a["state"] == "published"
    assert b["state"] == "failed" and b["error"] == "WIKI_TARGET_BUSY"


@pytest.mark.asyncio
async def test_query_conflict_false_budget_and_default_off(lab):
    job = await built(lab)
    candidate = copy.deepcopy(job["result"]["candidate"])
    candidate["facts"][0]["polarity"] = "negative"
    candidate["facts"].append({**candidate["facts"][0], "fact_id": "f2", "subject": "conflict", "polarity": "positive"})
    candidate["facts"].append({**candidate["facts"][0], "fact_id": "f3", "subject": "conflict"})
    await lab.ops.kee.edit(lab.p, job["id"], ProcedurePack.model_validate(candidate), job["result"]["candidate_digest"])
    job = await lab.ops.store.get(lab.p, job["id"])
    await lab.ops.kee.confirm(lab.p, job["id"], job["result"]["candidate_digest"])
    await lab.ops.kee.prepare(lab.p, job["id"], "viking://resources/wiki", ["admin", "reader"])
    await lab.ops.kee.approve(lab.p, job["id"], "核对冲突", True)
    await lab.ops.kee.commit(lab.p, job["id"], "conflicts")
    await drain(lab)
    data = {
        "target_uri": "viking://resources/wiki",
        "subject": "receipt-001",
        "predicate": "authorized",
        "expected": True,
    }
    assert (await lab.client.post("/kee/v1/ontology/check", json=data)).json()["truth"] == "FALSE"
    # JSON booleans and numbers have different meaning despite Python True == 1.
    typed = {**data, "expected": 1}
    assert (await lab.client.post("/kee/v1/ontology/check", json=typed)).json()["truth"] == "UNKNOWN"
    data["subject"] = "conflict"
    response = (await lab.client.post("/kee/v1/ontology/check", json=data)).json()
    assert response["truth"] == "UNKNOWN" and response["conflict"] is True
    data["limit"] = 1
    assert (await lab.client.post("/kee/v1/ontology/check", json=data)).json()["truncated"] is True
    lab.ops.config.kee_query_enabled = False
    assert (await lab.client.post("/kee/v1/ontology/check", json=data)).status_code == 404


@pytest.mark.asyncio
async def test_directory_pagination_unicode_hidden_limits(lab):
    for i in range(205):
        lab.ov.files[f"viking://resources/input/资料{i:04d}.md"] = "核验签收。"
    lab.ov.files["viking://resources/input/.private.md"] = "隐藏"
    lab.ov.files["viking://resources/input/image.png"] = "二进制占位"
    response = await lab.client.post(
        "/kee/v1/sources", json={"roots": ["viking://resources/input"], "readers": ["admin"]}
    )
    assert response.status_code == 201 and response.json()["count"] == 206
    lab.ops.config.source_max_files = 1
    assert (
        await lab.client.post("/kee/v1/sources", json={"roots": ["viking://resources/input"], "readers": ["admin"]})
    ).status_code == 413


@pytest.mark.asyncio
async def test_rollback_creates_new_version_and_requires_approval(lab):
    job, base = await approved(lab)
    await lab.client.post(base + "/commit", json={"commit_key": "initial"})
    await drain(lab)
    r = await lab.client.post(base + "/rollback", json={"commit_key": "rollback-one"})
    assert r.status_code == 202, r.text
    restored = r.json()
    assert restored["result"]["candidate_digest"] != job["result"]["candidate_digest"]
    assert "approval" not in restored["result"]
    assert (
        await lab.client.post("/kee/v1/methods/jobs/" + restored["id"] + "/commit", json={"commit_key": "no"})
    ).status_code == 409


@pytest.mark.asyncio
async def test_global_model_ceiling_across_tenants():
    import threading

    from teamEvolver.llm import _dispatcher_for

    current = peak = 0
    lock = threading.Lock()

    def model():
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.015)
        with lock:
            current -= 1

    a, b = _dispatcher_for("r6-a", 64, 64), _dispatcher_for("r6-b", 64, 64)
    await asyncio.gather(*[dispatcher.call(model) for dispatcher in [a, b] for _ in range(10)])
    assert 2 <= peak <= 8


@pytest.mark.asyncio
async def test_revoke_denies_existing_pages_and_cleanup(lab):
    value, _ = await approved(lab)
    await lab.client.post(f"/kee/v1/methods/jobs/{value['id']}/commit", json={"commit_key": "revoked"})
    await drain(lab)
    value = await lab.ops.store.get(lab.p, value["id"])
    uri = value["result"]["release"]["receipt"]["release_uri"]
    response = await lab.client.post("/kee/v1/sources/" + value["result"]["collection_id"] + "/revoke")
    assert response.status_code == 200, response.text
    assert response.json()["cleanup_jobs"]
    assert len(lab.ov.acls[uri]["entries"]) == 1
    await drain(lab)
    assert uri + "/index.md" not in lab.ov.files
    head = await lab.ops.kee.db.get("lab", "heads/" + digest(value["result"]["release"]["target_uri"]))
    assert not head.get("pending_job")


@pytest.mark.asyncio
async def test_internal_compile_binds_clarification(lab, monkeypatch):
    import team_ontology.kee.compile as module

    collection = await lab.ops.kee.sources.freeze(lab.p, ["viking://resources/input"], [], ["admin"])
    captured = []

    def call(payload):
        captured.append(payload)
        inputs = json.loads(payload["messages"][-1]["content"])
        return {"content": json.dumps(sample(inputs["sources"][0])), "usage": {"total_tokens": 5}}

    monkeypatch.setattr(module, "model_call", call)
    model = {"model": "test", "base_url": "http://model.test", "api_key": "fixture"}
    lab.ops.resolve = lambda p: async_settings(model, p)
    request = {
        "contract": "kee.method-build.v1",
        "pack_id": "handover",
        "collection_id": collection["collection_id"],
        "clarification": "保留否定和例外",
    }
    job = await lab.ops.store.submit(lab.p, "clarified", request)
    await lab.ops.execute(await lab.ops.store.claim())
    done = await lab.ops.store.get(lab.p, job["id"])
    assert done["state"] == "schema_review", done
    assert json.loads(captured[0]["messages"][-1]["content"])["clarification"] == "保留否定和例外"
    await lab.ops.kee.confirm(lab.p, job["id"], done["result"]["candidate_digest"])
    assert len(captured) == 1


async def async_settings(model, p):
    return {
        "url": "http://ov.test",
        "account": p["tenant"],
        "subject": p["subject"],
        "api_key": "fixture-key",
        "model": model,
    }


@pytest.mark.asyncio
async def test_memory_internal_compile_reconciles_without_second_model(tmp_path, monkeypatch):
    import team_memory.internal_compile as module
    from team_memory.aggregation.state import AggregationState
    from team_memory.compile_client import CompileClient
    from team_memory.internal_compile import MemoryCompilePort

    ov = GenericOV()
    source_root, target = "viking://resources/input", "viking://resources/memory"
    ov.files["viking://agent/skills/memory/SKILL.md"] = "聚合已有 Memory，保留来源。"
    client = CompileClient("http://ov.test", "lab", "admin", "fixture")
    client._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(ov.respond), base_url="http://ov.test")

    async def inspect(_self, kinds, **kwargs):
        root = kwargs["source_root"]
        files = tuple(SimpleNamespace(relative_path=k[len(root) + 1 :]) for k in ov.files if k.startswith(root + "/"))
        return SimpleNamespace(files=files)

    monkeypatch.setattr(module.DeterministicStagingClient, "inspect", inspect)
    calls = []

    def model(payload):
        calls.append(payload)
        return {
            "content": json.dumps({"files": [{"path": "overview.md", "content": "# 团队 Memory\n\n核验授权。"}]}),
            "usage": {"total_tokens": 20},
        }

    monkeypatch.setattr(module, "model_call", model)
    config = SimpleNamespace(llm_model_id="model-stub", llm_api_key="fixture", llm_api_base="http://model.test")
    state_path = tmp_path / "memory.json"
    stale = AggregationState.load(state_path, "lab")
    port = MemoryCompilePort(config, state_path)
    kwargs = dict(source_uris=[source_root], target_uri=target, skill_uri="viking://agent/skills/memory", reason="聚合")
    ov.index_ready = False
    first = await port.run_batch(client, **kwargs)
    assert not first["ok"]
    assert first["stderr"] == "MEMORY_INDEX_WAIT"
    stale.save(skill_fingerprint="frozen-skill")
    assert AggregationState.load(state_path, "lab").metadata["internal_compile"]
    writes = [call for call in ov.calls if call[:2] == ("POST", "/content/write")]
    ov.index_ready = True
    second = await MemoryCompilePort(config, state_path).run_batch(client, **kwargs)
    assert second["ok"], second
    assert len(calls) == 1
    assert [call for call in ov.calls if call[:2] == ("POST", "/content/write")] == writes
    assert (await port.status(client, second["result"]["task_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_real_reference_checks_are_tenant_scoped(lab):
    from team_ontology.kee.references import validate_references

    value = await built(lab)
    payload = copy.deepcopy(value["result"]["candidate"])
    payload["methods"][0]["capabilities"] = [{"skill_id": "skill-real", "version": "2", "description": "授权核验"}]
    with pytest.raises(HTTPException, match="CAPABILITY_SKILL_VERSION_NOT_FOUND"):
        await validate_references(lab.ops.kee.db, "lab", ProcedurePack.model_validate(payload))
    async with lab.ops.kee.db.transaction("lab") as conn:
        await conn.execute(
            f"INSERT INTO {lab.ops.kee.db.table}(tenant_id,key,content) VALUES($1,$2,$3)",
            "lab",
            "evolve_skill_registry.json",
            b'{"check":{"skill_id":"skill-real","version":2}}',
        )
    await validate_references(lab.ops.kee.db, "lab", ProcedurePack.model_validate(payload))
    with pytest.raises(HTTPException):
        await validate_references(lab.ops.kee.db, "other", ProcedurePack.model_validate(payload))


@pytest.mark.asyncio
async def test_history_migration_dry_run_idempotent_import_and_rollback(lab, tmp_path, monkeypatch):
    import importlib.util

    path = Path(__file__).parents[1] / "scripts/kee_migrate.py"
    spec = importlib.util.spec_from_file_location("kee_migrate_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("KEE_MIGRATE_LAB_DSN", DSN)
    inputs, receipt = tmp_path / "history.ndjson", tmp_path / "receipt.json"
    inputs.write_text(
        json.dumps(
            {
                "table": "ov_ontology_commits",
                "record": {"tenant": "lab", "generation": 7, "receipt": {"withdrawn": True}},
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        command="import",
        dsn_env="KEE_MIGRATE_LAB_DSN",
        account="lab",
        tenant="lab",
        schema=lab.ops.store.schema,
        input=inputs,
        receipt=receipt,
        apply=False,
    )
    await module.run(args)
    key = json.loads(receipt.read_text())["operations"][0]["key"][7:]
    assert await lab.ops.kee.db.get("lab", key) is None
    args.apply = True
    await module.run(args)
    history = await lab.ops.kee.db.get("lab", key)
    assert history["data"]["record"]["generation"] == 7
    assert history["data"]["record"]["receipt"]["withdrawn"]
    await module.run(args)
    assert json.loads(receipt.read_text())["operations"][0]["action"] == "unchanged"
    args.command = "rollback"
    args.apply = False
    await module.run(args)
    assert await lab.ops.kee.db.get("lab", key)
    args.apply = True
    await module.run(args)
    assert await lab.ops.kee.db.get("lab", key) is None


@pytest.mark.asyncio
async def test_failed_model_retries_consume_persistent_budget(lab, monkeypatch):
    import team_ontology.kee.compile as module

    collection = await lab.ops.kee.sources.freeze(lab.p, ["viking://resources/input"], [], ["admin"])
    calls = []

    def fail(payload):
        calls.append(1)
        raise HTTPException(502, "COMPILE_MODEL_FAILED")

    monkeypatch.setattr(module, "model_call", fail)
    lab.ops.resolve = lambda p: async_settings(
        {"model": "stub", "base_url": "http://model.test", "api_key": "fixture"}, p
    )
    job = await lab.ops.store.submit(
        lab.p,
        "model-budget",
        {"contract": "kee.method-build.v1", "pack_id": "handover", "collection_id": collection["collection_id"]},
    )
    for attempt in range(3):
        await lab.ops.execute(await lab.ops.store.claim())
        value = await lab.ops.store.get(lab.p, job["id"])
        assert value["state"] == "failed"
        if attempt < 2:
            await lab.ops.kee.retry(lab.p, job["id"])
    assert len(calls) == 2
    assert value["error"] == "COMPILE_MODEL_RETRY_BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_publication_cannot_widen_original_acl(lab):
    respond = lab.ov.respond

    def scoped(request):
        if request.headers.get("x-openviking-user") == "reader" and request.url.path == "/api/v1/content/read":
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
        return respond(request)

    lab.ops.transport = httpx.MockTransport(scoped)
    response = await lab.client.post(
        "/kee/v1/sources", json={"roots": ["viking://resources/input"], "readers": ["admin", "reader"]}
    )
    assert response.status_code == 403


def test_isolated_worker_does_not_inherit_ov_keys(monkeypatch):
    import team_ontology.kee.compile as module

    monkeypatch.setenv("TEAMEVOLVER_OV_API_KEY", "do-not-forward")
    monkeypatch.setenv("OV_ROOT_API_KEY", "do-not-forward")
    captured = {}

    class Child:
        pid = 991900
        returncode = 0

        def communicate(self, *args, **kwargs):
            return json.dumps({"content": "{}", "usage": {}}), ""

    def launch(args, **kwargs):
        captured.update(kwargs)
        return Child()

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    module.model_call({"model": {}, "messages": []})
    assert "TEAMEVOLVER_OV_API_KEY" not in captured["env"]
    assert "OV_ROOT_API_KEY" not in captured["env"]


@pytest.mark.asyncio
async def test_source_owner_must_remain_a_reader(lab):
    response = await lab.client.post(
        "/kee/v1/sources", json={"roots": ["viking://resources/input"], "readers": ["reader"]}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "SOURCE_OWNER_NOT_READER"


@pytest.mark.asyncio
async def test_native_r6_retires_legacy_writes_without_calling_ov(lab):
    from team_ontology.api import install as install_legacy

    app = FastAPI()

    async def principal():
        return lab.p

    async def unavailable(_):
        raise AssertionError("retired writes must not call OV")

    config = OntologyConfig(enabled=True, kee_legacy_writes_enabled=False, state_dir="/tmp/kee-r6-retired-test")
    install_legacy(app, principal, config=config, dsn=DSN, resolver=unavailable, schema=lab.ops.store.schema)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kee.test") as client:
        for path in ["/access", "/snapshots/freeze", "/schemas", "/rollback-candidates", "/jobs/old/commit"]:
            response = await client.request("PUT" if path == "/access" else "POST", "/te/enterprise/v1" + path, json={})
            assert response.status_code == 410
            assert response.json()["detail"] == "KEE_R6_USE_METHOD_BUILD"
        capabilities = await client.get("/te/enterprise/v1/capabilities")
        assert capabilities.json()["asset_backend"] == "generic-wiki"
