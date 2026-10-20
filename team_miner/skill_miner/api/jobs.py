"""Short transaction acceptance and PG reads for Skill mining jobs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .. import config
from ..artifacts import MAX_ARTIFACT_BYTES, _artifact_path, _job_metadata, _payload, artifact_job_id
from ..presentation import metadata
from ..presentation import read as read_view
from ..repository import MiningRevisionConflict
from ..settings import pipeline_settings, reset_prompt, settings_payload, update_settings
from ..storage import PgMiningFileStore
from ..task_io import State, accept
from .context import Context, read_body, translate


def router(services):
    routes = APIRouter()
    ctx = Context(services)

    async def owned_job(repository, job_id, settings):
        job = await asyncio.to_thread(repository.backend.load_job, job_id)
        if not job or job.get("_deleted"):
            raise KeyError(job_id)
        if job.get("account") != settings.miner_config["account_id"]:
            raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
        return job

    @routes.get("/api/mining/config")
    async def mining_config(request: Request):
        settings = await ctx.settings(request)
        defaults = pipeline_settings(settings.config)
        return {"engine": "ov_compile", "max_rounds_default": defaults["max_rounds"], "max_rounds_range": [1, 5],
                "runtime_timeout_seconds_default": defaults["runtime_timeout_seconds"],
                "runtime_timeout_seconds_range": [60, 7200], "rounds_mode": "orchestrated",
                "human_checkpoints_supported": True,
                "compile_model": {"configuration_source": "openviking_server", "editable_here": False}}

    @routes.get("/api/mining/settings")
    async def get_settings(request: Request):
        current = await ctx.settings(request)
        return {**settings_payload(current.config), "revision": current.revision}

    async def change_settings(request, stage_id=None):
        await ctx.settings(request)
        body = await read_body(request)
        revision = body.pop("expected_revision", None)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise HTTPException(400, "expected_revision must be a non-negative integer")
        try:
            async with services.tx.tenant_tx(ctx.tenant) as conn:
                current = await config.load(services, conn)
                value = current.config
                payload = reset_prompt(value, stage_id) if stage_id else update_settings(value, body)
                saved = await config.save(services, conn, value, expected_revision=revision)
            return {**payload, "revision": saved.revision}
        except (ValueError, KeyError, MiningRevisionConflict) as exc:
            raise translate(exc) from exc

    @routes.post("/api/mining/settings")
    async def save_settings(request: Request):
        return await change_settings(request)

    @routes.post("/api/mining/prompts/{stage_id}/reset")
    async def reset_settings(stage_id: str, request: Request):
        return await change_settings(request, stage_id)

    @routes.get("/api/mining/jobs")
    async def jobs(request: Request):
        await ctx.settings(request)
        repository = ctx.repo()
        def read():
            rows = repository.list_jobs()
            public = [metadata(row) for row in rows]
            return {"jobs": public, "summary": repository.summary(public)}
        return await asyncio.to_thread(read)

    @routes.post("/api/mining/jobs", status_code=202)
    async def create(request: Request):
        settings = await ctx.settings(request)
        body = await read_body(request, 128 * 1024)
        return await accept(request, services, "job.create", body, settings)

    @routes.get("/api/mining/jobs/{job_id}")
    async def detail(job_id: str, request: Request):
        settings = await ctx.settings(request)
        try:
            job, view = await read_view(services, ctx.tenant, job_id)
            if job.get("account") != settings.miner_config["account_id"]:
                raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
            if view is not None:
                return {"ok": True, "job": view}
            receipt = await accept(request, services, "job.detail", {}, settings, job_id=job_id)
            return JSONResponse(receipt, status_code=202)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise translate(exc) from exc

    async def command(request, job_id, operation, maximum=1024 * 1024):
        settings = await ctx.settings(request)
        body = await read_body(request, maximum)
        repository = ctx.repo()
        try:
            await owned_job(repository, job_id, settings)
        except (KeyError, ValueError) as exc:
            raise translate(exc) from exc
        return await accept(request, services, operation, body, settings, job_id=job_id)

    @routes.post("/api/mining/jobs/{job_id}/resume", status_code=202)
    async def resume(job_id: str, request: Request):
        return await command(request, job_id, "job.resume")

    @routes.post("/api/mining/jobs/{job_id}/supplements", status_code=202)
    async def supplements(job_id: str, request: Request):
        return await command(request, job_id, "job.supplements", 30 * 1024 * 1024)

    @routes.post("/api/mining/jobs/{job_id}/continue", status_code=202)
    async def continue_job(job_id: str, request: Request):
        return await command(request, job_id, "job.continue")

    @routes.post("/api/mining/jobs/{job_id}/finish", status_code=202)
    async def finish(job_id: str, request: Request):
        return await command(request, job_id, "job.finish")

    @routes.post("/api/mining/jobs/{job_id}/stop", status_code=202)
    async def stop(job_id: str, request: Request):
        return await command(request, job_id, "job.stop")

    @routes.delete("/api/mining/jobs/{job_id}", status_code=202)
    async def delete(job_id: str, request: Request):
        return await command(request, job_id, "job.delete")

    @routes.get("/api/mining/artifacts/content")
    async def artifact(request: Request, path: str):
        settings = await ctx.settings(request)
        repository = ctx.repo()
        try:
            job_id = artifact_job_id(path)
            job = await owned_job(repository, job_id, settings)
        except (KeyError, ValueError) as exc:
            raise translate(exc) from exc
        def read():
            parts, _, kind = _artifact_path(path)
            _job_metadata(None, job_id, job)
            content = PgMiningFileStore(repository.backend).read_file(
                job_id, "/".join(parts[2:]), maximum=MAX_ARTIFACT_BYTES)
            return _payload(parts, kind, content)
        try:
            return await asyncio.to_thread(read)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise translate(exc) from exc

    @routes.post("/api/mining/artifacts/content", status_code=202)
    async def save_artifact(request: Request):
        settings = await ctx.settings(request)
        body = await read_body(request, 3 * 1024 * 1024)
        try:
            job_id = artifact_job_id(body.get("path", ""))
        except ValueError as exc:
            raise translate(exc) from exc
        return await accept(request, services, "artifact.save", body, settings, job_id=job_id)

    @routes.get("/api/mined-jobs/{identifier}")
    async def operation_status(identifier: str, request: Request):
        await ctx.settings(request)
        async with services.tx.tenant_tx(ctx.tenant) as conn:
            task = await services.tasks.get(conn, task_id=identifier)
            if task is None or task["kind"] != "miner.skill_operation":
                raise HTTPException(404, "挖掘后台任务不存在")
            result = await State(services, ctx.tenant).get(conn, "result", identifier)
        return {"job_id": identifier, "status": task["status"], "result": result, "error_code": task["error_code"]}

    @routes.post("/api/mined-jobs/{job_id}/skills/{skill_name}/submit", status_code=202)
    async def submit_candidate(job_id: str, skill_name: str, request: Request):
        settings = await ctx.settings(request)
        body = await read_body(request)
        return await accept(request, services, "candidate.submit", {**body, "skill_name": skill_name},
                            settings, job_id=job_id)

    return routes
