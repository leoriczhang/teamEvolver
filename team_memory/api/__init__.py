"""Memory APIs: short PG acceptance and query endpoints."""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.config_runtime import ConfigRevisionConflict, TenantConfigMissing
from teamEvolver.tenants.registry import current_tenant_id

from ..config import DEFAULT_MEMORY_MODULE_CONFIG, MemoryModuleConfig, load_memory_config, resolve_memory_config
from ..repository import MemoryRepository
from ..runs import accept
from ..schemas import run_payload


def create_router(services=None, **kwargs) -> APIRouter:
    router = APIRouter(tags=["memory"])
    if services is None or not services.extras.get("config_repo"):
        return router
    repo = MemoryRepository(services.tx)
    configs = services.extras["config_repo"]

    def admin(request):
        require_tenant_admin(getattr(request.state, "console_user", None))

    def settings_payload(config):
        aggregation = config.aggregation
        maintenance = config.maintenance
        prefix = str(aggregation.get("shared_knowledge_prefix") or "shared-knowledge").strip("/")
        staging = str(aggregation.get("staging_dir") or "staging").strip("/")
        return {
            **aggregation,
            "target_root": f"viking://resources/{prefix}",
            "work_root": f"viking://user/{{merge_user}}/resources/teamEvolver/{staging}/<target-hash>",
            "maintenance_trigger_mode": maintenance["trigger_mode"],
            "maintenance_interval_seconds": maintenance["interval_seconds"],
            "revision": config.revision,
        }

    async def body(request):
        raw = await request.body()
        if len(raw) > 256 * 1024:
            raise HTTPException(413, "Memory request exceeds 256 KiB")
        try:
            value = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "body must be an object")
        return value

    async def start(request, maintenance=False):
        admin(request)
        raw = await body(request)
        if maintenance:
            raw = {**raw, "pipeline": "maintenance"}
        tenant = current_tenant_id()
        config = services.extras.get("config")
        if config is None:
            raise HTTPException(503, "OpenViking bootstrap config unavailable")
        key = request.headers.get("Idempotency-Key")
        if key and len(key) > 200:
            raise HTTPException(400, "Idempotency-Key too long")
        rid = "memory:" + (hashlib.sha256(key.encode()).hexdigest() if key else uuid.uuid4().hex)
        try:
            async with services.tx.tenant_tx(tenant) as conn:
                module = await load_memory_config(configs, conn)
                from teamEvolver.tenants.registry import effective_config, get_current_tenant
                cfg = effective_config(None, get_current_tenant(), config)
                payload = run_payload(raw, module, cfg.sharing_viking_account, cfg.sharing_viking_user or "team")
                record = await accept(conn, services, tenant, rid, raw, payload)
        except TenantConfigMissing as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": rid, "task_id": rid, "status": "queued", "status_url": f"/api/jobs/{rid}",
                "target_uri": record["target_uri"]}

    @router.post("/api/aggregation/run", status_code=202)
    async def aggregation(request: Request):
        return await start(request)

    @router.post("/api/memory/maintenance/run", status_code=202)
    async def maintenance(request: Request):
        return await start(request, maintenance=True)

    async def present(conn, row):
        task = await services.tasks.get(conn, task_id=row["run_id"])
        return {"task_id": row["run_id"], "job_id": row["run_id"], "target_uri": row["target_uri"],
                "status": task["status"] if task else "unavailable", "result": row["result"],
                "created_at": row["created_at"], "finished_at": row["finished_at"],
                "config_revision": row["input"]["config_revision"]}

    @router.get("/api/aggregation/runs")
    async def runs(request: Request):
        admin(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            return {"runs": [await present(conn, row) for row in await repo.list(conn)]}

    @router.get("/api/aggregation/status/{task_id}")
    async def status(task_id: str, request: Request):
        admin(request)
        try:
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                return await present(conn, await repo.get(conn, task_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/memory/maintenance/status")
    async def maintenance_status(request: Request):
        admin(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            rows = [r for r in await repo.list(conn) if r["input"]["pipeline"] == "maintenance"]
            return await present(conn, rows[0]) if rows else {"status": "idle"}

    @router.get("/api/memory/maintenance/dry-run")
    async def dry_run(request: Request):
        admin(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            module = await configs.read_tenant_module(conn, "memory")
        return {"config": module.config, "config_revision": module.revision}

    @router.get("/api/aggregation/settings")
    async def settings(request: Request):
        admin(request)
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            config = await load_memory_config(configs, conn)
        return settings_payload(config)

    @router.post("/api/aggregation/settings")
    async def save_settings(request: Request):
        admin(request)
        value = await body(request)
        expected = value.pop("expected_revision", None)
        if not isinstance(expected, int):
            raise HTTPException(400, "expected_revision required")
        trigger_mode = value.pop("maintenance_trigger_mode", "manual")
        interval = value.pop("maintenance_interval_seconds", 86400)
        allowed = set(DEFAULT_MEMORY_MODULE_CONFIG["aggregation"])
        if set(value) - allowed:
            raise HTTPException(400, "unsupported Memory setting")
        if trigger_mode not in {"manual", "automatic"}:
            raise HTTPException(400, "maintenance_trigger_mode must be manual or automatic")
        if type(interval) is not int or not 300 <= interval <= 2592000:
            raise HTTPException(400, "maintenance_interval_seconds must be 300..2592000")
        try:
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                module = await configs.read_tenant_module(conn, "memory")
                current = resolve_memory_config(module)
                updated = {
                    **current.config,
                    "aggregation": {**current.aggregation, **value},
                    "maintenance": {
                        **current.maintenance,
                        "trigger_mode": trigger_mode,
                        "interval_seconds": interval,
                    },
                }
                revision = await configs.write_tenant_module(conn, module="memory", expected_revision=expected,
                                                             config=updated)
        except ConfigRevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return settings_payload(MemoryModuleConfig(revision=revision, config=updated))

    @router.get("/api/memory/changes")
    async def changes(request: Request, limit: int = 100):
        admin(request)
        if not 1 <= limit <= 500:
            raise HTTPException(400, "limit must be 1..500")
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            return {"changes": await repo.changes(conn, limit)}

    from .skills import create_router as skill_router
    router.include_router(skill_router(services))
    from .replay import create_router as replay_router
    router.include_router(replay_router(services))
    return router
