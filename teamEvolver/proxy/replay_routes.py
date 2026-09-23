"""Replay binding control plane; Python source is restricted to the service owner."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException, Request

from session_ingestion.http import read_limited_json_body
from team_replay import adapter_runtime as adapters
from team_replay.hooks import ReplayContext, ReplayTreatment, validate_observation
from teamEvolver.config_store import ConfigStore
from teamEvolver.tenants.registry import effective_config, get_current_tenant
from .tenant_routes import get_tenant_registry


def register_replay_adapter_routes(owner, app):
    binding_lock = asyncio.Lock()

    def admin(request):
        user = getattr(request.state, "console_user", None)
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "console admin required")

    def root(request):
        if not getattr(request.state, "service_root_authenticated", False):
            raise HTTPException(403, "service Root Key required for Python source access")

    def config():
        return effective_config(get_tenant_registry(owner), get_current_tenant(), owner.config)

    @app.get("/api/replay-adapter")
    async def get_adapter(request: Request):
        admin(request)
        result = await asyncio.to_thread(adapters.describe, config(), get_current_tenant())
        result["available"] = await asyncio.to_thread(adapters.available, owner.config)
        result["source_editable"] = bool(getattr(request.state, "service_root_authenticated", False))
        return result

    @app.put("/api/replay-adapter")
    async def bind_adapter(request: Request):
        admin(request)
        body = await read_limited_json_body(request)
        if set(body) != {"file"} or not isinstance(body["file"], str):
            raise HTTPException(400, "Expected {file: string}")
        tenant = get_current_tenant()
        filename = body["file"]
        async with binding_lock:
            try:
                if filename:
                    source = await asyncio.to_thread(adapters.read_content, owner.config, filename)
                    metadata = adapters.validate_content(source["code"], filename)
                    if not metadata["enabled"]:
                        raise adapters.AdapterError("Replay adapter is disabled")
                if tenant is None or tenant.is_default():
                    config_file = getattr(owner.config, "_config_file", "")
                    store = ConfigStore(Path(config_file)) if config_file else ConfigStore()
                    data = await asyncio.to_thread(store.load)
                    data.setdefault("replay", {})["adapter"] = filename
                    await asyncio.to_thread(store.save, data)
                    owner.config.replay_adapter = filename
                else:
                    updated = await asyncio.to_thread(get_tenant_registry(owner).update_tenant_config,
                                                      tenant.tenant_id, {"replay_adapter": filename})
                    if updated is None:
                        raise HTTPException(404, "tenant not found")
            except adapters.AdapterError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"file": filename, "tenant_id": tenant.tenant_id if tenant else "default"}

    @app.get("/api/replay-adapter/code")
    async def get_code(request: Request):
        root(request)
        filename = request.query_params.get("file") or adapters.binding(config(), get_current_tenant())
        try:
            return await asyncio.to_thread(adapters.read_content, owner.config, filename)
        except adapters.AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/replay-adapter/code")
    async def save_code(request: Request):
        root(request)
        body = await read_limited_json_body(request)
        if set(body) != {"file", "code", "expected_revision"}:
            raise HTTPException(400, "Expected {file, code, expected_revision}")
        if not isinstance(body["file"], str) or not isinstance(body["code"], str):
            raise HTTPException(400, "file and code must be strings")
        if body["expected_revision"] is not None and not isinstance(body["expected_revision"], str):
            raise HTTPException(400, "expected_revision must be a string or null")
        try:
            return await asyncio.to_thread(adapters.save_content, owner.config, body["file"], body["code"], body["expected_revision"])
        except adapters.AdapterConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except adapters.AdapterError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/replay-adapter/code/test")
    async def test_code(request: Request):
        root(request)
        body = await read_limited_json_body(request)
        if set(body) != {"file", "code", "context", "query"}:
            raise HTTPException(400, "Expected explicit {file, code, context, query}")
        raw = body["context"]
        if not isinstance(raw, dict) or set(raw) - {"runtime_type", "skill", "materials", "context_snapshot", "timeout_seconds"}:
            raise HTTPException(400, "Invalid test context")
        if not isinstance(body["query"], str) or not body["query"].strip() or len(body["query"]) > 32000:
            raise HTTPException(400, "query must contain 1..32000 characters")
        if not isinstance(raw.get("runtime_type"), str) or not raw["runtime_type"].strip():
            raise HTTPException(400, "context.runtime_type is required")
        if raw.get("skill") is not None and not isinstance(raw["skill"], dict):
            raise HTTPException(400, "context.skill must be an object or null")
        materials = raw.get("materials", [])
        if not isinstance(materials, list) or any(not isinstance(item, dict) for item in materials):
            raise HTTPException(400, "context.materials must be a list of objects")
        if not isinstance(raw.get("context_snapshot", {}), dict):
            raise HTTPException(400, "context.context_snapshot must be an object")
        try:
            timeout = max(1, min(120, int(raw.get("timeout_seconds") or 30)))
            context = ReplayContext("test_" + uuid.uuid4().hex, raw["runtime_type"], ReplayTreatment("baseline", raw.get("skill")),
                                    tuple(materials), raw.get("context_snapshot", {}), timeout)
            cfg = config()
            tenant = get_current_tenant()

            def execute():
                factory = adapters.build_from_content(body["code"], body["file"], cfg,
                                                       tenant_id=tenant.tenant_id if tenant else "default")
                session = factory.open(context)
                try:
                    return {"observation": asdict(validate_observation(session.send(body["query"])))}
                finally:
                    session.close()
            return await asyncio.to_thread(execute)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(422, f"Adapter test failed: {exc}") from exc
