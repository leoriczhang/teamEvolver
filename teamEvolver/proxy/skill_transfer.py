"""Authenticated, bounded Skill transfer routes (all blocking work runs off-loop)."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from team_skills.library.mutations import SkillMutationService
from team_skills.transfer.packages import TransferError
from team_skills.transfer.service import SkillTransferService

from ..tenants.registry import current_tenant_id
from .skills_admin import _clear_version_cache, _require_admin_request


class ImportBody(BaseModel):
    channel: str
    options: dict[str, Any] = Field(default_factory=dict)
    conflict: str = "replace"


class ExportBody(BaseModel):
    channel: str
    names: list[str] = Field(min_length=1, max_length=256)
    options: dict[str, Any] = Field(default_factory=dict)


def register_skill_transfer_routes(app: FastAPI, owner) -> None:
    slots = threading.BoundedSemaphore(2)

    def service() -> SkillTransferService:
        from .routes import _tenant_effective_config
        config = _tenant_effective_config(owner)
        return SkillTransferService(owner._skills_dir(),
                                    SkillMutationService.from_config(config, tenant_id=current_tenant_id()))

    async def execute(callback):
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Skill 导入导出繁忙，请稍后重试")

        def work():
            try:
                return callback()
            finally:
                # Release in the worker even when the HTTP client disconnects.
                slots.release()
        try:
            return await asyncio.shield(asyncio.to_thread(work))
        except TransferError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "Skill 传输失败，请检查存储或远端连接") from exc

    @app.get("/api/skills/transfer/channels")
    async def channels(request: Request):
        _require_admin_request(request)
        return {"channels": [{"id": name, "import": True, "export": True}
                             for name in ("zip", "marketplace", "git")]}

    @app.get("/api/skills/transfer/skills")
    async def list_skills(request: Request):
        _require_admin_request(request)
        return await execute(lambda: {"skills": service().list_skills()})

    async def run_import(body: ImportBody, request: Request):
        _require_admin_request(request)

        def work():
            result = service().import_skills(body.channel, body.options, conflict=body.conflict)
            result["loaded_skills"] = owner._reload_skill_manager()
            _clear_version_cache()
            return result
        return await execute(work)

    app.post("/api/skills/import")(run_import)

    @app.post("/api/skills/export")
    async def export_skills(body: ExportBody, request: Request):
        _require_admin_request(request)
        result = await execute(lambda: service().export_skills(body.channel, body.names, body.options))
        if result.content is not None:
            return Response(result.content, media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{result.filename}"'})
        return result.metadata

    @app.post("/api/skills/import-zip")
    async def legacy_zip(body: dict[str, Any], request: Request):
        result = await run_import(ImportBody(channel="zip", options={**body, "single": True}), request)
        if result["errors"]:
            raise HTTPException(502, result["errors"][0]["error"])
        if len(result["imported"]) != 1:
            raise HTTPException(400, "请使用批量导入接口")
        item = result["imported"][0]
        return {**item, "loaded_skills": result["loaded_skills"]}

    @app.post("/api/skills/import-zip-batch")
    async def legacy_batch(body: dict[str, Any], request: Request):
        result = await run_import(ImportBody(channel="zip", options=body), request)
        return {**result, "cloud": [{"name": item["name"], "cloud": item["cloud"]}
                                    for item in result["imported"]]}
