"""Tenant datasource control plane for pull-based Session ingestion."""

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from session_ingestion.adapters import _runtime as adapters
from session_ingestion.http import read_limited_json_body
from teamEvolver.config_store import ConfigStore
from teamEvolver.proxy.tenant_routes import get_tenant_registry
from teamEvolver.tenants.registry import get_current_tenant

from .scheduler import (
    PullBusyError,
    PullCapacityError,
    get_pull_runtime,
    normalize_schedule,
    schedule_for,
    validate_schedule_descriptor,
)


def register_pull_routes(
    owner: Any,
    app: Any,
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> None:

    binding_lock = asyncio.Lock()
    pull_runtime = get_pull_runtime(owner, invalidate_cache=invalidate_cache)

    def admin(request):
        user = getattr(request.state, "console_user", None)
        if not user or str(user.get("role") or "user") != "admin":
            raise HTTPException(
                status_code=403,
                detail="only admin users can perform this operation",
            )

    async def claims() -> dict[str, str]:
        registry = get_tenant_registry(owner)
        tenants = await asyncio.to_thread(registry.list_tenants)
        claimed: dict[str, str] = {}
        default_file = adapters.binding(owner.config, None)
        if default_file:
            claimed[default_file] = "default"
        for tenant in tenants:
            filename = adapters.binding(owner.config, tenant)
            if filename:
                claimed[filename] = tenant.tenant_id
        return claimed

    async def catalog() -> list[dict]:
        entries = await asyncio.to_thread(adapters.available, owner.config)
        claimed = await claims()
        for entry in entries:
            entry["bound_tenant_id"] = claimed.get(entry["file"], "")
        return entries

    async def save_schedule(tenant, schedule: dict):
        if tenant.is_default():
            config_file = str(getattr(owner.config, "_config_file", "") or "").strip()
            store = ConfigStore(Path(config_file)) if config_file else ConfigStore()
            data = await asyncio.to_thread(store.load)
            data.setdefault("datasource", {})["schedule"] = schedule
            await asyncio.to_thread(store.save, data)
            owner.config.datasource_schedule = schedule
            return tenant
        else:
            updated = await asyncio.to_thread(
                get_tenant_registry(owner).update_tenant_config,
                tenant.tenant_id,
                {"datasource_schedule": schedule},
            )
            if updated is None:
                raise ValueError("Tenant no longer exists")
            return updated

    async def require_file_access(filename: str, tenant) -> None:
        bound_tenant_id = (await claims()).get(filename, "")
        if bound_tenant_id and bound_tenant_id != tenant.tenant_id:
            raise HTTPException(409, "This adapter is bound to another tenant")

    def persistence() -> dict:
        return {
            "durable": False,
            "mode": "runtime_only",
            "warning": adapters.PERSISTENCE_WARNING,
        }

    @app.get("/api/datasource")
    async def get_datasource(request: Request):
        admin(request)
        result = await asyncio.to_thread(
            adapters.describe,
            owner.config,
            get_current_tenant(),
        )
        result["available"] = await catalog()
        result["persistence"] = persistence()
        tenant = get_current_tenant()
        result["schedule"] = schedule_for(owner.config, tenant)
        result["schedule_status"] = await asyncio.to_thread(
            pull_runtime.status,
            tenant,
        )
        return result

    @app.put("/api/datasource")
    async def bind_datasource(request: Request):
        admin(request)
        body = await read_limited_json_body(request)
        if set(body) != {"file"} or not isinstance(body["file"], str):
            raise HTTPException(400, "Expected {file: string}")
        filename = body["file"]
        tenant = get_current_tenant()
        async with binding_lock:
            if pull_runtime.is_running(tenant.tenant_id):
                raise HTTPException(409, "Cannot change adapter during a pull")
            try:
                if filename:
                    path = adapters.adapter_path(owner.config, filename)
                    await asyncio.to_thread(adapters.metadata, path)
                    await require_file_access(filename, tenant)
                if tenant.is_default():
                    config_file = str(getattr(owner.config, "_config_file", "") or "").strip()
                    store = ConfigStore(Path(config_file)) if config_file else ConfigStore()
                    data = await asyncio.to_thread(store.load)
                    data.setdefault("datasource", {})["adapter"] = filename
                    await asyncio.to_thread(store.save, data)
                    owner.config.datasource_adapter = filename
                else:
                    await asyncio.to_thread(
                        get_tenant_registry(owner).update_tenant_config,
                        tenant.tenant_id,
                        {"datasource_adapter": filename},
                    )
            except HTTPException:
                raise
            except (ValueError, SyntaxError) as exc:
                raise HTTPException(400, str(exc)) from exc
        pull_runtime.wake()
        return {
            "file": filename,
            "tenant_id": tenant.tenant_id,
            "persistence": persistence(),
        }

    @app.get("/api/datasource/schedule")
    async def get_datasource_schedule(request: Request):
        admin(request)
        tenant = get_current_tenant()
        return {
            "schedule": schedule_for(owner.config, tenant),
            "schedule_status": await asyncio.to_thread(
                pull_runtime.status,
                tenant,
            ),
        }

    @app.put("/api/datasource/schedule")
    async def put_datasource_schedule(request: Request):
        admin(request)
        tenant = get_current_tenant()
        body = await read_limited_json_body(request)
        try:
            schedule = normalize_schedule(body)
            if schedule["enabled"]:
                descriptor = await asyncio.to_thread(
                    adapters.describe,
                    owner.config,
                    tenant,
                )
                validate_schedule_descriptor(descriptor)
            tenant = await save_schedule(tenant, schedule)
        except (ValueError, SyntaxError) as exc:
            raise HTTPException(400, str(exc)) from exc
        pull_runtime.wake()
        return {
            "schedule": schedule,
            "schedule_status": await asyncio.to_thread(
                pull_runtime.status,
                tenant,
            ),
        }

    @app.post("/api/datasource/schedule/run")
    async def run_datasource_schedule(request: Request):
        admin(request)
        tenant = get_current_tenant()
        try:
            schedule = schedule_for(owner.config, tenant)
            if not schedule["enabled"]:
                raise HTTPException(409, "Datasource pull schedule is disabled")
            status = await pull_runtime.trigger(tenant, force=True)
        except HTTPException:
            raise
        except (PullBusyError, PullCapacityError) as exc:
            raise HTTPException(429, str(exc)) from exc
        except (ValueError, SyntaxError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(
            status_code=202,
            content={"accepted": True, "schedule_status": status},
        )

    @app.get("/api/datasource/code")
    async def datasource_code(request: Request):
        admin(request)
        tenant = get_current_tenant()
        filename = str(request.query_params.get("file") or adapters.binding(owner.config, tenant) or "").strip()
        await require_file_access(filename, tenant)
        try:
            return await asyncio.to_thread(
                adapters.read_content,
                owner.config,
                filename,
            )
        except adapters.AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/datasource/code")
    async def save_datasource_code(request: Request):
        admin(request)
        tenant = get_current_tenant()
        body = await read_limited_json_body(request)
        if set(body) != {"file", "code", "expected_revision"}:
            raise HTTPException(
                400,
                "Expected {file: string, code: string, expected_revision: string|null}",
            )
        filename = body.get("file")
        code = body.get("code")
        expected_revision = body.get("expected_revision")
        if not isinstance(filename, str) or not isinstance(code, str):
            raise HTTPException(400, "file and code must be strings")
        if expected_revision is not None and not isinstance(expected_revision, str):
            raise HTTPException(400, "expected_revision must be a string or null")
        async with binding_lock:
            if pull_runtime.is_running(tenant.tenant_id):
                raise HTTPException(409, "Cannot change adapter during a pull")
            await require_file_access(filename, tenant)
            try:
                return await asyncio.to_thread(
                    adapters.save_content,
                    owner.config,
                    filename,
                    code,
                    expected_revision,
                )
            except adapters.AdapterConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except adapters.AdapterStorageError as exc:
                raise HTTPException(503, str(exc)) from exc
            except SyntaxError as exc:
                raise HTTPException(
                    400,
                    f"Python syntax error at line {exc.lineno}: {exc.msg}",
                ) from exc
            except adapters.AdapterError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.post("/api/datasource/code/test")
    async def test_datasource_code(request: Request):
        admin(request)
        tenant = get_current_tenant()
        body = await read_limited_json_body(request)
        allowed = {
            "file",
            "code",
            "mode",
            "filters",
            "max_sessions",
            "session_id",
        }
        unknown = set(body) - allowed
        if unknown:
            raise HTTPException(
                400,
                f"Unsupported fields: {', '.join(sorted(unknown))}",
            )
        filename = body.get("file")
        code = body.get("code")
        mode = str(body.get("mode") or "validate")
        if not isinstance(filename, str) or not isinstance(code, str):
            raise HTTPException(400, "file and code must be strings")
        filters = body.get("filters")
        if filters is None:
            filters = {}
        if not isinstance(filters, dict):
            raise HTTPException(400, "filters must be an object")
        test_input = dict(filters)
        if "max_sessions" in body:
            test_input["max_sessions"] = body["max_sessions"]
        if "session_id" in body:
            test_input["session_id"] = body["session_id"]
        await require_file_access(filename, tenant)
        try:
            return await asyncio.to_thread(
                adapters.test_content,
                code,
                filename,
                mode,
                test_input,
            )
        except SyntaxError as exc:
            raise HTTPException(
                400,
                f"Python syntax error at line {exc.lineno}: {exc.msg}",
            ) from exc
        except adapters.AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                422,
                f"Adapter draft execution failed: {type(exc).__name__}: {exc}",
            ) from exc

    @app.post("/api/datasource/test")
    async def test_datasource(request: Request):
        admin(request)
        try:
            return await asyncio.to_thread(
                adapters.probe,
                owner.config,
                get_current_tenant(),
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/datasource/sessions")
    @app.post("/langfuse/sessions", include_in_schema=False)
    async def list_sessions(request: Request):
        admin(request)
        body = await read_limited_json_body(request)
        try:
            return await asyncio.to_thread(
                adapters.preview,
                owner.config,
                get_current_tenant(),
                body,
            )
        except adapters.AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/datasource/pull")
    @app.post("/langfuse/pull", include_in_schema=False)
    async def pull(request: Request):
        tenant = get_current_tenant()
        if getattr(request.state, "tenant_source", "") != "token":
            admin(request)
        body = await read_limited_json_body(request)
        try:
            return await pull_runtime.pull(
                tenant,
                body,
            )
        except (PullBusyError, PullCapacityError) as exc:
            raise HTTPException(429, str(exc)) from exc
        except adapters.AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    async def retired(_request: Request):
        raise HTTPException(
            410,
            "Upstream settings moved to tenant .py files in "
            "session_ingestion/adapters/; use /api/datasource",
        )

    for path in (
        "/api/langfuse-config",
        "/api/langfuse-config/{rest:path}",
        "/api/datasource-config",
        "/api/datasource-config/{rest:path}",
        "/api/tenants/{tenant_id}/langfuse-config",
        "/api/tenants/{tenant_id}/langfuse-config/{rest:path}",
        "/api/tenants/{tenant_id}/datasource-config",
        "/api/tenants/{tenant_id}/converter/{rest:path}",
        "/langfuse/status",
        "/langfuse/mapper/{rest:path}",
    ):
        app.add_api_route(
            path,
            retired,
            methods=["GET", "POST", "PUT"],
            include_in_schema=False,
        )
