"""Native Compile contract tests with deterministic model output; never a real-model success claim."""

import copy
import json
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
        self.lose_response, self.forbidden, self.leak = False, set(), False

    def reply(self, data, status=200, native=True):
        return httpx.Response(status, json={"status": "ok", "result": data} if native else data)

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        params = parse_qs(request.url.query.decode())
        target = params.get("uri", [body.get("uri")])[0]
        user = request.headers.get("x-openviking-user")
        self.calls.append((request.method, path, body, user))
        assert request.headers["authorization"] == "Bearer backend-secret"
        if user.startswith("te-ontology-probe") and not self.leak:
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
        if target in self.forbidden:
            return httpx.Response(403, json={"error": {"code": "PERMISSION_DENIED"}})
        if path.endswith("/ontology/capabilities"):
            return self.reply({"tenant": P["tenant"], "subject": P["subject"], "generation": 0}, native=False)
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
            return self.reply({"isDir": target in self.dirs})
        if path.endswith("/fs/mkdir"):
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
            if request.method == "PUT":
                self.acls[body["uri"]] = {"acl_mode": "restricted", "effective_entries": body["entries"]}
            return self.reply(self.acls[target])
        if path.endswith("/content/write"):
            assert "te_ontology_compile" in target
            assert any(target.startswith(a + "/") for a in self.acls)
            if target in self.files:
                return httpx.Response(409, json={"error": {"code": "ALREADY_EXISTS"}})
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
