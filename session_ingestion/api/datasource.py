"""Datasource control plane: immutable PG adapters and queued Worker operations."""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.config_runtime import ConfigRevisionConflict, TenantConfigMissing
from teamEvolver.tenants.registry import current_tenant_id

from ..adapters._runtime import AdapterError, validate_content
from ..adapters.repository import AdapterConflict, AdapterRepository
from ..config import load_session_config
from ..http import read_limited_json_body
from . import _accepted_response, _require_tenant_admin


def create_router(services, accept):
    router = APIRouter(tags=["datasource"])
    tx, configs = services.tx, services.extras["config_repo"]
    repo = AdapterRepository(tx)

    async def describe(conn, request=None):
        cfg = await load_session_config(configs, conn)
        rows = await conn.fetch(
            f"SELECT DISTINCT ON(adapter_id) adapter_id,revision,source FROM {tx.pool.schema}.adapter_versions "
            "ORDER BY adapter_id,revision DESC"
        )
        bound = await repo.binding(conn, binding_key="datasource")
        entries = [
            {"file": r["adapter_id"], "revision": r["revision"], **validate_content(r["source"], r["adapter_id"])}
            for r in rows
        ]
        result = {
            "tenant_id": current_tenant_id(),
            "file": bound["adapter_id"] if bound else "",
            "configured": bool(bound),
            "enabled": bool(bound),
            "available": entries,
            "source_editable": bool(request and getattr(request.state, "service_root_authenticated", False)),
            "persistence": {"durable": True, "mode": "postgres"},
            "schedule": cfg.get("datasource_schedule"),
            "config_revision": cfg.revision,
        }
        latest = await conn.fetchrow(
            f"SELECT task_id,status,error_code,created_at,updated_at,result_ref FROM {tx.pool.schema}.worker_tasks "
            "WHERE kind='session.pull' ORDER BY created_at DESC LIMIT 1"
        )
        result["schedule_status"] = {
            "running": bool(
                latest and latest["status"] in {"queued", "running", "retry_wait", "waiting_external", "reconciling"}
            ),
            "job_id": latest["task_id"] if latest else "",
            "job_type": "manual",
            "last_status": latest["status"] if latest else "",
            "last_error": latest["error_code"] if latest else "",
            "last_started_at": latest["created_at"].isoformat() if latest else "",
            "last_finished_at": latest["updated_at"].isoformat() if latest else "",
        }
        if bound:
            version = await repo.get_version(conn, adapter_id=bound["adapter_id"], revision=bound["revision"])
            result.update(validate_content(version["source"], bound["adapter_id"]), revision=bound["revision"])
        return result

    def root(request):
        require_tenant_admin(getattr(request.state, "console_user", None))
        if not getattr(request.state, "service_root_authenticated", False):
            raise HTTPException(403, "service Root Key required for Python adapter source")

    @router.get("/api/datasource")
    async def status(request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return await describe(conn, request)

    @router.put("/api/datasource")
    async def bind(request: Request):
        _require_tenant_admin(request)
        raw = await read_limited_json_body(request)
        if set(raw) - {"file", "revision"} or not isinstance(raw.get("file"), str):
            raise HTTPException(400, "expected file and optional integer revision")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            if not raw["file"]:
                await conn.execute(f"DELETE FROM {tx.pool.schema}.adapter_bindings WHERE binding_key='datasource'")
            else:
                revision = raw.get("revision") or await repo.latest_revision(conn, adapter_id=raw["file"])
                version = await repo.get_version(conn, adapter_id=raw["file"], revision=revision)
                if not version:
                    raise HTTPException(404, "adapter version not found")
                await repo.bind(
                    conn,
                    tenant_id=current_tenant_id(),
                    binding_key="datasource",
                    adapter_id=raw["file"],
                    revision=revision,
                )
            return await describe(conn, request)

    @router.get("/api/datasource/code")
    async def code(request: Request, file: str):
        root(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            revision = await repo.latest_revision(conn, adapter_id=file)
            row = await repo.get_version(conn, adapter_id=file, revision=revision)
        if row is None:
            raise HTTPException(404, "adapter not found")
        return {"file": file, "code": row["source"], "revision": revision}

    @router.put("/api/datasource/code")
    async def save(request: Request):
        root(request)
        raw = await read_limited_json_body(request)
        if set(raw) != {"file", "code", "expected_revision"}:
            raise HTTPException(400, "expected file, code and expected_revision")
        try:
            metadata = validate_content(raw["code"], raw["file"])
            async with tx.tenant_tx(current_tenant_id()) as conn:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    f"{current_tenant_id()}:adapter:{raw['file']}",
                )
                head = await repo.latest_revision(conn, adapter_id=raw["file"])
                if (raw["expected_revision"] or 0) != head:
                    raise HTTPException(409, "adapter changed; reload before saving")
                revision = await repo.save_source(
                    conn, tenant_id=current_tenant_id(), adapter_id=raw["file"], source=raw["code"]
                )
            return {"file": raw["file"], "revision": revision, **metadata}
        except (AdapterError, AdapterConflict, SyntaxError) as exc:
            raise HTTPException(400, str(exc)) from exc

    async def operation(request, mode, raw=None):
        _require_tenant_admin(request)
        raw = raw if raw is not None else await read_limited_json_body(request)
        if set(raw) - {"filters", "max_sessions", "file", "code", "mode", "session_id"}:
            raise HTTPException(400, "expected filters and optional max_sessions")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            await load_session_config(configs, conn)
            if "code" in raw:
                root(request)
                metadata = validate_content(raw["code"], raw["file"])
                if mode == "validate":
                    return {"ok": True, "metadata": {"file": raw["file"], **metadata}}
                filename = "draft-" + hashlib.sha256(raw["code"].encode()).hexdigest() + ".py"
                revision = await repo.save_source(
                    conn, tenant_id=current_tenant_id(), adapter_id=filename, source=raw["code"]
                )
            else:
                bound = await repo.binding(conn, binding_key="datasource")
                if not bound:
                    raise HTTPException(409, "bind an adapter before submitting")
                filename, revision = bound["adapter_id"], bound["revision"]
        filters = raw.get("filters", {})
        if not isinstance(filters, dict):
            raise HTTPException(400, "filters must be an object")
        limit = raw.get("max_sessions", 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise HTTPException(400, "max_sessions must be 1..1000")
        pull = {"filters": filters, "operation": mode, "max_sessions": limit}
        if mode == "session":
            pull["session_id"] = raw.get("session_id", "")
        job = await accept(
            source="pull",
            external_id="",
            user_id="",
            account_id="",
            session={"_pull": pull},
            pull=pull,
            adapter_id=filename,
            adapter_revision=revision,
            kind="session.pull",
        )
        return _accepted_response(job)

    @router.post("/api/datasource/code/test")
    async def draft_test(request: Request):
        raw = await read_limited_json_body(request)
        if raw.get("mode") not in {"validate", "health", "preview", "session"}:
            raise HTTPException(400, "invalid adapter test mode")
        return await operation(request, raw["mode"], raw)

    @router.post("/api/datasource/test", status_code=202)
    async def health(request: Request):
        return await operation(request, "health", {})

    @router.post("/api/datasource/sessions", status_code=202)
    async def preview(request: Request):
        return await operation(request, "preview")

    @router.get("/api/datasource/jobs/{job_id}/result")
    async def result(job_id: str, request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            task = await services.tasks.get(conn, task_id=job_id)
        if not task or task["kind"] != "session.pull":
            raise HTTPException(404, "datasource job not found")
        if not task["result_ref"]:
            return {"result": None}
        return {"result": services.extras["session_payloads"].get_session(task["result_ref"])}

    @router.get("/api/datasource/schedule")
    async def schedule(request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return await describe(conn, request)

    @router.put("/api/datasource/schedule")
    async def save_schedule(request: Request):
        _require_tenant_admin(request)
        from ..schedule import validate_schedule

        raw = await read_limited_json_body(request)
        try:
            value = validate_schedule(raw)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                cfg = await load_session_config(configs, conn)
                if value["enabled"] and not await repo.binding(conn, binding_key="datasource"):
                    raise ValueError("bind an adapter before enabling schedule")
                await configs.write_tenant_module(
                    conn,
                    module="session",
                    expected_revision=cfg.revision,
                    config={**cfg.config, "datasource_schedule": value},
                )
                return await describe(conn, request)
        except (ValueError, ConfigRevisionConflict, TenantConfigMissing) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/datasource/schedule/run", status_code=202)
    async def run_schedule(request: Request):
        _require_tenant_admin(request)
        from ..schedule import yesterday_filters

        async with tx.tenant_tx(current_tenant_id()) as conn:
            cfg = await load_session_config(configs, conn)
            bound = await repo.binding(conn, binding_key="datasource")
        if not bound:
            raise HTTPException(409, "adapter is not bound")
        window = yesterday_filters(cfg.get("datasource_schedule"))
        job = await accept(
            source="pull",
            external_id="",
            user_id="",
            account_id="",
            session={"_pull": window},
            pull={**window, "request_id": uuid.uuid4().hex},
            adapter_id=bound["adapter_id"],
            adapter_revision=bound["revision"],
            kind="session.pull",
        )
        return _accepted_response(job)

    return router
