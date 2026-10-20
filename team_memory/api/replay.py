"""Persistent Memory Replay acceptance; execution is injected by composition."""
import uuid

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.tenants.registry import current_tenant_id

from ..replay import specification
from ..repository import MemoryRepository


def create_router(services):
    router = APIRouter()
    accept_replay = services.extras.get("memory_replay_accept")
    list_replay = services.extras.get("memory_replay_list")

    async def accept(request, change_id=""):
        require_tenant_admin(getattr(request.state, "console_user", None))
        if accept_replay is None:
            raise HTTPException(503, "Memory Replay integration unavailable")
        raw = await request.json()
        if not isinstance(raw, dict):
            raise HTTPException(400, "body must be an object")
        if change_id:
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                changes = await MemoryRepository(services.tx).changes(conn, 500)
            change = next((c for c in changes if c["change_id"] == change_id), None)
            if change is None:
                raise HTTPException(404, "Memory Change not found")
            raw["memory_path"] = change["target_uri"]
            reader = services.extras.get("memory_change_reader")
            if reader is None:
                raise HTTPException(503, "Memory Change snapshots unavailable")
            raw["before_content"], raw["after_content"] = await reader(change)
        try:
            rid = "memory-replay:" + uuid.uuid4().hex
            spec = specification(rid, change_id or raw.get("memory_path", ""), raw,
                                 str(raw.get("before_content", "")), str(raw.get("after_content", "")),
                                 str(raw.get("memory_path", "")))
            result = await accept_replay(spec, raw, current_tenant_id())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": result["run_id"], "run_id": result["run_id"], "status_url": "/api/jobs/" + result["run_id"]}

    @router.post("/api/openviking/memory/true-replay", status_code=202)
    async def adhoc(request: Request):
        return await accept(request)

    @router.post("/api/memory/changes/{change_id}/true-replay", status_code=202)
    async def historical(change_id: str, request: Request):
        return await accept(request, change_id)

    @router.get("/api/memory/changes/{change_id}/true-replays")
    async def histories(change_id: str, request: Request, limit: int = 100):
        require_tenant_admin(getattr(request.state, "console_user", None))
        if not 1 <= limit <= 500:
            raise HTTPException(400, "invalid limit")
        if list_replay is None:
            raise HTTPException(503, "Memory Replay integration unavailable")
        return {"replays": await list_replay(current_tenant_id(), change_id, limit)}
    return router
