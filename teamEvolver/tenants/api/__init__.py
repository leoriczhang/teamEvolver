"""Tenant control-plane APIs using injected configuration and registry.

Platform administrators create tenants, rotate their machine token and change
status. Configuration writes validate types and use revision compare-and-swap.
Tokens are returned only when issued; persisted records contain hashes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from ...admin.authorization import (
    authorize_target,
    available_tenants_for,
    is_platform_admin,
    require_platform_admin,
)
from ...admin.identity_store import IdentityStore
from ..registry import (
    DEFAULT_TENANT_ID,
    QUOTA_KEYS,
    SERVICE_WIDE_CONFIG_KEYS,
    TenantRegistry,
    apply_tenant_config_overrides,
)

logger = logging.getLogger(__name__)

# Config keys a (non-platform) tenant admin may self-serve. Platform admins get
# the full ``_tenant_config_keys`` set computed from the config dataclass.
TENANT_CONFIG_KEYS = {
    "llm_provider", "llm_api_base", "llm_model_id", "llm_api_key", "llm_max_tokens",
    "llm_temperature", "llm_max_concurrency", "llm_queue_capacity", "llm_request_timeout_seconds",
    "max_concurrent_sessions", "max_evolve_per_day",
}

_MAX_CONFIG_BODY_BYTES = 1_000_000


def create_router(services=None, *, lifecycle=None, **kwargs) -> APIRouter:
    """Return the tenants ``APIRouter`` for the composition root to mount.

    ``services.extras`` must carry ``config`` (effective config provider). The
    ``TenantRegistry`` is taken from ``extras['tenant_registry']`` when injected
    (tests / shared control pool) and otherwise built lazily from config. Without
    config the factory returns a valid empty router (offline route inspection).
    """
    router = APIRouter(tags=["tenants"])
    extras = getattr(services, "extras", None) or {}
    config = extras.get("config")
    if config is None:
        return router

    registry_provider = _registry_provider(extras, config)
    _register_tenant_routes(router, config, registry_provider)
    return router


def _registry_provider(extras, config):
    injected = extras.get("tenant_registry")
    if injected is not None:
        return lambda: injected

    cache: dict[str, Any] = {}

    def provider():
        registry = cache.get("registry")
        if registry is None:
            registry = TenantRegistry(config)
            cache["registry"] = registry
        return registry

    return provider


async def _read_limited_json_body(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_CONFIG_BODY_BYTES:
            raise HTTPException(status_code=413, detail=f"body exceeds {_MAX_CONFIG_BODY_BYTES} bytes")
        raw.extend(chunk)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return parsed


def _tenant_config_keys(config) -> set[str]:
    """Keys a console admin may write into tenants.config.

    Flat ``TeamEvolverConfig`` field names (``apply_tenant_config_overrides``
    contract) plus the scheduler-level quota keys. Anything else is rejected so
    typos never become silently-ignored overrides.
    """
    fields = {
        f.name for f in dataclasses.fields(config)
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
    secret_presence: dict[str, bool] = {}

    def redact(value, path=""):
        if isinstance(value, list):
            return [redact(item, f"{path}.{i}") for i, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        public = {}
        for key, item in value.items():
            field = f"{path}.{key}" if path else key
            if any(part in key.lower() for part in (
                "key", "token", "password", "secret", "credential", "headers", "dsn",
            )):
                secret_presence[field] = bool(item)
            else:
                public[key] = redact(item, field)
        return public

    public = redact(dict(overrides or {}))
    secret_presence.setdefault("llm_api_key", False)
    return public, secret_presence


def _register_tenant_routes(router: APIRouter, config, registry_provider) -> None:
    @router.get("/api/tenants")
    async def list_tenants(request: Request):  # noqa: ANN202
        user = getattr(request.state, "console_user", None)
        if user is None:
            raise HTTPException(401, "login required")
        registry = registry_provider()
        store = IdentityStore(config)
        if is_platform_admin(user):
            tenants = [
                {"tenant_id": t.tenant_id, "display_name": t.display_name, "status": t.status}
                for t in await asyncio.to_thread(registry.list_tenants)
            ]
        else:
            tenants = await asyncio.to_thread(available_tenants_for, registry, store, user)
        return {
            "mode": registry.mode,
            # Single-tenant deployments configure one credential instead of
            # issuing per-tenant ones; only its presence is ever exposed.
            "machine_credential": {
                "configured": registry.default_machine_credential_configured,
                "env_var": "TEAMEVOLVER_TENANT_TOKEN",
                "prefix": "tevt_",
            },
            "tenants": tenants,
        }

    @router.post("/api/tenants")
    async def create_tenant(request: Request):  # noqa: ANN202
        require_platform_admin(getattr(request.state, "console_user", None))
        registry = registry_provider()
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
        # the tenant_id is created as the default binding (fail-open — errors do
        # not roll back the PG tenant; the console warns the operator instead).
        from ...admin.openviking_accounts import ensure_openviking_account

        viking = await asyncio.to_thread(
            ensure_openviking_account, config, ctx.tenant_id, "team"
        )
        default_account = str(viking.get("account_id") or ctx.tenant_id).strip() or ctx.tenant_id
        try:
            await asyncio.to_thread(
                registry.update_tenant_config,
                ctx.tenant_id,
                {"sharing_viking_account": default_account},
            )
        except Exception as exc:  # noqa: BLE001 - binding is best-effort
            logger.warning("[Tenants] failed to persist default OV account for %s: %s", ctx.tenant_id, exc)
        # Best-effort directory bootstrap for the fresh (empty) account namespace.
        from ...admin.openviking_dirs import ensure_openviking_dirs

        try:
            dirs = await asyncio.to_thread(
                ensure_openviking_dirs,
                config,
                account_id=default_account,
                extra_users=[str(viking.get("admin_user") or "team")],
            )
        except Exception as exc:  # noqa: BLE001 - bootstrap is best-effort
            logger.warning("[Tenants] OpenViking dir bootstrap failed for %s: %s", ctx.tenant_id, exc)
            dirs = {"action": "failed", "error": str(exc), "created": [], "existing": [], "errors": []}
        # The plaintext token is returned exactly once; only its SHA-256 hash is
        # persisted in the tenant registry.
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

    @router.post("/api/tenants/{tenant_id}/rotate-token")
    async def rotate_tenant_token(tenant_id: str, request: Request):  # noqa: ANN202
        authorize_target(request, tenant_id)
        registry = registry_provider()
        try:
            token = await asyncio.to_thread(registry.rotate_token, tenant_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if token is None:
            raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
        return {"agent_token": token}

    @router.post("/api/tenants/{tenant_id}/status")
    async def set_tenant_status(tenant_id: str, request: Request):  # noqa: ANN202
        require_platform_admin(getattr(request.state, "console_user", None))
        registry = registry_provider()
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

    @router.get("/api/tenants/{tenant_id}/config")
    async def get_tenant_config(tenant_id: str, request: Request):  # noqa: ANN202
        authorize_target(request, tenant_id)
        registry = registry_provider()
        try:
            ctx = await asyncio.to_thread(registry.get, tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if ctx is None:
            raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
        public_overrides, secret_presence = _public_tenant_overrides(ctx.config_overrides)
        return {
            "tenant_id": ctx.tenant_id,
            "display_name": ctx.display_name,
            "config_overrides": public_overrides,
            "secret_presence": secret_presence,
            "editable_keys": sorted(_tenant_config_keys(config) if is_platform_admin(
                getattr(request.state, "console_user", None)
            ) else TENANT_CONFIG_KEYS),
        }

    @router.put("/api/tenants/{tenant_id}/config")
    async def put_tenant_config(tenant_id: str, request: Request):  # noqa: ANN202
        authorize_target(request, tenant_id)
        registry = registry_provider()
        body = await _read_limited_json_body(request)
        overrides = (body or {}).get("overrides")
        if not isinstance(overrides, dict) or not overrides:
            raise HTTPException(status_code=400, detail="overrides must be a non-empty object")
        if not is_platform_admin(getattr(request.state, "console_user", None)):
            if set(overrides) - TENANT_CONFIG_KEYS:
                raise HTTPException(403, "PLATFORM_CONFIG_REQUIRED")
        # JSON null deletes a key (_update_config semantics); validate names first.
        unknown = sorted(str(k) for k in overrides if str(k) not in _tenant_config_keys(config))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown config keys (ignored by tenants scope): {', '.join(unknown)}",
            )
        # Type pre-check: the merged overrides must apply onto TeamEvolverConfig
        # now (e.g. a numeric field written as a string fails here, not later).
        try:
            registry_ctx = await asyncio.to_thread(registry.get, tenant_id)
            if registry_ctx is None:
                raise HTTPException(status_code=404, detail=f"unknown tenant: {tenant_id}")
            merged = dict(registry_ctx.config_overrides or {})
            for key, value in overrides.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            apply_tenant_config_overrides(config, merged)
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
            logger.info("[Tenants] config updated for tenant %s (%d keys)", tenant_id, len(overrides))
        public_overrides, secret_presence = _public_tenant_overrides(ctx.config_overrides)
        return {
            "tenant_id": ctx.tenant_id,
            "config_overrides": public_overrides,
            "secret_presence": secret_presence,
        }
