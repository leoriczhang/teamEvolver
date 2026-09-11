"""Tenant management REST API (multi-tenancy plan Phase 1).

Admin-only surface backing the console's future tenant-management page:
list / create (returns the plaintext agent token exactly once) / rotate
token / enable-disable. All mutations are refused in single-tenant mode
(``storage_pg`` disabled) with a 409, keeping non-PG deployments unchanged.

Implemented as module-level functions called from ``RoutesMixin._build_app``
(instead of a mixin) so test doubles that assemble partial ProxyServers keep
working. Imports from ``routes`` are deferred to avoid a circular import.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from ..tenants.registry import TenantRegistry

logger = logging.getLogger(__name__)


def get_tenant_registry(owner) -> TenantRegistry:
    """Process-wide registry, lazily built from the owner's current config."""
    registry = getattr(owner, "_tenant_registry", None)
    if registry is None:
        registry = TenantRegistry(getattr(owner, "config", None))
        owner._tenant_registry = registry
    return registry


def register_tenant_routes(owner, app) -> None:
    from .routes import _read_limited_json_body, _require_admin_user

    async def project_config(tenant_id, request):
        from ..tenants.registry import effective_config

        _require_admin_user(getattr(request.state, "console_user", None))
        ctx = await asyncio.to_thread(get_tenant_registry(owner).get, tenant_id)
        if ctx is None or ctx.status != "active":
            raise HTTPException(status_code=404, detail="project not found or disabled")
        return effective_config(None, ctx, owner.config)

    @app.get("/api/tenants/{tenant_id}/langfuse-config")
    async def project_langfuse(tenant_id: str, request: Request):
        from .routes import _langfuse_settings_payload

        return _langfuse_settings_payload(await project_config(tenant_id, request), {})

    @app.get("/api/tenants/{tenant_id}/datasource-config")
    async def project_datasource(tenant_id: str, request: Request):
        from .routes import _datasource_settings_payload

        return _datasource_settings_payload(await project_config(tenant_id, request), {})

    @app.post("/api/tenants/{tenant_id}/langfuse-config")
    async def save_project_langfuse(tenant_id: str, request: Request):
        from urllib.parse import urlsplit
        from .routes import _langfuse_settings_payload
        from ..tenants.registry import effective_config
        from ..integrations.langfuse_mapper import normalize_mapper_entries

        config = await project_config(tenant_id, request)
        if tenant_id == "default":
            raise HTTPException(status_code=400, detail="edit default settings through the service settings")
        body = await _read_limited_json_body(request)
        allowed = {"enabled", "host", "public_key", "secret_key", "max_sessions", "default_environment",
                   "default_user_id", "default_tags", "default_trace_name", "mappers"}
        overrides = {}
        for key, value in body.items():
            if key.startswith("tracing_"):
                raise HTTPException(
                    status_code=409,
                    detail="tracing settings are service-wide; use /api/langfuse-tracing-config",
                )
            if key not in allowed:
                raise HTTPException(status_code=400, detail=f"unsupported project setting: {key}")
            if key in {"public_key", "secret_key"} and not value:
                continue
            if key == "host" and (not isinstance(value, str) or urlsplit(value).scheme not in {"http", "https"}):
                raise HTTPException(status_code=400, detail="host must be HTTP(S)")
            if key == "enabled" and not isinstance(value, bool):
                raise HTTPException(status_code=400, detail="enabled must be boolean")
            if key == "max_sessions" and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000):
                raise HTTPException(status_code=400, detail="max_sessions must be between 1 and 1000")
            if key in {"default_environment", "default_tags"} and not isinstance(value, list):
                raise HTTPException(status_code=400, detail=f"{key} must be an array")
            if key == "mappers":
                value = normalize_mapper_entries(value)
            overrides["langfuse_" + key] = value
        if overrides:
            ctx = await asyncio.to_thread(get_tenant_registry(owner).update_tenant_config, tenant_id, overrides)
            config = effective_config(None, ctx, owner.config)
            pool = owner._get_engine_pool()
            if pool is not None:
                await asyncio.to_thread(pool.drop, tenant_id, reason="project settings updated")
        return _langfuse_settings_payload(config, {})

    @app.post("/api/tenants/{tenant_id}/langfuse-config/test")
    async def test_project_langfuse(tenant_id: str, request: Request):
        from ..integrations.langfuse_client import LangfuseClient

        config = await project_config(tenant_id, request)
        body = await _read_limited_json_body(request)
        client = LangfuseClient(
            body.get("host") or config.langfuse_host,
            body.get("public_key") or config.langfuse_public_key,
            body.get("secret_key") or config.langfuse_secret_key,
            timeout=config.langfuse_timeout_seconds,
        )
        try:
            return await asyncio.to_thread(client.health)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await asyncio.to_thread(client.close)

    @app.post("/api/tenants/{tenant_id}/converter/check")
    async def check_converter(tenant_id: str, request: Request):
        from ..integrations.legacy_converter import inspect_converter

        _require_admin_user(getattr(request.state, "console_user", None))
        body = await _read_limited_json_body(request)
        return inspect_converter(str(body.get("code") or ""))

    @app.post("/api/tenants/{tenant_id}/converter/test")
    async def test_converter(tenant_id: str, request: Request):
        import json
        import subprocess
        import sys

        _require_admin_user(getattr(request.state, "console_user", None))
        body = await _read_limited_json_body(request)
        encoded = json.dumps(body).encode()
        if len(encoded) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="converter preview input exceeds 1 MiB")
        try:
            result = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-m", "teamEvolver.integrations.legacy_converter"],
                input=encoded, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=422, detail="converter exceeded 5 second preview limit") from exc
        if result.returncode != 0:
            raise HTTPException(status_code=422, detail="converter worker failed")
        output = json.loads(result.stdout)
        if "error" in output:
            raise HTTPException(status_code=422, detail=output["error"])
        return output

    @app.get("/api/tenants")
    async def list_tenants(request: Request):
        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        tenants = await asyncio.to_thread(registry.list_tenants)
        return {
            "mode": registry.mode,
            "tenants": [
                {
                    "tenant_id": t.tenant_id,
                    "display_name": t.display_name,
                    "status": t.status,
                }
                for t in tenants
            ],
        }

    @app.post("/api/tenants")
    async def create_tenant(request: Request):
        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        body = await _read_limited_json_body(request)
        display_name = str((body or {}).get("display_name") or "").strip() or "Untitled tenant"
        try:
            ctx, token = await asyncio.to_thread(
                registry.create_tenant, display_name, str((body or {}).get("account_id") or "")
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        logger.info("[Tenants] created tenant %s", ctx.tenant_id)
        # The plaintext token is returned exactly once; only its sha256 is
        # persisted (same policy as agent_registry).
        return {
            "tenant": {
                "tenant_id": ctx.tenant_id,
                "display_name": ctx.display_name,
                "status": ctx.status,
            },
            "agent_token": token,
        }

    @app.post("/api/tenants/{tenant_id}/rotate-token")
    async def rotate_tenant_token(tenant_id: str, request: Request):
        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        try:
            token = await asyncio.to_thread(registry.rotate_token, tenant_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if token is None:
            raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
        return {"agent_token": token}

    @app.post("/api/tenants/{tenant_id}/status")
    async def set_tenant_status(tenant_id: str, request: Request):
        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        body = await _read_limited_json_body(request)
        status = str((body or {}).get("status") or "").strip().lower()
        try:
            changed = await asyncio.to_thread(registry.set_status, tenant_id, status)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not changed:
            raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
        return {"tenant_id": tenant_id, "status": status}

    # -- tenant config (multi-tenancy §1.3 self-service onboarding) ----------- #

    def _tenant_config_keys() -> set[str]:
        """Keys a console admin may write into tenants.config.

        Flat ``TeamEvolverConfig`` field names (``apply_tenant_config_overrides``
        contract) plus the scheduler-level quota keys. Anything else is rejected
        with a 400 so typos never become silently-ignored overrides.
        """
        import dataclasses

        from ..config import TeamEvolverConfig
        from ..tenants.registry import QUOTA_KEYS, SERVICE_WIDE_CONFIG_KEYS

        fields = {
            f.name for f in dataclasses.fields(TeamEvolverConfig)
            if not f.name.startswith(("_", "storage_pg_", "proxy_"))
        }
        fields -= {
            "sharing_viking_account", "users_registry_path", "skills_dir", "skills_public_root",
            "sharing_local_root", "sharing_local_fallback_enabled", "sharing_skill_mirror_spool_dir",
        }
        fields -= set(SERVICE_WIDE_CONFIG_KEYS)
        return fields | set(QUOTA_KEYS)

    @app.get("/api/tenants/{tenant_id}/config")
    async def get_tenant_config(tenant_id: str, request: Request):
        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        try:
            ctx = await asyncio.to_thread(registry.get, tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if ctx is None:
            raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
        return {
            "tenant_id": ctx.tenant_id,
            "display_name": ctx.display_name,
            # default 租户的生效配置就是全局 config.yaml，overrides 恒为空
            "config_overrides": dict(ctx.config_overrides or {}),
            "editable_keys": sorted(_tenant_config_keys()),
        }

    @app.put("/api/tenants/{tenant_id}/config")
    async def put_tenant_config(tenant_id: str, request: Request):
        from ..tenants.registry import (
            DEFAULT_TENANT_ID,
            apply_tenant_config_overrides,
        )

        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        if tenant_id == DEFAULT_TENANT_ID:
            raise HTTPException(
                status_code=400,
                detail="default 租户直接使用全局 config.yaml，不支持 overrides；请编辑配置文件",
            )
        body = await _read_limited_json_body(request)
        overrides = (body or {}).get("overrides")
        if not isinstance(overrides, dict) or not overrides:
            raise HTTPException(status_code=400, detail="overrides must be a non-empty object")
        # JSON null 删除该键（_update_config 语义）；这里先剥掉再校验。
        unknown = sorted(
            str(k) for k in overrides if str(k) not in _tenant_config_keys()
        )
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown config keys (ignored by tenants scope): {', '.join(unknown)}",
            )
        base_config = getattr(owner, "config", None)
        if base_config is not None:
            # 类型预检：合并后的覆盖必须能套到 TeamEvolverConfig 上（比如把
            # 数字字段写成字符串会在这里失败，而不是在下次请求时才炸）。
            try:
                registry_ctx = await asyncio.to_thread(registry.get, tenant_id)
                if registry_ctx is None:
                    raise HTTPException(
                        status_code=404, detail=f"unknown tenant: {tenant_id}"
                    )
                merged = dict(registry_ctx.config_overrides or {})
                for key, value in overrides.items():
                    if value is None:
                        merged.pop(key, None)
                    else:
                        merged[key] = value
                apply_tenant_config_overrides(base_config, merged)
                if merged.get("datasource_type") == "skillopt":
                    from ..integrations.legacy_converter import inspect_converter

                    validation = inspect_converter(str(merged.get("datasource_legacy_converter_code") or ""))
                    if validation["issues"]:
                        raise ValueError("; ".join(validation["issues"]))
            except HTTPException:
                raise
            except TypeError as exc:
                raise HTTPException(status_code=400, detail=f"invalid override value: {exc}")
            except Exception as exc:  # noqa: BLE001 - value coercion errors
                raise HTTPException(status_code=400, detail=f"invalid overrides: {exc}")
        try:
            ctx = await asyncio.to_thread(registry.update_tenant_config, tenant_id, overrides)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if ctx is None:
            raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
        if tenant_id != DEFAULT_TENANT_ID:
            # 让常驻引擎/内置 app 在下次使用时按新配置重建。
            pool = owner._get_engine_pool() if hasattr(owner, "_get_engine_pool") else None
            if pool is not None:
                await asyncio.to_thread(pool.drop, tenant_id, reason="config updated")
            apps = getattr(owner, "_embedded_evolve_apps", None)
            if isinstance(apps, dict):
                apps.pop(tenant_id, None)
        logger.info("[Tenants] config updated for tenant %s (%d keys)", tenant_id, len(overrides))
        return {
            "tenant_id": ctx.tenant_id,
            "config_overrides": dict(ctx.config_overrides or {}),
        }
