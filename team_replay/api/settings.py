"""Versioned tenant Replay limits for console administrators."""

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.tenants.registry import current_tenant_id

from ..config import load_replay_config, run_options


def create_router(services):
    router = APIRouter(tags=["replay-settings"])
    configs, tx = services.extras["config_repo"], services.tx

    @router.get("/api/replay/settings")
    async def read(request: Request):
        require_tenant_admin(getattr(request.state, "console_user", None))
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await load_replay_config(configs, conn)
        return {"revision": row.revision, "config": run_options(
            {k: row.config[k] for k in ("concurrency", "timeout_seconds", "max_interactions")})}

    @router.put("/api/replay/settings")
    async def save(request: Request):
        require_tenant_admin(getattr(request.state, "console_user", None))
        from session_ingestion.http import read_limited_json_body
        raw = await read_limited_json_body(request)
        if (not isinstance(raw, dict) or set(raw) != {"expected_revision", "config"}
                or type(raw["expected_revision"]) is not int):
            raise HTTPException(400, "Expected expected_revision and config")
        try:
            value = run_options(raw["config"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await load_replay_config(configs, conn)
            await configs.write_tenant_module(conn, module="replay", expected_revision=raw["expected_revision"],
                                              config={**row.config, **value})
        return await read(request)

    return router
