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








    @app.get("/api/tenants")
    async def list_tenants(request: Request):
        _require_admin_user(getattr(request.state, "console_user", None))
        registry = get_tenant_registry(owner)
        tenants = await asyncio.to_thread(registry.list_tenants)
        return {
            "mode": registry.mode,
            # Single-tenant deployments configure one credential instead of
            # issuing per-tenant ones; only its presence is ever exposed.
            "machine_credential": {
                "configured": registry.default_machine_credential_configured,
                "env_var": "TEAMEVOLVER_TENANT_TOKEN",
                "prefix": "tevt_",
            },
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
        # Best-effort OpenViking account provisioning: an account named after
        # the tenant_id is created as the default binding; the admin can freely
        # re-point the tenant to a different OV account in the console (the
        # account is no longer locked to the tenant_id).
        # If the account already exists it is reused; otherwise it is created.
        # Fail-open: errors do not roll back the PG tenant; the result is
        # returned so the console can warn the operator to provision manually.
        from .users_admin import ensure_openviking_account

        viking = await asyncio.to_thread(
            ensure_openviking_account, owner.config, ctx.tenant_id, "team"
        )
        # Persist the provisioned OpenViking account as the tenant's default
        # binding in config_overrides, so the effective config points at this
        # account even though it is no longer forced to equal the tenant_id.
        # The admin may later re-point it to a different account from the
        # console (运行状态 → OpenViking 部署).
        default_account = str(viking.get("account_id") or ctx.tenant_id).strip() or ctx.tenant_id
        try:
            await asyncio.to_thread(
                registry.update_tenant_config,
                ctx.tenant_id,
                {"sharing_viking_account": default_account},
            )
        except Exception as exc:  # noqa: BLE001 - binding is best-effort
            logger.warning("[Tenants] failed to persist default OV account for %s: %s", ctx.tenant_id, exc)
        # Best-effort directory bootstrap: a brand-new OpenViking account has an
        # empty namespace, so the console workspace / skill sync / team-memory
        # targets would all report "Directory not found" until the skeleton
        # exists. Existing directories are kept untouched. Fail-open like the
        # account provisioning above.
        from .viking_dirs import ensure_openviking_dirs

        try:
            dirs = await asyncio.to_thread(
                ensure_openviking_dirs,
                owner.config,
                account_id=default_account,
                extra_users=[str(viking.get("admin_user") or "team")],
            )
        except Exception as exc:  # noqa: BLE001 - bootstrap is best-effort
            logger.warning("[Tenants] OpenViking dir bootstrap failed for %s: %s", ctx.tenant_id, exc)
            dirs = {"action": "failed", "error": str(exc), "created": [], "existing": [], "errors": []}
        # The plaintext token is returned exactly once; only its sha256 is
        # persisted (same policy as agent_registry).
        return {
            "tenant": {
                "tenant_id": ctx.tenant_id,
                "display_name": ctx.display_name,
                "status": ctx.status,
            },
            "agent_token": token,
            "openviking_account": {
                "account_id": viking.get("account_id"),
                "action": viking.get("action"),
                "error": viking.get("error"),
            },
            "openviking_dirs": {
                "action": dirs.get("action"),
                "account_id": dirs.get("account_id"),
                "created": len(dirs.get("created") or []),
                "existing": len(dirs.get("existing") or []),
                "errors": dirs.get("errors") or [],
                "error": dirs.get("error"),
            },
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
            "users_registry_path", "skills_dir", "skills_public_root",
            "sharing_local_root", "sharing_local_fallback_enabled", "sharing_skill_mirror_spool_dir",
        }
        fields -= set(SERVICE_WIDE_CONFIG_KEYS)
        fields = {key for key in fields if not key.startswith(("langfuse_", "datasource_"))}
        return fields | set(QUOTA_KEYS)

    def _public_tenant_overrides(overrides) -> tuple[dict, dict[str, bool]]:
        public = dict(overrides or {})
        secret_presence = {
            "llm_api_key": bool(str(public.pop("llm_api_key", "") or "")),
        }
        return public, secret_presence

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
        public_overrides, secret_presence = _public_tenant_overrides(
            ctx.config_overrides
        )
        return {
            "tenant_id": ctx.tenant_id,
            "display_name": ctx.display_name,
            # default 租户的生效配置就是全局 config.yaml，overrides 恒为空
            "config_overrides": public_overrides,
            "secret_presence": secret_presence,
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
        public_overrides, secret_presence = _public_tenant_overrides(
            ctx.config_overrides
        )
        return {
            "tenant_id": ctx.tenant_id,
            "config_overrides": public_overrides,
            "secret_presence": secret_presence,
        }
