"""KEE APIs: console governance uses existing membership, optional structural reads are finite."""

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.routing import APIRoute
from pydantic import Field

from .contracts import Contract, ProcedurePack, digest, safe_uri
from .service import BUILD


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.method in {"POST", "PUT", "PATCH"}:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 8 * 1024 * 1024:
                        raise HTTPException(413, "KEE_REQUEST_SIZE_LIMIT")
                request._body = bytes(body)
            return await handler(request)

        return bounded


class Upload(Contract):
    name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2000000)


class Freeze(Contract):
    roots: list[str] = Field(default_factory=list, max_length=20)
    uploads: list[Upload] = Field(default_factory=list, max_length=1000)
    readers: list[str] = Field(min_length=1, max_length=20)


class Build(Contract):
    contract: Literal["kee.method-build.v1"] = BUILD
    submission_key: str = Field(min_length=1, max_length=200)
    collection_id: str
    pack_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
    clarification: str = Field(default="", max_length=10000)
    fixture: ProcedurePack | None = None


class Edit(Contract):
    expected_digest: str
    candidate: ProcedurePack


class Confirm(Contract):
    expected_digest: str


class Prepare(Contract):
    target_uri: str
    readers: list[str] = Field(min_length=1, max_length=20)


class Approve(Contract):
    note: str = Field(min_length=1, max_length=2000)
    acknowledge_gaps: bool = False


class Commit(Contract):
    commit_key: str = Field(min_length=1, max_length=200)


class Query(Contract):
    target_uri: str
    generation: str = ""
    subject: str = Field(default="", max_length=200)
    predicate: str = Field(default="", max_length=100)
    limit: int = Field(default=50, ge=1, le=100)
    max_scan: int = Field(default=1000, ge=1, le=10000)
    timeout_ms: int = Field(default=100, ge=1, le=1000)
    expected: str | int | bool | None = None
    caller_supplied: list[dict] = Field(default_factory=list, max_length=20)


def install(app, ops, admin_dependency, reader_dependency=None, reader_mapper=None):
    api = APIRouter(prefix="/kee/v1", tags=["kee-governance"], route_class=BoundedRoute)
    admin = Depends(admin_dependency)
    reader = Depends(reader_dependency or admin_dependency)
    from ..review import Document, parse_document

    svc = ops.kee
    svc.reader_mapper = reader_mapper

    @api.get("/capabilities")
    async def capabilities(p=reader):
        return {
            "platform": "KEE",
            "contract": "kee.r6.v1",
            "structural_reads": ops.config.kee_query_enabled,
            "execution_interceptors": False,
            "executor": "kee.internal.v1",
            "mcp_tools": [],
            "views": ["agent-manager", "knowledge-manager", "research"],
            "view_changes_permissions": False,
        }

    @api.post("/sources", status_code=201)
    async def freeze(body: Freeze, p=admin):
        await svc.map_readers(p, body.readers)
        return await svc.sources.freeze(p, body.roots, [u.model_dump() for u in body.uploads], body.readers)

    @api.post("/documents", status_code=201)
    async def document(body: Document, p=admin):
        await svc.map_readers(p, body.readers)
        parsed = await asyncio.to_thread(parse_document, body)
        return await svc.sources.freeze(p, [], [{"name": body.filename, "text": parsed.text}], body.readers)

    @api.get("/sources/{collection_id}")
    async def sources(collection_id: str, p=admin):
        value = await svc.sources.load(p, collection_id)
        if not value or p["subject"] not in value["readers"]:
            raise HTTPException(404, "SOURCE_COLLECTION_NOT_FOUND")
        return value

    @api.post("/sources/{collection_id}/revoke")
    async def revoke(collection_id: str, p=admin):
        await sources(collection_id, p)
        # The marker fences any in-flight compile/publication. Deny dependent Wiki
        # roots immediately, then reuse existing jobs for recursive deletion.
        await svc.db.put(p["tenant"], "revoked/" + collection_id, {"at": time.time(), "subject": p["subject"]})
        affected = []
        async with ops.store.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {ops.store.jobs} WHERE tenant=$1 AND result->>'collection_id'=$2",
                p["tenant"],
                collection_id,
            )
        from ..control import row_value

        for row in rows:
            old = row_value(row)
            result = old["result"]
            release = result.get("release")
            if not release or release["state"] == "withdrawn" or result.get("action") == "withdraw":
                continue
            uri = release["target_uri"] + "/releases/" + release["pack_digest"]
            if await svc.publisher.read(p, uri + "/index.md") is not None:
                await svc.publisher.acl(p, uri, [], private=True)
            head = await svc.db.get(p["tenant"], "heads/" + digest(release["target_uri"]), default={})
            if head.get("digest") == release["pack_digest"]:
                await svc.publisher.acl(p, release["target_uri"] + "/index.md", [], private=True)
            async with ops.store.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    f"UPDATE {ops.store.jobs} SET state='failed',attempt=attempt+1,error='SOURCE_REVOKED' "
                    "WHERE tenant=$1 AND id=$2",
                    p["tenant"],
                    old["id"],
                )
                await conn.execute(f"UPDATE {ops.store.outbox} SET pending=false WHERE job_id=$1", old["id"])
            cleanup = await ops.store.submit(
                p,
                "revoke:" + old["id"],
                {
                    "contract": BUILD,
                    "auth_context": {k: p[k] for k in ("support_id", "service_root") if p.get(k)},
                    "revocation_of": old["id"],
                },
                imported={**result, "action": "withdraw", "stage": "wiki_withdraw"},
            )
            if cleanup["state"] == "imported":
                cleanup = await ops.store.resume(p, cleanup["id"], ["imported"], cleanup["result"])
            affected.append(cleanup["id"])
        await ops.store.audit(p, "kee.source.revoked", {"collection_id": collection_id, "cleanup_jobs": affected})
        return {"revoked": True, "cleanup_jobs": affected, "cleanup_complete": not affected}

    @api.post("/methods/build", status_code=202)
    async def build(body: Build, p=admin):
        await sources(body.collection_id, p)
        if await svc.db.get(p["tenant"], "revoked/" + body.collection_id):
            raise HTTPException(403, "SOURCE_REVOKED")
        request = body.model_dump(mode="json")
        request["auth_context"] = {k: p[k] for k in ("support_id", "service_root") if p.get(k)}
        return await ops.store.submit(p, body.submission_key, request)

    @api.get("/methods/jobs")
    async def jobs(p=admin):
        # Includes legacy executor IDs for historical query/cancel, without modifying their executor.
        return await ops.store.list(p)

    @api.get("/methods/jobs/{job_id}")
    async def job(job_id: str, p=admin):
        return await ops.store.get(p, job_id)

    @api.put("/methods/jobs/{job_id}/candidate")
    async def edit(job_id: str, body: Edit, p=admin):
        return await svc.edit(p, job_id, body.candidate, body.expected_digest)

    @api.post("/methods/jobs/{job_id}/confirm-schema")
    async def confirm(job_id: str, body: Confirm, p=admin):
        return await svc.confirm(p, job_id, body.expected_digest)

    @api.post("/methods/jobs/{job_id}/prepare")
    async def prepare(job_id: str, body: Prepare, p=admin):
        await svc.map_readers(p, body.readers)
        try:
            target = safe_uri(body.target_uri)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return await svc.prepare(p, job_id, target, body.readers)

    @api.post("/methods/jobs/{job_id}/approve")
    async def approve(job_id: str, body: Approve, p=admin):
        return await svc.approve(p, job_id, body.note, body.acknowledge_gaps)

    @api.post("/methods/jobs/{job_id}/commit")
    async def commit(job_id: str, body: Commit, p=admin):
        return await svc.commit(p, job_id, body.commit_key)

    @api.post("/methods/jobs/{job_id}/retry")
    async def retry(job_id: str, p=admin):
        return await svc.retry(p, job_id)

    @api.post("/methods/jobs/{job_id}/cancel")
    async def cancel(job_id: str, p=admin):
        value = await job(job_id, p)
        if value["request"].get("contract") != BUILD:
            return await ops.cancel(p, job_id)
        return await svc.cancel(p, job_id)

    @api.post("/methods/jobs/{job_id}/withdraw")
    async def withdraw(job_id: str, p=admin):
        return await svc.withdraw(p, job_id)

    @api.post("/methods/jobs/{job_id}/rollback", status_code=202)
    async def rollback(job_id: str, body: Commit, p=admin):
        old = await job(job_id, p)
        result = {
            k: v
            for k, v in old["result"].items()
            if k
            in {
                "collection_id",
                "candidate",
                "candidate_digest",
                "schema_confirmed",
                "readers",
                "connection",
                "executor",
            }
        }
        if not result.get("candidate"):
            raise HTTPException(409, "NO_ROLLBACK_MATERIAL")
        pack = dict(result["candidate"])
        pack["version"] = "rollback-" + digest(body.commit_key)[:16]
        result.update(candidate=pack, candidate_digest=digest(pack))
        await svc.db.put(p["tenant"], "packs/" + pack["pack_id"] + "/" + digest(pack), pack, immutable=True)
        # New task, Prepare and approval against the current baseline; never mutate history.
        return await ops.store.submit(p, body.commit_key, {**old["request"], "rollback_of": job_id}, imported=result)

    @api.get("/methods/jobs/{job_id}/export")
    async def export(job_id: str, p=admin):
        value = await job(job_id, p)
        pack = ProcedurePack.model_validate(value["result"]["candidate"])
        return {"management": "NOT_MANAGED", "files": render(pack), "pack_digest": digest(pack.model_dump())}

    @api.get("/wiki/releases")
    async def releases(after: str = "", p=reader):
        rows = await svc.db.list(p["tenant"], "releases/", after=after)
        return {
            "items": [
                {"key": row["key"], "release": row["value"]["release"]}
                for row in rows
                if p["subject"] in row["value"]["readers"]
            ],
            "next": rows[-1]["key"] if len(rows) == 100 else None,
        }

    @api.get("/wiki/releases/{target_hash}/{generation}/pages/{path:path}")
    async def read_page(target_hash: str, generation: str, path: str, p=reader):
        value = await svc.db.get(p["tenant"], "releases/" + target_hash + "/" + generation)
        if (
            not value
            or value["release"]["state"] != "published"
            or p["subject"] not in value["readers"]
            or path not in value["release"]["pages"]
        ):
            raise HTTPException(404, "WIKI_PAGE_NOT_FOUND")
        for collection_id in value["release"]["sources"]:
            if await svc.db.get(p["tenant"], "revoked/" + collection_id):
                raise HTTPException(403, "SOURCE_REVOKED")
            collection = await svc.sources.load(p, collection_id)
            await svc.sources.check_current(p, collection, [p["subject"]])
        settings = await ops.resolve(p)
        from urllib.parse import urlencode

        uri = value["release"]["receipt"]["release_uri"] + "/" + path
        content = await ops.ov(
            p,
            "GET",
            "/content/read?" + urlencode({"uri": uri}),
            native=True,
            identity=settings.get("subject") or p["subject"],
        )
        import hashlib

        if (
            not isinstance(content, str)
            or hashlib.sha256(content.encode()).hexdigest() != value["release"]["pages"][path]
        ):
            raise HTTPException(409, "WIKI_READBACK_MISMATCH")
        return {"generation": generation, "path": path, "content": content}

    @api.get("/methods/jobs/{job_id}/impact")
    async def impact(job_id: str, p=admin):
        value = await job(job_id, p)
        result = value["result"]
        pack = result.get("candidate", {})
        baseline = result.get("prepared", {}).get("baseline", "")
        previous = None
        if baseline:
            target = result["prepared"]["target_uri"]
            previous = await svc.db.get(p["tenant"], "releases/" + digest(target) + "/" + baseline)
        old = {m["method_id"]: m for m in (previous or {}).get("candidate", {}).get("methods", [])}
        current = {m["method_id"]: m for m in pack.get("methods", [])}
        return {
            "baseline": baseline,
            "added": sorted(current.keys() - old.keys()),
            "removed": sorted(old.keys() - current.keys()),
            "changed": sorted(key for key in old.keys() & current.keys() if digest(old[key]) != digest(current[key])),
            "links": sum(len(m.get("related_methods", [])) for m in current.values()),
            "source_count": len({e["source_id"] for m in current.values() for e in m["evidence"]}),
        }

    @api.post("/feedback", status_code=202)
    async def feedback(body: Approve, p=reader):
        await ops.store.audit(p, "kee.feedback", {"note": body.note})
        return {"accepted": True, "formal_assets_changed": False}

    async def query_data(body, p):
        if not ops.config.kee_query_enabled:
            raise HTTPException(404, "KEE_STRUCTURAL_READS_DISABLED")
        try:
            target = safe_uri(body.target_uri)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        head = await svc.db.get(p["tenant"], "heads/" + digest(target), default={})
        generation = body.generation or head.get("digest", "")
        stored = await svc.db.get(p["tenant"], "releases/" + digest(target) + "/" + generation)
        if not stored or stored["release"]["state"] != "published" or p["subject"] not in stored["readers"]:
            raise HTTPException(404, "WIKI_RELEASE_NOT_FOUND")
        for source in stored["release"]["sources"]:
            if await svc.db.get(p["tenant"], "revoked/" + source):
                raise HTTPException(403, "SOURCE_REVOKED")
        for source in stored["release"]["sources"]:
            collection = await svc.sources.load(p, source)
            await svc.sources.check_current(p, collection, [p["subject"]])
        start = time.monotonic()
        selected, scanned, clipped = [], 0, False
        for fact in stored["candidate"].get("facts", []):
            if scanned >= body.max_scan or (time.monotonic() - start) * 1000 >= body.timeout_ms:
                clipped = True
                break
            scanned += 1
            if (body.subject and fact["subject"] != body.subject) or (
                body.predicate and fact["predicate"] != body.predicate
            ):
                continue
            if len(selected) >= body.limit:
                clipped = True
                break
            selected.append(fact)
        return {
            "generation": generation,
            "facts": selected,
            "scanned": scanned,
            "truncated": clipped,
            "caller_supplied": "non_authoritative",
            "schema_revision": stored["candidate"]["schema_revision"],
        }

    @api.post("/ontology/query")
    async def query(body: Query, p=reader):
        return await query_data(body, p)

    @api.post("/ontology/check")
    async def check(body: Query, p=reader):
        if not body.subject or not body.predicate or body.expected is None:
            raise HTTPException(422, "FINITE_RULE_REQUIRED")
        value = await query_data(body, p)
        matching = [
            f for f in value["facts"] if type(f["value"]) is type(body.expected) and f["value"] == body.expected
        ]
        positive = [f for f in matching if f["polarity"] == "positive"]
        negative = [f for f in matching if f["polarity"] == "negative"]
        conflict = bool(positive and negative)
        truth = (
            "UNKNOWN" if conflict or value["truncated"] else "TRUE" if positive else "FALSE" if negative else "UNKNOWN"
        )
        return {
            **value,
            "truth": truth,
            "conflict": conflict,
            "gaps": [] if positive or negative else ["NO_EVIDENCE"],
            "execution_authorized": False,
        }

    from .wiki import render

    app.include_router(api)
