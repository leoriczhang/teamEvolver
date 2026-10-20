"""Skill transfer API: only short acceptance and shared-state queries."""
import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from team_skills.api.candidates import accepted, actor, command_id, error
from team_skills.config import load_skills_config
from team_skills.library.repository import SkillLibraryError
from team_skills.transfer.commands import TransferCommands
from team_skills.transfer.packages import TransferError
from team_skills.transfer.service import SkillTransferService
from teamEvolver.config_runtime import TenantConfigMissing
from teamEvolver.tenants.registry import current_tenant_id


def create_router(services=None, **kwargs):
    router = APIRouter(tags=["skill-transfer"])
    if services is None:
        return router
    commands, service = TransferCommands(services), SkillTransferService(services.tx)

    @router.get("/api/skills/transfer/channels")
    async def channels(request: Request):
        actor(request)
        return {"channels": [{"id": c, "import": True, "export": True} for c in ("zip", "marketplace", "git")]}

    @router.get("/api/skills/transfer/skills")
    async def skills(request: Request):
        actor(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            return {"skills": await service.list_skills(conn)}

    async def submit(request, operation):
        from . import _read_json

        reviewer = actor(request)
        media = request.headers.get("content-type", "").split(";")[0].strip()
        if media and media != "application/json":
            raise HTTPException(415, "需要 application/json；ZIP 使用 zip_b64 字段")
        body = await _read_json(request)
        try:
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(services.extras["config_repo"], conn)
                result = await commands.accept(
                    conn, tenant_id=current_tenant_id(), command_id=command_id(request, f"transfer-{operation}"),
                    operation=operation, body=body, config=config, actor=reviewer,
                )
            return accepted(result)
        except (SkillLibraryError, TransferError, TenantConfigMissing) as exc:
            raise error(exc) from exc
        except ValueError:
            raise HTTPException(400, "Skill 名称或渠道参数不合法") from None

    @router.post("/api/skills/import", status_code=202)
    async def import_skills(request: Request):
        return await submit(request, "import")

    @router.post("/api/skills/export", status_code=202)
    async def export_skills(request: Request):
        return await submit(request, "export")

    @router.get("/api/skills/transfer/jobs/{job_id}/result")
    async def result(job_id: str, request: Request):
        actor(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            command = await commands.get(conn, job_id)
        if not command:
            raise HTTPException(404, "transfer not found")
        return {"job_id": job_id, "result": command["result"]}

    @router.get("/api/skills/transfer/jobs/{job_id}/download")
    async def download(job_id: str, request: Request):
        actor(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            row = await commands.archive(conn, job_id)
        if row is None:
            raise HTTPException(404, "archive not found")
        data = bytes(row["archive"])
        if hashlib.sha256(data).hexdigest() != row["archive_hash"]:
            raise HTTPException(409, "archive integrity check failed")
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="skills.zip"', "ETag": f'"{row["archive_hash"]}"',
        })

    return router
