"""Knowledge operations console, hosted in the existing TE application."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .compile_contract import (
    DEFAULT_COMPILE_SKILL_URI,
    CompileBuildRequest,
    ConfirmSchemaRequest,
    SourceCollectionRequest,
)
from .compile_flow import CompileFlow
from .config import OntologyConfig
from .service import Operations
from .wire import CandidateBundle, Manifest, Schema, Source


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobRequest(Wire):
    submission_key: str = Field(min_length=1, max_length=200)
    manifest: Manifest
    sources: list[Source] = Field(default_factory=list, max_length=100)


class EditRequest(Wire):
    candidate: CandidateBundle
    expected_digest: str


class ApprovalRequest(Wire):
    note: str = Field(min_length=1, max_length=2000)
    acknowledge_gaps: bool = False


class RebaseRequest(Wire):
    submission_key: str = Field(min_length=1, max_length=200)


class RollbackRequest(RebaseRequest):
    generation: int = Field(ge=1)


class CommitRequest(Wire):
    commit_key: str = Field(min_length=1, max_length=200)


def public_job(job):
    result = {**job, "task_id": job["id"]}
    result["result"] = {k: v for k, v in job["result"].items() if k != "grant"}
    if "output" in result["result"]:
        result["result"]["output"] = {"schema": result["result"]["output"]["schema"]}
    for key in ("previous_output", "payload", "source_map"):
        result["result"].pop(key, None)
    return result


def install(app, principal_dependency, *, config, dsn, resolver, transport=None, schema="teamevolver"):
    if not config.enabled:
        return
    ops = Operations(config, dsn, resolver, transport, schema=schema)
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            await ops.start()
            application.state.ontology = ops
            try:
                yield
            finally:
                await ops.close()

    app.router.lifespan_context = lifespan
    api = APIRouter(prefix="/te/enterprise/v1", tags=["knowledge-operations"])
    dep = Depends(principal_dependency)

    @api.get("/capabilities")
    async def capabilities(p=dep):
        capabilities = await ops.ov(p, "GET", "/ontology/capabilities")
        return {**capabilities, "compile_skill_uri": config.compile_skill_uri or DEFAULT_COMPILE_SKILL_URI}

    @api.put("/access")
    async def access(body: dict, p=dep):
        result = await ops.ov(p, "PUT", "/ontology/access", body)
        await ops.store.audit(p, "access.updated", result)
        return result

    @api.get("/jobs")
    async def jobs(p=dep):
        return [
            public_job(j)
            for j in await ops.store.list(p)
            if j["request"].get("contract") != "sf.te.ontology.sources.v1"
        ]

    @api.post("/jobs", status_code=202)
    async def create(body: CompileBuildRequest, p=dep):
        await resolver(p)
        collection = await ops.store.get(p, body.collection_id)
        if collection["state"] != "sources_ready":
            raise HTTPException(409, "SOURCE_COLLECTION_NOT_READY")
        return public_job(await ops.store.submit(p, body.submission_key, body.model_dump(mode="json")))

    @api.get("/source-collections")
    async def collections(p=dep):
        return [
            public_job(j)
            for j in await ops.store.list(p)
            if j["request"].get("contract") == "sf.te.ontology.sources.v1"
        ]

    @api.post("/source-collections", status_code=202)
    async def create_collection(body: SourceCollectionRequest, p=dep):
        await resolver(p)
        return public_job(await ops.store.submit(p, body.submission_key, body.model_dump(mode="json")))

    @api.get("/source-collections/{collection_id}")
    async def collection(collection_id: str, p=dep):
        return public_job(await ops.store.get(p, collection_id))

    @api.post("/source-collections/{collection_id}/retry")
    async def retry_collection(collection_id: str, p=dep):
        return public_job(await CompileFlow(ops).retry(p, collection_id))

    @api.post("/source-collections/{collection_id}/cancel")
    async def cancel_collection(collection_id: str, p=dep):
        return public_job(await ops.cancel(p, collection_id))

    @api.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str, p=dep):
        return public_job(await CompileFlow(ops).retry(p, job_id))

    @api.post("/jobs/{job_id}/schema-confirm")
    async def confirm_schema(job_id: str, body: ConfirmSchemaRequest, p=dep):
        return public_job(await CompileFlow(ops).confirm(p, job_id, body))

    @api.get("/jobs/{job_id}")
    async def get(job_id: str, p=dep):
        return public_job(await ops.store.get(p, job_id))

    @api.put("/jobs/{job_id}/candidate")
    async def edit(job_id: str, body: EditRequest, p=dep):
        return public_job(await ops.edit(p, job_id, body.candidate.model_dump(mode="json"), body.expected_digest))

    @api.post("/jobs/{job_id}/prepare")
    async def prepare(job_id: str, p=dep):
        return public_job(await ops.prepare(p, job_id))

    @api.post("/jobs/{job_id}/approve")
    async def approve(job_id: str, body: ApprovalRequest, p=dep):
        return public_job(await ops.approve(p, job_id, body.note, body.acknowledge_gaps))

    @api.post("/jobs/{job_id}/commit")
    async def commit(job_id: str, body: CommitRequest, p=dep):
        return public_job(await ops.commit(p, job_id, body.commit_key))

    @api.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str, p=dep):
        return public_job(await ops.cancel(p, job_id))

    @api.post("/jobs/{job_id}/rebase", status_code=202)
    async def rebase(job_id: str, body: RebaseRequest, p=dep):
        return public_job(await ops.rebase(p, job_id, body.submission_key))

    @api.post("/rollback-candidates", status_code=202)
    async def rollback(body: RollbackRequest, p=dep):
        return public_job(await ops.rollback(p, body.generation, body.submission_key))

    @api.post("/snapshots/freeze", status_code=201)
    async def freeze(body: dict, p=dep):
        return await ops.ov(p, "POST", "/snapshots/freeze", body)

    @api.post("/sources", status_code=201)
    async def source(body: Source, p=dep):
        return await ops.ov(p, "POST", "/sources", body.model_dump(mode="json"))

    @api.post("/schemas", status_code=201)
    async def schema(body: Schema, p=dep):
        return await ops.ov(p, "POST", "/schemas", body.model_dump(mode="json"))

    @api.get("/versions")
    async def versions(p=dep):
        return await ops.ov(p, "GET", "/assets/versions")

    @api.get("/feedback")
    async def feedback(p=dep):
        return await ops.ov(p, "GET", "/ontology/feedback")

    @api.post("/source-events")
    async def source_event(body: dict, p=dep):
        await ops.store.audit(p, "withdrawal.requested", body)
        result = await ops.ov(p, "POST", "/source-events", body)
        await ops.store.audit(p, "withdrawal.acknowledged", result)
        return result

    @api.post("/context/compose")
    async def compose(body: dict, p=dep):
        return await ops.ov(p, "POST", "/context/compose", body)

    from .review import install_review

    install_review(api, ops, dep)
    app.include_router(api)


def install_native(app):
    from teamEvolver.proxy.tenant_routes import get_tenant_registry
    from teamEvolver.storage.pg_pool import dsn_from_env
    from teamEvolver.tenants.registry import current_tenant_id, effective_config

    owner = app.state.owner
    config = OntologyConfig.from_host(owner.config)
    if not config.enabled:
        return

    async def principal(request: Request):
        user = getattr(request.state, "console_user", None)
        if not isinstance(user, dict) or user.get("role") != "admin":
            from teamEvolver.logging_runtime import event

            from .diagnostics import logger

            event(logger, "ontology.access_denied", code="CONSOLE_ADMIN_REQUIRED")
            raise HTTPException(403, "CONSOLE_ADMIN_REQUIRED")
        return {"tenant": current_tenant_id(), "subject": str(user["id"])}

    async def resolver(p):
        registry = get_tenant_registry(owner)
        tenant = await asyncio.to_thread(registry.get, p["tenant"])
        if tenant is None or tenant.status != "active":
            raise HTTPException(403, "TENANT_DISABLED")
        cfg = await asyncio.to_thread(effective_config, registry, tenant, owner.config)
        from teamEvolver.proxy.users_admin import _load_registry, _registry_path

        users = await asyncio.to_thread(_load_registry, _registry_path(cfg), cfg)
        user = next(
            (u for u in users.get("users", []) if u.get("id") == p["subject"] and u.get("role") == "admin"), None
        )
        if not user:
            raise HTTPException(403, "CONSOLE_ADMIN_REQUIRED")
        space = user.get("team_space") or {}
        key = cfg.sharing_viking_api_key
        if not key:
            raise HTTPException(503, "OV_CREDENTIAL_REQUIRED")
        return {
            "url": cfg.sharing_viking_endpoint,
            "account": cfg.sharing_viking_account,
            "api_key": key,
            "model": config.llm(cfg),
            "subject": space.get("viking_user") or p["subject"],
        }

    install(
        app,
        principal,
        config=config,
        dsn=owner.config.storage_pg_dsn or dsn_from_env(),
        resolver=resolver,
        schema=owner.config.storage_pg_schema,
    )
