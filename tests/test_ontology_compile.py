"""Native Compile contract tests with deterministic model output; never a real-model success claim."""

import copy
import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException

from team_ontology.compile_contract import (
    BUILD_CONTRACT,
    OUTPUT_CONTRACT,
    SOURCE_CONTRACT,
    ConfirmSchemaRequest,
    candidate_from_drafts,
    check_coverage,
)
from team_ontology.compile_flow import CompileFlow, uri
from team_ontology.config import OntologyConfig
from team_ontology.service import Operations
from team_ontology.wire import Source, digest

P = {"tenant": "tenant-a", "subject": "frank"}
SCHEMA = {
    "revision": "example-v1",
    "entity_types": ["Shipment"],
    "predicates": {
        "status": {"subject_type": "Shipment", "value_type": "string", "object_type": None, "required_qualifiers": []}
    },
    "rules": [],
    "tools": [],
    "evidence_slots": ["status"],
    "issue_pack_revision": "example-v1",
}


class MemoryStore:
    def __init__(self):
        self.rows = {}

    async def submit(self, p, key, request):
        for row in self.rows.values():
            if (row["tenant"], row["subject"], row["key"]) == (p["tenant"], p["subject"], key):
                if row["request"] != request:
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                return copy.deepcopy(row)
        job = dict(
            id="ont_" + f"{len(self.rows) + 1:024x}",
            **p,
            key=key,
            request=request,
            result={},
            attempt=0,
            state="queued",
        )
        self.rows[job["id"]] = copy.deepcopy(job)
        return job

    async def get(self, p, identifier):
        row = self.rows[identifier]
        if (row["tenant"], row["subject"]) != (p["tenant"], p["subject"]):
            raise HTTPException(404, "JOB_NOT_FOUND")
        return copy.deepcopy(row)

    async def list(self, p):
        return [
            copy.deepcopy(r) for r in self.rows.values() if r["tenant"] == p["tenant"] and r["subject"] == p["subject"]
        ]

    async def checkpoint(self, job, result, state="queued", error=None):
        row = self.rows[job["id"]]
        if row["attempt"] != job["attempt"] or row["state"] != "running":
            raise HTTPException(409, "JOB_STATE_CHANGED")
        row.update(result=copy.deepcopy(result), state=state, error=error)
        return copy.deepcopy(row)

    async def resume(self, p, identifier, allowed, result):
        row = await self.get(p, identifier)
        assert row["state"] in allowed
        self.rows[identifier].update(result=copy.deepcopy(result), state="queued", error=None)
        return await self.get(p, identifier)

    async def audit(self, *args):
        pass


class OV:
    def __init__(self):
        self.files = {
            "viking://resources/wiki/index.md": "signed on 2026-09-01 " * 6000,
            "viking://resources/wiki/sub/body.md": "signed on 2026-09-01",
            "viking://resources/wiki/skip.png": "image",
        }
        self.dirs = {"viking://resources/wiki", "viking://resources/wiki/sub"}
        self.frozen, self.tasks, self.acls, self.calls = {}, {}, {}, []
        self.published_schema = None
        self.lose_response, self.forbidden, self.leak = False, set(), False
        self.deny_acl_management = False

    def reply(self, data, status=200, native=True):
        return httpx.Response(status, json={"status": "ok", "result": data} if native else data)

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        params = parse_qs(request.url.query.decode())
        target = params.get("uri", [body.get("uri")])[0]
        user = request.headers.get("x-openviking-user")
        role = request.headers.get("x-openviking-role")
        self.calls.append((request.method, path, body, user))
        assert request.headers["authorization"] == "Bearer backend-secret"
        if user == "te-ontology-compiler" or user.startswith("te-ontology-probe"):
            assert role == "user", "Compile and isolation probes must not gain admin access"
        if user.startswith("te-ontology-probe") and not self.leak:
            # The deployed OV answers a probe's semantic search with 404 rather
            # than 403; file-level reads still deny with 403.
            if path.endswith("/search/find"):
                return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "Resource not found"}})
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
        if target in self.forbidden:
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
        if path.endswith("/ontology/capabilities"):
            return self.reply({"tenant": P["tenant"], "subject": P["subject"], "generation": 0,
                               "schema_revision": self.published_schema}, native=False)
        if path.endswith("/schemas"):
            return self.reply({}, native=False)
        if path.endswith("/snapshots/freeze"):
            source = Source(
                source_id=body["source_id"],
                revision=digest(self.files[body["uri"]]),
                text=self.files[body["uri"]],
                readers=body["readers"],
                origin_uri=body["uri"],
            ).model_dump(mode="json")
            self.frozen[source["source_id"]] = source
            return self.reply(
                {k: source[k] for k in ("source_id", "revision")} | {"digest": digest(source)}, native=False
            )
        if path.endswith("/snapshots/read"):
            return self.reply({"sources": [self.frozen[r["source_id"]] for r in body["sources"]]}, native=False)
        if path.endswith("/fs/stat"):
            return self.reply({"uri": target, "isDir": target in self.dirs})
        if path.endswith("/fs/mkdir"):
            if target in self.dirs or target in self.files:
                return httpx.Response(409, json={"error": {"code": "CONFLICT"}})
            # OV applies a mkdir acl argument atomically with directory creation;
            # a denied ACL administration also denies the atomic grant.
            if isinstance(body.get("acl"), dict) and body["acl"].get("entries"):
                if self.deny_acl_management:
                    return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
                self.acls[body["uri"]] = {
                    "acl_mode": body["acl"].get("acl_mode", "restricted"),
                    "effective_entries": body["acl"]["entries"],
                }
            self.dirs.add(body["uri"])
            return self.reply({"uri": body["uri"]})
        if path.endswith("/fs/ls"):
            all_paths = sorted(self.dirs | self.files.keys())
            children = [
                {"uri": u, "isDir": u in self.dirs}
                for u in all_paths
                if u.startswith(target + "/") and "/" not in u[len(target) + 1 :]
            ]
            start = int(params.get("offset", [0])[0])
            return self.reply(children[start : start + int(params.get("limit", [100])[0])])
        if path.endswith("/acl"):
            # A new resources directory grants no implicit ACL management to
            # its creator. OV requires admin/root or an existing manage grant.
            grant = {"principal": "user:" + user, "level": "manage"}
            granted = grant in self.acls.get(target, {}).get("effective_entries", [])
            if self.deny_acl_management or (role not in {"admin", "root"} and not granted):
                return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
            if request.method == "PUT":
                self.acls[body["uri"]] = {"acl_mode": "restricted", "effective_entries": body["entries"]}
            return self.reply(self.acls[target])
        if path.endswith("/content/write"):
            assert "te_ontology_compile" in target
            assert any(target.startswith(a + "/") for a in self.acls)
            if target in self.files or target in self.dirs:
                return httpx.Response(409, json={"error": {"code": "CONFLICT"}})
            self.files[target] = body["content"]
            return self.reply({})
        if path.endswith("/content/read"):
            if target.endswith("ontology-extraction-v1/SKILL.md"):
                return self.reply(Path("team_ontology/skills/ontology-extraction-v1/SKILL.md").read_text())
            return self.reply(self.files[target])
        if path.endswith("/compile"):
            assert user == "te-ontology-compiler"
            args = json.loads(body["instruction"])
            self.output(body["to"], args["sources"], args.get("schema"))
            task = {"task_id": "cmp_" + str(len(self.tasks)), "status": "completed", "meta": {"request": body}}
            self.tasks[task["task_id"]] = task
            if self.lose_response:
                self.lose_response = False
                raise httpx.ReadTimeout("response lost")
            return self.reply(task)
        if path.endswith("/tasks"):
            return self.reply(list(self.tasks.values()))
        if "/tasks/" in path:
            return self.reply(self.tasks[path.split("/")[4]])
        if path.endswith("/artifacts/sessions"):
            return self.reply({}, native=False)
        if path.endswith("/artifacts"):
            return self.reply(
                {"artifact_id": "artifact-test", "candidate_digest": digest(body["candidate"])}, native=False
            )
        raise AssertionError(path)

    def output(self, target, sources, schema=None):
        rows, coverage = [], []
        for i, ref in enumerate(sources):
            sid = ref["source_id"]
            rows.extend(
                [
                    {
                        "kind": "entities",
                        "value": {
                            "entity_id": f"e{i}",
                            "entity_type": "Shipment",
                            "authority_namespace": "test",
                            "external_id": sid,
                            "label": sid,
                        },
                    },
                    {
                        "kind": "evidence",
                        "value": {
                            "evidence_id": f"v{i}",
                            "source_id": sid,
                            "quote": "signed on 2026-09-01",
                            "start": 0,
                            "end": 20,
                        },
                    },
                    {
                        "kind": "assertions",
                        "value": {
                            "assertion_id": f"a{i}",
                            "subject": f"e{i}",
                            "predicate": "status",
                            "value": "signed",
                            "valid_from": "2026-09-01T00:00:00Z",
                            "support_sets": [[f"v{i}"]],
                        },
                    },
                ]
            )
            coverage.append({"source_id": sid, "status": "complete", "ranges": [[0, ref["characters"]]]})
        self.files[target + "/result-manifest.json"] = json.dumps(
            {"contract": OUTPUT_CONTRACT, "draft_files": ["drafts/1.jsonl"]}
        )
        self.files[target + "/schema-proposal.json"] = json.dumps(schema or SCHEMA)
        self.files[target + "/coverage.json"] = json.dumps(coverage)
        self.files[target + "/drafts/1.jsonl"] = "\n".join(json.dumps(r) for r in rows)


@pytest.fixture
def env(tmp_path):
    remote = OV()

    async def resolve(p):
        return {"url": "http://ov.test", "account": p["tenant"], "subject": p["subject"], "api_key": "backend-secret"}

    ops = Operations(
        OntologyConfig(state_dir=str(tmp_path)), "postgresql://unused", resolve, httpx.MockTransport(remote)
    )
    ops.store = MemoryStore()
    return ops, CompileFlow(ops), remote


async def drive(ops, flow, identifier, max_steps=60):
    for _ in range(max_steps):
        row = ops.store.rows[identifier]
        if row["state"] != "queued":
            return copy.deepcopy(row)
        row.update(state="running", attempt=row["attempt"] + 1)
        await flow.step(copy.deepcopy(row))
    pytest.fail("step budget exceeded")


async def drive_until(ops, flow, identifier, stage, max_steps=60):
    for _ in range(max_steps):
        row = ops.store.rows[identifier]
        if row["state"] != "queued":
            return copy.deepcopy(row)
        if row["result"].get("stage") == stage:
            return copy.deepcopy(row)
        row.update(state="running", attempt=row["attempt"] + 1)
        await flow.step(copy.deepcopy(row))
    pytest.fail("step budget exceeded")


async def collection(env):
    ops, flow, _ = env
    row = await ops.store.submit(
        P,
        "sources",
        {"contract": SOURCE_CONTRACT, "roots": ["viking://resources/wiki/"], "readers": ["frank"], "references": []},
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "sources_ready", row
    return row


async def build(env):
    ops, flow, _ = env
    group = await collection(env)
    row = await ops.store.submit(
        P, "build", {"contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0}
    )
    return await drive(ops, flow, row["id"])


@pytest.mark.asyncio
async def test_directory_schema_confirm_no_second_compile_and_partial_coverage(env):
    ops, flow, remote = env
    row = await build(env)
    assert row["state"] == "schema_review", row
    assert len(row["result"]["refs"]) == 2
    assert len(row["result"]["gaps"]) == 1
    assert len(remote.tasks) == 1
    await flow.confirm(
        P, row["id"], ConfirmSchemaRequest(schema_body=SCHEMA, expected_digest=row["result"]["output_digest"])
    )
    ready = await drive(ops, flow, row["id"])
    assert ready["state"] == "review_ready", ready
    assert len(remote.tasks) == 1
    assert len(ready["result"]["candidate"]["assertions"]) == 2
    assert ready["result"]["manifest"]["mode"] == "delta"
    assert all(ref["digest"] for ref in ready["result"]["manifest"]["sources"])


@pytest.mark.asyncio
async def test_response_lost_reconciles_without_resubmission(env):
    ops, flow, remote = env
    remote.lose_response = True
    row = await build(env)
    assert row["state"] == "compile_unknown", row
    await flow.retry(P, row["id"])
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "schema_review", row
    assert len([c for c in remote.calls if c[1].endswith("/compile")]) == 1


@pytest.mark.asyncio
async def test_acl_leak_prevents_writing_source_text(env):
    _, _, remote = env
    remote.leak = True
    row = await build(env)
    assert row["state"] == "failed"
    assert row["error"] == "COMPILE_WORKSPACE_NOT_PRIVATE"
    assert not any(c[1].endswith("/content/write") for c in remote.calls)


@pytest.mark.asyncio
async def test_workspace_acl_bootstraps_atomically_on_mkdir(env):
    _, _, remote = env
    row = await build(env)
    assert row["state"] == "schema_review", row
    # ACLs bootstrap atomically with directory creation: the backend identity
    # carries the acl argument on mkdir. Fresh directories skip the ACL
    # readback entirely (the mode marker lands with asynchronous indexing);
    # isolation is proven by the denial probe and the compiler's own writes.
    grants = [c for c in remote.calls if c[:2] == ("POST", "/api/v1/fs/mkdir") and c[2].get("acl")]
    assert grants
    assert all(c[3] == P["subject"] for c in grants)
    assert not any(c[:2] == ("PUT", "/api/v1/acl") for c in remote.calls)
    assert not any(c[:2] == ("GET", "/api/v1/acl") for c in remote.calls)
    assert all(acl["effective_entries"] == [
        {"principal": "user:te-ontology-compiler", "level": "manage"}
    ] for acl in remote.acls.values())


@pytest.mark.asyncio
async def test_acl_management_denied_stops_before_content_or_compile(env):
    _, _, remote = env
    remote.deny_acl_management = True
    row = await build(env)
    assert row["state"] == "failed" and row["error"] == "PERMISSION_DENIED"
    assert not any(c[1].endswith(("/content/write", "/compile")) for c in remote.calls)


@pytest.mark.asyncio
async def test_acl_lag_is_checkpointed_and_recovers_without_new_round(env, monkeypatch):
    ops, flow, remote = env
    group = await collection(env)
    row = await ops.store.submit(P, "acl-lag", {
        "contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0})
    now, ready = 1000.0, False
    monkeypatch.setattr("team_ontology.compile_flow.time.time", lambda: now)

    def upstream(request):
        if not ready and request.method == "PUT" and request.url.path.endswith("/acl"):
            return httpx.Response(400, json={"error": {
                "code": "INVALID_ARGUMENT", "message": "ACL target has no context record; index it first"}})
        return remote(request)

    ops.transport = httpx.MockTransport(upstream)
    async def tick():
        row = ops.store.rows[row_id]
        row.update(state="running", attempt=row["attempt"] + 1)
        return await flow.step(copy.deepcopy(row))

    row_id = row["id"]
    # Reused workspace: directories already exist and the input ACL drifted, so
    # protect() re-applies it through the ACL PUT — the path that still races
    # the directory's asynchronous indexing on OV.
    root = "viking://resources/te_ontology_compile/" + digest([P["tenant"], P["subject"]])[:24]
    folder = f"{root}/{row_id}/0"
    correct = {"acl_mode": "restricted", "effective_entries": [
        {"principal": "user:te-ontology-compiler", "level": "manage"}]}
    remote.dirs.update({root, f"{root}/{row_id}", folder, folder + "/input", folder + "/output"})
    remote.acls.update({
        root: correct,
        folder + "/input": {"acl_mode": "restricted", "effective_entries": [
            {"principal": "user:te-ontology-compiler", "level": "manage"},
            {"principal": "user:frank", "level": "manage"}]},
        folder + "/output": correct,
    })
    waiting = await tick()
    assert waiting["state"] == "queued" and waiting["result"]["round"] == 0
    assert waiting["result"]["acl_wait"]["failures"] == 1
    calls = len(remote.calls)
    await tick()
    assert len(remote.calls) == calls  # No I/O before the persisted retry time.
    assert not any(c[1].endswith(("/content/write", "/compile")) for c in remote.calls)
    now += 60  # A real index queue may take longer than the old 11.5 second window.
    ready = True
    flow = CompileFlow(ops)  # A new worker resumes the persisted checkpoint.
    recovered = await tick()
    assert recovered["result"]["protected"]
    assert "acl_wait" not in recovered["result"]
    completed = await drive(ops, flow, row_id)
    assert completed["state"] == "schema_review"
    assert completed["result"]["round"] == 0 and len(remote.tasks) == 1


@pytest.mark.asyncio
async def test_acl_wait_exhaustion_and_manual_retry_keep_workspace(env, monkeypatch):
    ops, flow, remote = env
    group = await collection(env)
    row = await ops.store.submit(P, "acl-timeout", {
        "contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0})
    row.update(state="running", result={"stage": "stage_inputs", "round": 2, "refs": group["result"]["refs"],
        "gaps": [], "staged": 0, "acl_wait": {"started_at": 1000, "retry_at": 1305, "failures": 10}})
    ops.store.rows[row["id"]] = copy.deepcopy(row)
    monkeypatch.setattr("team_ontology.compile_flow.time.time", lambda: 1301)
    failed = await flow.step(row)
    assert failed["state"] == "failed" and failed["error"] == "COMPILE_ACL_WAIT_EXHAUSTED"
    assert not remote.tasks
    resumed = await flow.retry(P, row["id"])
    assert resumed["result"]["round"] == 2
    assert "acl_wait" not in resumed["result"]
    assert not resumed["result"].get("history")


@pytest.mark.asyncio
async def test_existing_workspace_is_reused_on_second_build(env):
    ops, flow, remote = env
    first = await build(env)
    assert first["state"] == "schema_review", first
    second = await ops.store.submit(P, "another-build", first["request"])
    second = await drive(ops, flow, second["id"])
    assert second["state"] == "schema_review", second
    assert len(remote.tasks) == 2


@pytest.mark.asyncio
async def test_workspace_file_collision_stops_before_acl_or_content(env):
    _, _, remote = env
    remote.files["viking://resources/te_ontology_compile"] = "existing business file"
    row = await build(env)
    assert row["state"] == "failed" and row["error"] == "COMPILE_PATH_TYPE_CONFLICT"
    assert not any(c[1].endswith(("/acl", "/content/write", "/compile")) for c in remote.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(403, "PERMISSION_DENIED"), (404, "NOT_FOUND"), (503, "UNAVAILABLE")])
async def test_directory_conflict_stat_errors_are_not_success(env, status, code):
    ops, flow, remote = env
    target = "viking://resources/te_ontology_compile"
    remote.dirs.add(target)

    def upstream(request):
        if request.url.path.endswith("/fs/stat"):
            return httpx.Response(status, json={"error": {"code": code}})
        return remote(request)

    ops.transport = httpx.MockTransport(upstream)
    with pytest.raises(HTTPException) as error:
        await flow.mkdir(P, target)
    assert (error.value.status_code, error.value.detail) == (status, code)


@pytest.mark.asyncio
@pytest.mark.parametrize("stat", [None, [], {}, {"uri": "viking://resources/wrong", "isDir": True},
                                 {"uri": "viking://resources/te_ontology_compile", "isDir": "true"}])
async def test_directory_conflict_requires_valid_stat(env, stat):
    ops, flow, remote = env
    target = "viking://resources/te_ontology_compile"
    remote.dirs.add(target)
    ops.transport = httpx.MockTransport(
        lambda request: remote.reply(stat) if request.url.path.endswith("/fs/stat") else remote(request)
    )
    with pytest.raises(HTTPException, match="COMPILE_INVALID_STAT"):
        await flow.mkdir(P, target)


@pytest.mark.asyncio
async def test_directory_conflict_stat_timeout_stays_failed(env):
    ops, flow, remote = env
    target = "viking://resources/te_ontology_compile"
    remote.dirs.add(target)

    def upstream(request):
        if request.url.path.endswith("/fs/stat"):
            raise httpx.ReadTimeout("stat timeout")
        return remote(request)

    ops.transport = httpx.MockTransport(upstream)
    with pytest.raises(HTTPException, match="OV_UNAVAILABLE_RECONCILE_BY_KEY"):
        await flow.mkdir(P, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,error", [("same bytes", None), ("different bytes", "COMPILE_INPUT_CHANGED"),
                                         (None, "COMPILE_PATH_TYPE_CONFLICT")])
async def test_input_create_conflict_reuses_only_identical_file(env, initial, error):
    _, flow, remote = env
    folder = "viking://resources/te_ontology_compile/test"
    target = folder + "/00000.txt"
    await flow.protect(P, folder)
    if initial is None:
        remote.dirs.add(target)
    else:
        remote.files[target] = initial
    if error:
        with pytest.raises(HTTPException, match=error):
            await flow.write(P, target, "same bytes")
    else:
        await flow.write(P, target, "same bytes")
    assert remote.files.get(target) == initial
    writes = [c for c in remote.calls if c[1].endswith("/content/write")]
    assert len(writes) == 1 and writes[0][2]["mode"] == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_input_staging_does_not_wait_for_index_and_reconciles_written_bytes(env, lost_response):
    ops, flow, remote = env
    folder = "viking://resources/te_ontology_compile/test"
    await flow.protect(P, folder)
    target = folder + "/00000.txt"

    def upstream(request):
        response = remote(request)
        if request.url.path.endswith("/content/write"):
            assert json.loads(request.content)["wait"] is False
            if lost_response:
                raise httpx.ReadTimeout("response lost after persistence")
        if request.url.path.endswith("/content/read"):
            assert parse_qs(request.url.query.decode())["raw"] == ["true"]
        return response

    ops.transport = httpx.MockTransport(upstream)
    await flow.write(P, target, "frozen 原文\n")
    assert remote.files[target] == "frozen 原文\n"
    assert len([c for c in remote.calls if c[1].endswith("/content/write")]) == 1


@pytest.mark.asyncio
async def test_uncertain_input_write_cannot_accept_changed_bytes(env):
    ops, flow, remote = env
    folder = "viking://resources/te_ontology_compile/test"
    await flow.protect(P, folder)

    def upstream(request):
        if request.url.path.endswith("/content/write"):
            remote.files[json.loads(request.content)["uri"]] = "different bytes"
            return httpx.Response(504, json={"error": {"code": "DEADLINE_EXCEEDED"}})
        return remote(request)

    ops.transport = httpx.MockTransport(upstream)
    with pytest.raises(HTTPException, match="COMPILE_INPUT_CHANGED"):
        await flow.write(P, folder + "/00000.txt", "original bytes")


@pytest.mark.asyncio
async def test_unreadable_document_skipped(env):
    _, _, remote = env
    remote.forbidden.add("viking://resources/wiki/sub/body.md")
    row = await collection(env)
    assert len(row["result"]["refs"]) == 1
    assert {g["code"] for g in row["result"]["gaps"]} == {"PERMISSION_DENIED", "UNSUPPORTED_DOCUMENT"}


@pytest.mark.asyncio
async def test_cross_tenant_denied(env):
    ops, _, _ = env
    row = await collection(env)
    with pytest.raises(HTTPException, match="JOB_NOT_FOUND"):
        await ops.store.get({**P, "tenant": "tenant-b"}, row["id"])


@pytest.mark.parametrize(
    "target",
    ["https://example.com/x", "viking://resources/a/../b", "viking://resources/a/%2e%2e/b", "viking://resources/a?x=y"],
)
def test_uri_scope(target):
    with pytest.raises(HTTPException):
        uri(target)


@pytest.mark.asyncio
async def test_size_budget_and_hidden_files(env):
    ops, flow, remote = env
    ops.config.source_max_bytes = 100
    remote.files["viking://resources/wiki/.overview.md"] = "secret"
    row = await ops.store.submit(
        P, "sources", {"contract": SOURCE_CONTRACT, "roots": ["viking://resources/wiki"], "readers": ["frank"]}
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "failed" and row["error"] == "SOURCE_TOTAL_SIZE_LIMIT"
    assert not any(f["uri"].endswith(".overview.md") for f in row["result"]["files"])


@pytest.mark.asyncio
async def test_schema_mapping_no_model_and_semantic_change_recompiles(env):
    ops, flow, remote = env
    row = await build(env)
    schema = copy.deepcopy(SCHEMA)
    schema["entity_types"] = ["Parcel"]
    schema["predicates"]["status"]["subject_type"] = "Parcel"
    schema["revision"] = "parcel-v1"
    await flow.confirm(
        P,
        row["id"],
        ConfirmSchemaRequest(
            schema_body=schema, type_mapping={"Shipment": "Parcel"}, expected_digest=row["result"]["output_digest"]
        ),
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "review_ready", row
    assert row["result"]["candidate"]["entities"][0]["entity_type"] == "Parcel"
    assert len(remote.tasks) == 1


@pytest.mark.asyncio
async def test_output_path_escape_rejected(env):
    _, flow, remote = env
    remote.files["viking://resources/out/result-manifest.json"] = json.dumps(
        {"contract": OUTPUT_CONTRACT, "draft_files": ["drafts/../secret.jsonl"]}
    )
    remote.files["viking://resources/out/schema-proposal.json"] = json.dumps(SCHEMA)
    remote.files["viking://resources/out/coverage.json"] = "[]"
    with pytest.raises(HTTPException, match="COMPILE_OUTPUT_PATH_INVALID"):
        await flow.collect(P, "viking://resources/out")


@pytest.mark.asyncio
async def test_revision_bound_cache(env):
    ops, flow, remote = env
    ops.config.compile_model_revision = "synthetic-v1"
    row = await build(env)
    second = await ops.store.submit(P, "another-build", row["request"])
    second = await drive(ops, flow, second["id"])
    assert second["state"] == "schema_review", second
    assert second["result"]["cache_hit"] == row["id"]
    assert len(remote.tasks) == 1


@pytest.mark.parametrize("ranges", [[[0, 2], [3, 5]], [[0, 3], [2, 5]], [[1, 5]], [[0, 6]]])
def test_coverage_gaps_and_overlap_rejected(ranges):
    with pytest.raises(HTTPException):
        check_coverage(
            [{"source_id": "s", "status": "complete", "ranges": ranges}], [{"source_id": "s", "text": "hello"}]
        )


@pytest.mark.asyncio
async def test_pagination_covers_more_than_one_page_and_duplicate_roots(env):
    ops, flow, remote = env
    remote.files = {f"viking://resources/wiki/{i:03d}.md": "signed on 2026-09-01" for i in range(105)}
    remote.dirs = {"viking://resources/wiki"}
    row = await ops.store.submit(
        P,
        "many",
        {
            "contract": SOURCE_CONTRACT,
            "roots": ["viking://resources/wiki", "viking://resources/wiki/"],
            "readers": ["frank"],
        },
    )
    row = await drive(ops, flow, row["id"], max_steps=115)
    assert row["state"] == "sources_ready", row
    assert len(row["result"]["refs"]) == 105


@pytest.mark.asyncio
async def test_semantic_change_queues_bounded_full_recompile(env):
    ops, flow, remote = env
    row = await build(env)
    changed = copy.deepcopy(SCHEMA)
    changed["revision"] = "v2"
    changed["predicates"]["new"] = {"subject_type": "Shipment", "value_type": "string"}
    await flow.confirm(
        P, row["id"], ConfirmSchemaRequest(schema_body=changed, expected_digest=row["result"]["output_digest"])
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "review_ready", row
    assert len(remote.tasks) == 2
    assert len({t["meta"]["request"]["to"] for t in remote.tasks.values()}) == 2


@pytest.mark.asyncio
async def test_late_result_cannot_advance_cancelled_job(env):
    ops, flow, _ = env
    row = await collection(env)
    stale = copy.deepcopy(row)
    stale.update(state="running", attempt=1)
    ops.store.rows[row["id"]].update(state="cancelled", attempt=1)
    with pytest.raises(HTTPException, match="JOB_STATE_CHANGED"):
        await flow.save(stale, {"stage": "sources_ready"}, "sources_ready")


@pytest.mark.asyncio
async def test_model_revision_change_does_not_reuse_cache(env):
    ops, flow, remote = env
    ops.config.compile_model_revision = "model-v1"
    row = await build(env)
    ops.config.compile_model_revision = "model-v2"
    second = await ops.store.submit(P, "other", row["request"])
    second = await drive(ops, flow, second["id"])
    assert second["state"] == "schema_review"
    assert not second["result"].get("cache_hit")
    assert len(remote.tasks) == 2


@pytest.mark.asyncio
async def test_repeated_quote_requires_exact_offsets_and_invalid_proof_rejected(env):
    ops, flow, remote = env
    row = await build(env)
    output = copy.deepcopy(row["result"]["output"])
    sources = list(remote.frozen.values())
    manifest = flow.manifest(row, row["result"], output["schema"])
    output["evidence"][0]["start"] = -1
    with pytest.raises(HTTPException, match="AMBIGUOUS_EVIDENCE_LOCATION"):
        candidate_from_drafts(output, sources, manifest, output["schema"])
    output = copy.deepcopy(row["result"]["output"])
    for proofs in ([["not-existing"]], [[]], []):
        output["assertions"][0]["support_sets"] = proofs
        with pytest.raises(HTTPException, match="PROOF_INCOMPLETE"):
            candidate_from_drafts(output, sources, manifest, output["schema"])
    output["assertions"] = []
    with pytest.raises(HTTPException, match="NO_VALID_FACTS"):
        candidate_from_drafts(output, sources, manifest, output["schema"])


@pytest.mark.asyncio
async def test_schema_and_evidence_contracts_are_bounded_and_preserve_conflict(env):
    _, flow, remote = env
    row = await build(env)
    output = copy.deepcopy(row["result"]["output"])
    opposing = copy.deepcopy(output["assertions"][0])
    opposing.update(assertion_id="negative", polarity="negative")
    output["assertions"].append(opposing)
    manifest = flow.manifest(row, row["result"], output["schema"])
    bundle = candidate_from_drafts(output, list(remote.frozen.values()), manifest, output["schema"])
    assert {a["polarity"] for a in bundle["assertions"]} == {"positive", "negative"}
    output["assertions"][0]["epistemic_kind"] = "system_fact"
    with pytest.raises(HTTPException, match="UNTRUSTED_FACT_KIND"):
        candidate_from_drafts(output, list(remote.frozen.values()), manifest, output["schema"])


@pytest.mark.asyncio
async def test_confirm_changed_digest_is_rejected(env):
    _, flow, _ = env
    row = await build(env)
    with pytest.raises(HTTPException, match="SCHEMA_PROPOSAL_CHANGED"):
        await flow.confirm(P, row["id"], ConfirmSchemaRequest(schema_body=SCHEMA, expected_digest="changed"))


@pytest.mark.asyncio
async def test_http_contract_restore_and_schema_confirmation(env, monkeypatch):
    from fastapi import FastAPI

    from team_ontology import api

    ops, flow, remote = env
    monkeypatch.setattr(api, "Operations", lambda *args, **kwargs: ops)
    app = FastAPI()

    async def principal():
        return P

    api.install(app, principal, config=OntologyConfig(enabled=True), dsn="unused", resolver=ops.resolve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te.test") as client:
        response = await client.post(
            "/te/enterprise/v1/source-collections",
            json={
                "contract": SOURCE_CONTRACT,
                "submission_key": "http-sources",
                "roots": ["viking://resources/wiki"],
                "readers": ["frank"],
            },
        )
        assert response.status_code == 202, response.text
        group_id = response.json()["id"]
        await drive(ops, flow, group_id)
        groups = (await client.get("/te/enterprise/v1/source-collections")).json()
        assert groups[0]["state"] == "sources_ready"
        response = await client.post(
            "/te/enterprise/v1/jobs",
            json={
                "contract": BUILD_CONTRACT,
                "submission_key": "http-build",
                "collection_id": group_id,
                "expected_generation": 0,
            },
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        await drive(ops, flow, job_id)
        job = (await client.get("/te/enterprise/v1/jobs/" + job_id)).json()
        assert job["state"] == "schema_review"
        assert set(job["result"]["output"]) == {"schema"}
        assert "payload" not in job["result"]
        response = await client.post(
            f"/te/enterprise/v1/jobs/{job_id}/schema-confirm",
            json={
                "schema_body": job["result"]["output"]["schema"],
                "expected_digest": job["result"]["output_digest"],
            },
        )
        assert response.status_code == 200, response.text
        row = await drive(ops, flow, job_id)
        assert row["state"] == "review_ready", row
        assert len(remote.tasks) == 1
        assert len((await client.get("/te/enterprise/v1/jobs")).json()) == 1


@pytest.mark.asyncio
async def test_partial_recompile_namespaces_model_ids_and_retains_other_proofs(env):
    ops, flow, remote = env
    row = await build(env)
    schema = copy.deepcopy(SCHEMA)
    schema["predicates"]["extra"] = {**schema["predicates"]["status"]}
    schema["revision"] = "example-v2"
    selected = row["result"]["refs"][1]["source_id"]
    await flow.confirm(
        P,
        row["id"],
        ConfirmSchemaRequest(
            schema_body=schema,
            expected_digest=row["result"]["output_digest"],
            affected_sources=[selected],
        ),
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "review_ready", row
    assert len(remote.tasks) == 2
    assert len(row["result"]["candidate"]["assertions"]) == 2
    assert {e["source_id"] for e in row["result"]["candidate"]["evidence"]} == {
        r["source_id"] for r in row["result"]["refs"]
    }


@pytest.mark.asyncio
async def test_chinese_single_file_and_uploaded_reference_share_collection_flow(env):
    ops, flow, remote = env
    path = "viking://resources/中文 Wiki/签收记录.md"
    remote.files[path] = "signed on 2026-09-01"
    row = await ops.store.submit(
        P,
        "single",
        {
            "contract": SOURCE_CONTRACT,
            "roots": [path],
            "references": [],
            "readers": ["frank"],
        },
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "sources_ready", row
    refs = row["result"]["refs"]
    uploaded = await ops.store.submit(
        P,
        "uploaded",
        {
            "contract": SOURCE_CONTRACT,
            "roots": [],
            "references": refs + refs,
            "readers": ["frank"],
        },
    )
    uploaded = await drive(ops, flow, uploaded["id"])
    assert uploaded["state"] == "sources_ready", uploaded
    assert uploaded["result"]["refs"] == refs
    assert uploaded["result"]["files"] == []


@pytest.mark.asyncio
async def test_empty_directory_cannot_build(env):
    ops, flow, remote = env
    remote.dirs.add("viking://resources/empty")
    row = await ops.store.submit(
        P,
        "empty",
        {
            "contract": SOURCE_CONTRACT,
            "roots": ["viking://resources/empty"],
            "references": [],
            "readers": ["frank"],
        },
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "failed"
    assert row["error"] == "NO_READABLE_SOURCES"
    assert not remote.tasks


@pytest.mark.asyncio
async def test_cancel_uses_checkpoint_returned_by_atomic_fence(env, monkeypatch):
    ops, _, _ = env
    row = await ops.store.submit(P, "cancel-race", {"contract": BUILD_CONTRACT})
    ops.store.rows[row["id"]]["result"] = {"stage": "submit"}
    observed = []

    async def change(p, identifier, allowed, state, result=None):
        current = ops.store.rows[identifier]
        # Simulate the worker saving acceptance just before cancellation's atomic update.
        if state == "cancelling":
            current["result"] = {"stage": "poll", "compile_id": "accepted-during-race"}
        current["state"] = state
        return copy.deepcopy(current)

    async def cancel_remote(self, p, job):
        observed.append(job["result"].get("compile_id"))

    async def ov(*args, **kwargs):
        return {}

    monkeypatch.setattr(ops.store, "change", change, raising=False)
    monkeypatch.setattr(CompileFlow, "cancel_remote", cancel_remote)
    monkeypatch.setattr(ops, "ov", ov)
    cancelled = await ops.cancel(P, row["id"])
    assert cancelled["state"] == "cancelled"
    assert observed == ["accepted-during-race"]


@pytest.mark.asyncio
async def test_cancel_waits_for_native_terminal_receipt(env, monkeypatch):
    ops, _, _ = env
    row = await ops.store.submit(P, "cancel-async", {"contract": BUILD_CONTRACT})
    ops.store.rows[row["id"]]["result"] = {"stage": "poll", "compile_id": "cmp_async"}
    native_state = "running"
    calls = []

    async def change(p, identifier, allowed, state, result=None):
        current = ops.store.rows[identifier]
        assert current["state"] in allowed
        current["state"] = state
        return copy.deepcopy(current)

    async def ov(p, method, path, body=None, **kwargs):
        nonlocal native_state
        calls.append((method, path))
        if path == "/tasks/cmp_async/cancel":
            native_state = "cancelling"
        return {"status": native_state}

    monkeypatch.setattr(ops.store, "change", change, raising=False)
    monkeypatch.setattr(ops, "ov", ov)
    assert (await ops.cancel(P, row["id"]))["state"] == "cancelling"
    assert (await ops.cancel(P, row["id"]))["state"] == "cancelling"
    assert calls.count(("POST", "/tasks/cmp_async/cancel")) == 1
    assert ("POST", "/artifacts/cancel") not in calls
    native_state = "cancelled"
    assert (await ops.cancel(P, row["id"]))["state"] == "cancelled"
    assert ("POST", "/artifacts/cancel") in calls


@pytest.mark.asyncio
async def test_parent_source_never_reingests_private_compile_workspace(env):
    ops, _, remote = env
    private = "viking://resources/wiki/private"
    ops.config.compile_workspace_uri = private
    remote.dirs.add(private)
    remote.files[private + "/draft.md"] = "must not enter another source collection"
    row = await collection(env)
    assert not any(f["uri"].startswith(private) for f in row["result"]["files"])


def test_compile_skill_accepts_yaml_reserialization_but_not_changed_instructions():
    import yaml

    from team_ontology.compile_contract import compile_skill_digest

    original = Path("team_ontology/skills/ontology-extraction-v1/SKILL.md").read_text()
    _, metadata, body = original.split("---", 2)
    rewrapped = "---\n" + yaml.safe_dump(yaml.safe_load(metadata), sort_keys=False, width=65) + "---" + body
    assert original != rewrapped
    assert compile_skill_digest(original) == compile_skill_digest(rewrapped)
    assert compile_skill_digest(original) != compile_skill_digest(rewrapped + "\nChanged instructions.")


@pytest.mark.parametrize(
    "path",
    [
        "https://example.test/skill",
        "viking://resources/a",
        "viking://agent/skills/../a",
        "viking://agent/skills/%252e%252e",
        "viking://agent/skills/a?token=x",
        "viking://agent/skills/a#fragment",
        "viking://agent/skills",
        "viking://agent/skills/a/sub",
    ],
)
def test_build_rejects_invalid_skill_uri(path):
    from pydantic import ValidationError

    from team_ontology.compile_contract import CompileBuildRequest

    with pytest.raises(ValidationError, match="COMPILE_SKILL_URI_INVALID"):
        CompileBuildRequest(submission_key="s", collection_id="c", expected_generation=0, skill_uri=path)


@pytest.mark.asyncio
async def test_custom_compile_skill_is_saved_and_survives_config_change(env):
    import yaml

    from team_ontology.compile_contract import CompileBuildRequest

    ops, flow, remote = env
    path = "viking://agent/skills/ontology-extraction-copy"
    original = Path("team_ontology/skills/ontology-extraction-v1/SKILL.md").read_text()
    _, metadata, body = original.split("---", 2)
    remote.files[path + "/SKILL.md"] = "---\n" + yaml.safe_dump(yaml.safe_load(metadata)) + "---" + body
    remote.dirs.add(path)
    group = await collection(env)
    request = CompileBuildRequest(
        submission_key="custom", collection_id=group["id"], expected_generation=0, skill_uri="  " + path + "/SKILL.md  "
    ).model_dump(mode="json")
    assert request["skill_uri"] == path
    row = await ops.store.submit(P, "custom", request)
    ops.store.rows[row["id"]].update(state="running", attempt=1)
    await flow.step(copy.deepcopy(ops.store.rows[row["id"]]))
    assert ops.store.rows[row["id"]]["result"]["skill_uri"] == path
    ops.config.compile_skill_uri = "invalid-new-config"
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "schema_review", row
    assert next(iter(remote.tasks.values()))["meta"]["request"]["skill"] == path


@pytest.mark.asyncio
async def test_blank_compile_skill_uses_service_default(env):
    from team_ontology.compile_contract import CompileBuildRequest

    ops, flow, remote = env
    group = await collection(env)
    request = CompileBuildRequest(
        submission_key="default", collection_id=group["id"], expected_generation=0, skill_uri="  "
    ).model_dump(mode="json")
    assert request["skill_uri"] == ""
    row = await ops.store.submit(P, "default", request)
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "schema_review", row
    assert row["result"]["skill_uri"] == ops.config.compile_skill_uri
    assert next(iter(remote.tasks.values()))["meta"]["request"]["skill"] == ops.config.compile_skill_uri


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [408, 429, 502, 503, 504])
async def test_transient_poll_preserves_task_and_recovers(env, monkeypatch, status):
    ops, flow, _ = env
    row = await ops.store.submit(P, 'poll', {'contract': BUILD_CONTRACT})
    now = 1000.0
    monkeypatch.setattr('team_ontology.compile_flow.time.time', lambda: now)
    row.update(state='running', result={'stage': 'poll', 'compile_id': 'cmp-existing',
                                      'round': 0, 'submitted_at': now})
    ops.store.rows[row['id']] = copy.deepcopy(row)
    calls = []

    async def native(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise HTTPException(status, 'OV_REQUEST_FAILED')
        return {'status': 'failed', 'error': 'BOT_RESTARTED: private error text'}

    monkeypatch.setattr(flow, 'native', native)
    pending = await flow.step(row)
    assert pending['state'] == 'queued'
    assert pending['result']['compile_id'] == 'cmp-existing'
    assert pending['result']['poll_failures'] == 1
    pending['state'] = 'running'
    ops.store.rows[row['id']] = copy.deepcopy(pending)
    await flow.step(pending)
    assert len(calls) == 1  # No I/O during persisted backoff.
    now += 3
    pending = ops.store.rows[row['id']]
    pending['state'] = 'running'
    failed = await flow.step(copy.deepcopy(pending))
    assert failed['error'] == 'COMPILE_EXECUTION_FAILED'
    assert failed['result']['remote_error'] == 'BOT_RESTARTED'
    assert 'poll_failures' not in failed['result']
    assert all(args[1:3] == ('GET', '/tasks/cmp-existing') for args in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,failures,expected', [(403, 0, 'FORBIDDEN'), (503, 7, 'COMPILE_POLL_UNAVAILABLE')])
async def test_poll_permissions_and_retry_budget(env, monkeypatch, status, failures, expected):
    import time
    ops, flow, _ = env
    row = await ops.store.submit(P, 'poll-budget', {'contract': BUILD_CONTRACT})
    row.update(state='running', result={'stage': 'poll', 'compile_id': 'cmp-existing',
        'round': 0, 'submitted_at': time.time(), 'poll_failures': failures})
    ops.store.rows[row['id']] = copy.deepcopy(row)

    async def native(*args, **kwargs):
        raise HTTPException(status, 'FORBIDDEN' if status == 403 else 'OV_REQUEST_FAILED')

    monkeypatch.setattr(flow, 'native', native)
    failed = await flow.step(row)
    assert failed['state'] == 'failed'
    assert failed['error'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['OV_REQUEST_FAILED', 'COMPILE_POLL_UNAVAILABLE'])
async def test_retry_failed_poll_reuses_existing_remote_task(env, error):
    ops, flow, _ = env
    row = await ops.store.submit(P, 'retry-poll', {'contract': BUILD_CONTRACT})
    row.update(state='failed', error=error, result={'stage': 'poll', 'compile_id': 'cmp-existing',
        'round': 0, 'submitted_at': 1, 'poll_failures': 8, 'poll_retry_at': 999})
    ops.store.rows[row['id']] = copy.deepcopy(row)
    resumed = await flow.retry(P, row['id'])
    assert resumed['state'] == 'queued'
    assert resumed['result'] == {'stage': 'poll', 'compile_id': 'cmp-existing', 'round': 0, 'submitted_at': 1}


@pytest.mark.asyncio
async def test_partial_invalid_dates_are_quarantined_without_model_recompile(env):
    ops, flow, remote = env
    ops.config.compile_model_revision = "temporal-cache-v1"
    original = remote.output
    def output(target, sources, schema=None):
        original(target, sources, schema)
        # Find the shard rather than relying on its fixture filename.
        path = target + "/" + json.loads(remote.files[target + "/result-manifest.json"])["draft_files"][0]
        rows = [json.loads(line) for line in remote.files[path].splitlines()]
        assertion = next(r for r in rows if r["kind"] == "assertions")
        duplicate = copy.deepcopy(assertion)
        duplicate["value"].update(assertion_id="undated", valid_from="not-a-date")
        rows.append(duplicate)
        remote.files[path] = "\n".join(json.dumps(r) for r in rows)
    remote.output = output
    row = await build(env)
    assert row["state"] == "schema_review", row
    assert row["result"]["assertion_gaps"][0]["code"] == "ASSERTION_DATETIME_INVALID"
    cached = await ops.store.submit(P, "temporal-cache", row["request"])
    cached = await drive(ops, flow, cached["id"])
    assert cached["result"]["cache_hit"] == row["id"]
    assert cached["result"]["assertion_gaps"] == row["result"]["assertion_gaps"]
    await flow.confirm(P, row["id"], ConfirmSchemaRequest(
        schema_body=SCHEMA, expected_digest=row["result"]["output_digest"]))
    candidate = await drive(ops, flow, row["id"])
    assert candidate["state"] == "review_ready"
    assert len(candidate["result"]["candidate"]["assertions"]) == 2
    assert any(g["code"] == "ASSERTION_DATETIME_INVALID" for g in candidate["result"]["gaps"])
    assert len(remote.tasks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("dates,code", [
    ({"valid_from": "secret-not-a-date"}, "ASSERTION_DATETIME_INVALID"),
    ({"valid_to": "2026-09-01T00:00:00Z"}, "ASSERTION_INTERVAL_EMPTY"),
    ({"valid_to": "2026-08-31T23:00:00Z"}, "ASSERTION_INTERVAL_EMPTY"),
])
async def test_all_invalid_dates_send_safe_bounded_repair_feedback(env, dates, code):
    ops, flow, remote = env
    original = remote.output
    def output(target, sources, schema=None):
        original(target, sources, schema)
        path = target + "/" + json.loads(remote.files[target + "/result-manifest.json"])["draft_files"][0]
        rows = [json.loads(line) for line in remote.files[path].splitlines()]
        for r in rows:
            if r["kind"] == "assertions":
                r["value"].update(dates)
        remote.files[path] = "\n".join(json.dumps(r) for r in rows)
    remote.output = output
    row = await build(env)
    assert row["state"] == "failed" and row["error"] == "COMPILE_ASSERTION_DATES_INVALID"
    assert len(remote.tasks) == 3  # Initial compile plus the existing two repair limit.
    feedback = row["result"]["repair_issues"]
    assert feedback and feedback[0]["code"] == code
    assert "secret-not-a-date" not in json.dumps(feedback)
    instructions = [json.loads(t["meta"]["request"]["instruction"]) for t in remote.tasks.values()]
    assert instructions[1]["repair_issues"] == feedback
    assert instructions[1]["repair_issue_count"] == 2


def test_temporal_repair_feedback_limit():
    from team_ontology.compile_contract import DraftValidationError, temporal_issues
    issues = [issue for i in range(30) for issue in temporal_issues({"valid_from": "not-a-date"}, i)]
    error = DraftValidationError("COMPILE_ASSERTION_DATES_INVALID", issues)
    assert error.repair_issue_count == 30 and len(error.repair_issues) == 20


@pytest.mark.asyncio
async def test_acl_wait_does_not_override_cancellation(env, monkeypatch):
    ops, flow, _ = env
    group = await collection(env)
    row = await ops.store.submit(P, "cancel-acl", {
        "contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0})
    row.update(state="running", result={"stage": "stage_inputs", "round": 0, "refs": group["result"]["refs"],
        "gaps": [], "staged": 0, "acl_wait": {"started_at": 1000, "retry_at": 1010, "failures": 1}})
    ops.store.rows[row["id"]] = {**copy.deepcopy(row), "state": "cancelled"}
    monkeypatch.setattr("team_ontology.compile_flow.time.time", lambda: 1005)
    assert (await flow.step(row))["state"] == "fenced"
    assert ops.store.rows[row["id"]]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_during_acl_request_fences_wait_checkpoint(env, monkeypatch):
    ops, flow, _ = env
    row = await ops.store.submit(P, "cancel-during-acl", {"contract": BUILD_CONTRACT})
    row["state"] = "running"
    ops.store.rows[row["id"]] = copy.deepcopy(row)

    async def build_with_cancellation(p, job, result):
        result.update(stage="stage_inputs", round=0, workspace_target="viking://resources/private")
        ops.store.rows[job["id"]]["state"] = "cancelled"
        raise HTTPException(400, "COMPILE_ACL_TARGET_NOT_READY")

    monkeypatch.setattr(flow, "build", build_with_cancellation)
    assert (await flow.step(row))["state"] == "fenced"
    assert ops.store.rows[row["id"]]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_reference_collection_persists_reference_characters(env):
    ops, flow, remote = env
    remote.frozen["wiki_preexisting"] = {
        "source_id": "wiki_preexisting",
        "revision": "rev-1",
        "text": "签收于 2026-09-01，由上海转运中心发出 " * 5,
        "readers": ["frank"],
        "origin_uri": "viking://resources/wiki/elsewhere.md",
    }
    row = await ops.store.submit(
        P,
        "refs",
        {
            "contract": SOURCE_CONTRACT,
            "roots": [],
            "readers": ["frank"],
            "references": [{"source_id": "wiki_preexisting", "revision": "rev-1", "digest": "unused"}],
        },
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "sources_ready", row
    assert row["result"]["reference_characters"] == len(remote.frozen["wiki_preexisting"]["text"])


@pytest.mark.asyncio
async def test_submit_transition_persists_predicted_deadline(env):
    ops, flow, remote = env
    row = await build(env)
    assert row["state"] == "schema_review", row
    predicted = row["result"]["predicted"]
    # Fixture corpus: index.md is 126,000 chars, body.md is 20 chars.
    # Greedy packing: 80,000 + 46,020 -> 2 batches -> deadline 1.5*(1800+4500+1800).
    expected_chars = len(remote.files["viking://resources/wiki/index.md"]) + len(
        remote.files["viking://resources/wiki/sub/body.md"]
    )
    assert predicted == {
        "batches": 2,
        "total_characters": expected_chars,
        "duration_seconds": 12_150,
        "note": "预计编译 ~3 小时",
        "estimated": False,
    }
    assert row["result"]["deadline_seconds"] == 12_150
    assert row["result"]["deadline_at"]


@pytest.mark.asyncio
async def test_predicted_deadline_exceeded_cancels_and_fails(env):
    ops, flow, remote = env
    group = await collection(env)
    row = await ops.store.submit(
        P, "deadline", {"contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0}
    )
    await drive_until(ops, flow, row["id"], "poll")
    current = ops.store.rows[row["id"]]
    assert current["result"]["deadline_seconds"] == 12_150
    current["result"]["deadline_seconds"] = 0  # expire immediately
    current.update(state="queued")
    final = await drive(ops, flow, row["id"])
    assert final["state"] == "failed", final
    assert final["error"] == "COMPILE_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_poll_without_deadline_field_falls_back_to_fixed_budget(env):
    ops, flow, remote = env
    group = await collection(env)
    row = await ops.store.submit(
        P, "legacy", {"contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0}
    )
    await drive_until(ops, flow, row["id"], "poll")
    current = ops.store.rows[row["id"]]
    current["result"].pop("deadline_seconds", None)  # simulate a pre-upgrade job
    current["result"]["submitted_at"] = time.time() - 1801
    current["result"].pop("deadline_at", None)
    current.update(state="queued")
    final = await drive(ops, flow, row["id"])
    assert final["state"] == "failed", final
    assert final["error"] == "COMPILE_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_repair_round_recomputes_predicted_for_subset(env):
    ops, flow, remote = env
    row = await build(env)
    assert row["result"]["predicted"]["batches"] == 2
    schema = copy.deepcopy(SCHEMA)
    schema["predicates"]["extra"] = {**schema["predicates"]["status"]}
    schema["revision"] = "example-v2"
    selected = row["result"]["refs"][1]["source_id"]  # body.md only -> subset re-round
    await flow.confirm(
        P,
        row["id"],
        ConfirmSchemaRequest(
            schema_body=schema,
            expected_digest=row["result"]["output_digest"],
            affected_sources=[selected],
        ),
    )
    # Round reset clears the deadline fields before re-staging.
    queued = ops.store.rows[row["id"]]["result"]
    assert "predicted" not in queued and "deadline_seconds" not in queued and "deadline_at" not in queued
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "review_ready", row
    # The subset re-round staged only body.md (20 chars) -> one batch, recomputed deadline.
    assert row["result"]["predicted"]["batches"] == 1
    assert row["result"]["predicted"]["estimated"] is False
    # Round 0's snapshot feeds the calibration dataset (spec §7).
    assert row["result"]["history"][0]["deadline_seconds"] == 12_150
    assert row["result"]["history"][0]["predicted"]["batches"] == 2


@pytest.mark.asyncio
async def test_http_job_creation_returns_predicted_duration(env, monkeypatch):
    from fastapi import FastAPI

    from team_ontology import api

    ops, flow, remote = env
    monkeypatch.setattr(api, "Operations", lambda *args, **kwargs: ops)
    app = FastAPI()

    async def principal():
        return P

    api.install(app, principal, config=OntologyConfig(enabled=True), dsn="unused", resolver=ops.resolve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te.test") as client:
        response = await client.post(
            "/te/enterprise/v1/source-collections",
            json={
                "contract": SOURCE_CONTRACT,
                "submission_key": "predict-sources",
                "roots": ["viking://resources/wiki"],
                "readers": ["frank"],
            },
        )
        assert response.status_code == 202, response.text
        group_id = response.json()["id"]
        await drive(ops, flow, group_id)
        response = await client.post(
            "/te/enterprise/v1/jobs",
            json={
                "contract": BUILD_CONTRACT,
                "submission_key": "predict-build",
                "collection_id": group_id,
                "expected_generation": 0,
            },
        )
        assert response.status_code == 202, response.text
        # The injected preview is ephemeral; only the submit transition persists it.
        assert "predicted" not in ops.store.rows[response.json()["id"]]["result"]
        predicted = response.json()["result"]["predicted"]
        expected_chars = len(remote.files["viking://resources/wiki/index.md"]) + len(
            remote.files["viking://resources/wiki/sub/body.md"]
        )
        assert predicted == {
            "batches": 2,
            "total_characters": expected_chars,
            "duration_seconds": 12_150,
            "note": "预计编译 ~3 小时",
            "estimated": False,
        }


@pytest.mark.asyncio
async def test_http_creation_prediction_legacy_references_estimated(env, monkeypatch):
    from fastapi import FastAPI

    from team_ontology import api

    ops, flow, remote = env
    remote.frozen["wiki_preexisting"] = {
        "source_id": "wiki_preexisting",
        "revision": "rev-1",
        "text": "签收于 2026-09-01，由上海转运中心发出 " * 5,
        "readers": ["frank"],
        "origin_uri": "viking://resources/wiki/elsewhere.md",
    }
    monkeypatch.setattr(api, "Operations", lambda *args, **kwargs: ops)
    app = FastAPI()

    async def principal():
        return P

    api.install(app, principal, config=OntologyConfig(enabled=True), dsn="unused", resolver=ops.resolve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te.test") as client:
        response = await client.post(
            "/te/enterprise/v1/source-collections",
            json={
                "contract": SOURCE_CONTRACT,
                "submission_key": "legacy-refs",
                "roots": [],
                "readers": ["frank"],
                "references": [{"source_id": "wiki_preexisting", "revision": "rev-1", "digest": "unused"}],
            },
        )
        assert response.status_code == 202, response.text
        group_id = response.json()["id"]
        await drive(ops, flow, group_id)
        # Simulate a collection created before reference_characters existed.
        ops.store.rows[group_id]["result"].pop("reference_characters", None)
        response = await client.post(
            "/te/enterprise/v1/jobs",
            json={
                "contract": BUILD_CONTRACT,
                "submission_key": "legacy-refs-build",
                "collection_id": group_id,
                "expected_generation": 0,
            },
        )
        assert response.status_code == 202, response.text
        predicted = response.json()["result"]["predicted"]
        assert predicted["estimated"] is True
        assert predicted["batches"] >= 1


def test_temporal_issues_standing_and_normalization():
    from team_ontology.compile_contract import temporal_issues

    standing = {"assertion_id": "a", "valid_from": None}
    assert temporal_issues(standing, 0) == []
    assert standing["valid_from"] is None  # standing stays untouched

    naive = {"valid_from": "2026-04-15T00:00:00"}
    assert temporal_issues(naive, 0) == []
    assert naive["valid_from"] == "2026-04-15T00:00:00+08:00"  # normalized in place

    ended_standing = {"valid_from": None, "valid_to": "2026-10-01T00:00:00Z"}
    assert temporal_issues(ended_standing, 0) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("published,expected_mode", [("handover-v0", "replace"), ("example-v1", "delta"), (None, "delta")])
async def test_manifest_mode_follows_published_schema(env, published, expected_mode):
    ops, flow, remote = env
    remote.published_schema = published
    row = await build(env)
    await flow.confirm(P, row["id"], ConfirmSchemaRequest(
        schema_body=SCHEMA, expected_digest=row["result"]["output_digest"]))
    candidate = await drive(ops, flow, row["id"])
    assert candidate["state"] == "review_ready", candidate
    assert candidate["result"]["manifest"]["mode"] == expected_mode
