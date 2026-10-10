"""Authenticated, bounded Skill transfer routes (all blocking work runs off-loop)."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
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

    async def require_json(request: Request):
        _require_admin_request(request)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type and media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            # Reject binary/multipart bodies before model validation tries to
            # serialize the invalid byte input into FastAPI's error response.
            raise HTTPException(415, "Skill 导入导出接口需要 application/json；ZIP 请使用 zip_b64 字段")

    def service() -> SkillTransferService:
        from .routes import _tenant_effective_config
        config = _tenant_effective_config(owner)
        return SkillTransferService(
            SkillMutationService.from_config(
                config,
                tenant_id=current_tenant_id(),
            )
        )

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
            transfer = service()
            result = transfer.import_skills(
                body.channel,
                body.options,
                conflict=body.conflict,
            )
            for item in result.get("imported") or []:
                name = str(item.get("name") or "")
                if not name:
                    continue
                bundle = transfer.hub.read_skill_bundle(name)
                owner._materialize_runtime_bundle(
                    name,
                    bundle,
                    category=str(item.get("category") or "general"),
                )
            _clear_version_cache()
            return result
        return await execute(work)

    app.post("/api/skills/import", dependencies=[Depends(require_json)])(run_import)

    @app.post("/api/skills/export", dependencies=[Depends(require_json)])
    async def export_skills(body: ExportBody, request: Request):
        _require_admin_request(request)
        result = await execute(lambda: service().export_skills(body.channel, body.names, body.options))
        if result.content is not None:
            return Response(result.content, media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{result.filename}"'})
        return result.metadata
