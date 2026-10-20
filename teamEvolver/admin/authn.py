"""Console and Agent authentication for the shared HTTP runtime.

Console sessions carry platform identity; memberships and tenant status are
rechecked on every request. Agent machine tokens select their tenant and require
a declared user ID. Service Root Key access is separate from console roles."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ..tenants.registry import AGENT_TOKEN_PREFIX, current_tenant_id
from .authorization import (
    ADMIN_CAPABILITIES,
    MEMBER_CAPABILITIES,
    PLATFORM_CAPABILITIES,
    available_tenants_for,
    control_path,
    public_path,
    require_platform_admin,
    resolve_console_membership,
)
from .console_session import SESSION_TTL_SECONDS, ConsoleSessionManager
from .identity_store import IdentityStore

SESSION_COOKIE = "teamEvolver_console_session"

# Machine paths a verified ``tevt_`` Agent credential may reach (baseline proxy
# allowlist). Everything else requires a console login — a machine token never
# unlocks the console admin surface.
_AGENT_MACHINE_PATHS = (
    "/ingest_session", "/api/datasource/pull", "/trigger", "/status",
    "/history", "/sessions", "/conversations", "/storage/status",
    "/internal/agents/context",
)


def _bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "").strip()
    return header[7:].strip() if header.lower().startswith("bearer ") else header


def console_cookie_name(request: Request) -> str:
    host = request.url.hostname or "localhost"
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    root_path = str(request.scope.get("root_path") or "/")
    if not root_path.startswith("/"):
        root_path = f"/{root_path}"
    scope = f"{host}:{port}{root_path}"
    suffix = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
    return f"{SESSION_COOKIE}_{suffix}"


def console_session_token(request: Request) -> str:
    scoped = request.cookies.get(console_cookie_name(request), "")
    if scoped:
        return scoped
    legacy = request.cookies.get(SESSION_COOKIE, "")
    if legacy:
        request.state.migrate_console_session = legacy
    return legacy


def set_console_cookie(response: Response, request: Request, token: str) -> None:
    cookie_name = console_cookie_name(request)
    response.set_cookie(
        cookie_name,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    if cookie_name != SESSION_COOKIE and request.cookies.get(SESSION_COOKIE):
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=request.url.scheme == "https",
        )


def clear_console_cookies(response: Response, request: Request) -> None:
    response.delete_cookie(console_cookie_name(request), path="/", secure=request.url.scheme == "https")
    response.delete_cookie(SESSION_COOKIE, path="/", secure=request.url.scheme == "https")


def _bind_platform_service_user(user: dict[str, Any]) -> dict[str, Any]:
    """Capabilities for the Root Key service identity (platform-admin service).

    The service identity has no membership rows, so it keeps the baseline
    platform branch: operate every tenant by default against the current
    (default) tenant with the full platform + admin + member capability set.
    """
    tenant_id = current_tenant_id()
    capabilities = set(PLATFORM_CAPABILITIES) | set(MEMBER_CAPABILITIES) | set(ADMIN_CAPABILITIES)
    return {
        **user,
        "tenant_id": tenant_id,
        "tenant_role": "tenant_admin",
        "role": "admin",
        "capabilities": sorted(capabilities),
    }


def build_console_authorizer(session_manager: ConsoleSessionManager, *, config, registry_provider):
    """Return an ``http_runtime`` authorizer with full tenant resolution.

    The returned coroutine is installed as the shared runtime ``authorizer``
. It resolves identity *and* tenant for the composed app so the
    HTTP runtime no longer needs a separate ``tenant_resolver`` for the console
    path: it binds the console identity, resolves the explicit ``X-Tenant-Id``
    selector against membership (or the ``tevt_`` Agent machine token against
    the registry), enforces the login gate, and sets ``request.state.tenant`` so
    the runtime binds/resets the request's ``contextvars`` tenant.

    ``registry_provider`` is a zero-arg callable returning the process-wide
  :class:`TenantRegistry`; it is called lazily so a registry that needs a live
    PG control pool is only built when first used.
    """

    async def authorizer(request: Request, services) -> None:  # noqa: ANN001, ARG001
        import asyncio

        path = request.url.path
        claimed = str(request.headers.get("x-tenant-id") or "").strip()

        # 1. Service Root Key bearer bypass → a platform-admin service identity.
        root_key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "")
        bearer = _bearer_token(request)
        if root_key and bearer and hmac.compare_digest(bearer, root_key):
            registry = registry_provider()
            ctx = await asyncio.to_thread(registry.get, claimed or "default")
            if ctx is None or ctx.status != "active":
                raise HTTPException(403, "TENANT_ACCESS_DENIED")
            request.state.tenant = ctx
            request.state.tenant_source = "service_root"
            request.state.service_root_authenticated = True
            request.state.console_user = _bind_platform_service_user({
                "id": "service-root", "role": "user",
                "platform_role": "platform_admin", "service_root": True,
            })
            return

        registry = registry_provider()

        # 2. Agent machine token (tevt_): resolve to its tenant and gate it to
        #    the machine-path allowlist. A machine token never unlocks the
        #    console admin surface (baseline proxy tenant middleware).
        if bearer.startswith(AGENT_TOKEN_PREFIX):
            ctx = await asyncio.to_thread(registry.resolve_by_agent_token, bearer)
            if ctx is None or ctx.status != "active":
                raise HTTPException(status_code=401, detail="invalid tenant token")
            if path != "/sync/skills" and not any(
                path == prefix or path.startswith(prefix + "/") for prefix in _AGENT_MACHINE_PATHS
            ):
                raise HTTPException(status_code=403, detail="console admin required")
            if claimed and claimed != ctx.tenant_id:
                raise HTTPException(status_code=403, detail="tenant mismatch")
            request.state.tenant = ctx
            request.state.tenant_source = "token"
            return

        # 3. Resolve the browser console session (shared-store authoritative).
        token = console_session_token(request)
        user = await asyncio.to_thread(session_manager.resolve, token) if token else None
        if user is not None:
            renewed = bool(user.pop("_renewed", False))
            store = IdentityStore(config)
            resolved, ctx = await asyncio.to_thread(
                resolve_console_membership,
                registry, store, user, claimed, control_path=control_path(path),
            )
            request.state.console_user = resolved
            request.state.identity_store = store
            request.state.tenant = ctx
            request.state.tenant_source = "console"
            if renewed:
                request.state.renew_console_session = token
            _enforce_platform_routes(request)
            return

        # 4. Login gate for everything that is not a public control path.
        if not public_path(path):
            raise HTTPException(status_code=401, detail="login required")

    return authorizer


def console_available_tenants(request: Request, config, registry) -> list[dict[str, Any]]:
    """Tenants the resolved console user may enter (for ``/api/auth/status``)."""
    user = getattr(request.state, "console_user", None)
    if not user:
        return []
    return available_tenants_for(registry, IdentityStore(config), user)


def _enforce_platform_routes(request: Request) -> None:
    """Platform-only guard for infrastructure-config routes (baseline parity)."""
    path = request.url.path
    if path.startswith(("/api/langfuse-tracing-config", "/api/logging/", "/api/openviking-accounts")):
        require_platform_admin(getattr(request.state, "console_user", None))
    if path == "/api/sharing-config" and request.method != "GET":
        require_platform_admin(getattr(request.state, "console_user", None))
