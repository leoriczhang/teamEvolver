"""Console authorization with separate platform and tenant scopes."""
from __future__ import annotations

from fastapi import HTTPException

from ..tenants.registry import current_tenant_id
from .identity_store import IdentityStore

MEMBER_CAPABILITIES = {"workspace.use", "sessions.use", "personal.manage", "team.read", "team.submit"}
ADMIN_CAPABILITIES = MEMBER_CAPABILITIES | {"tenant.manage", "members.manage", "team.publish"}
PLATFORM_CAPABILITIES = {"platform.manage", "users.manage", "audit.read"}


def is_platform_admin(user):
    """Platform authority comes only from the persisted platform role."""
    return bool(user and user.get("platform_role") == "platform_admin")


def require_platform_admin(user):
    if not is_platform_admin(user):
        raise HTTPException(403, "PLATFORM_ADMIN_REQUIRED")


def is_tenant_admin(user):
    return bool(
        user
        and (
            user.get("service_root")
            or (
                user.get("tenant_id") == current_tenant_id()
                and "tenant.manage" in user.get("capabilities", [])
            )
        )
    )


def require_tenant_admin(user):
    if not is_tenant_admin(user):
        raise HTTPException(403, "TENANT_ADMIN_REQUIRED")


def control_path(path):
    return (path in {"/", "/console", "/favicon.ico"}
            or path.startswith(("/assets/", "/docs-assets/"))
            or path.startswith(("/api/auth/", "/api/identity/", "/api/openviking-accounts",
                             "/api/langfuse-tracing-config", "/api/logging/"))
            or path == "/api/team-settings"
            or path == "/api/sharing-config"
            or path == "/api/sharing-config/bootstrap-dirs"
            or path == "/api/tenants" or path.startswith("/api/tenants/")
            or path == "/api/users"
            or (path.startswith("/api/users/") and "/skills" not in path
                and "/share" not in path and "/publish-requests" not in path))


def public_path(path):
    return (path in {"/", "/console", "/health", "/healthz", "/readyz", "/favicon.ico"}
            or path.startswith(("/assets/", "/docs-assets/", "/api/auth/")))


def available_tenants(owner, user):
    from ..proxy.tenant_routes import get_tenant_registry

    registry = get_tenant_registry(owner)
    if is_platform_admin(user):
        tenants = registry.list_tenants()
    else:
        memberships = IdentityStore(owner.config).memberships(user_id=user["id"])
        tenants = [registry.get(m["tenant_id"]) for m in memberships if m["status"] == "active"]
    return [{"tenant_id": t.tenant_id, "display_name": t.display_name, "status": t.status}
            for t in tenants if t is not None and t.status == "active"]


def bind_console_user(owner, request, user, claimed, _session_token):
    """Resolve a selector against membership on every request, even default."""
    from ..proxy.tenant_routes import get_tenant_registry

    registry = get_tenant_registry(owner)
    store = IdentityStore(owner.config)
    platform = is_platform_admin(user)
    memberships = store.memberships(user_id=user["id"])
    members = {m["tenant_id"]: m for m in memberships if m["status"] == "active"}
    # Never enter a tenant implicitly. The browser must present an explicit
    # selector, which is then checked against current membership on every call.
    tid = claimed
    ctx = registry.get(tid) if tid else None
    valid = bool(ctx and ctx.status == "active" and (platform or tid in members))
    # Auth/control-plane endpoints remain usable without any membership, so
    # users can sign out or accept membership and operators can recover access.
    if not valid and not control_path(request.url.path):
        raise HTTPException(403, "TENANT_ACCESS_DENIED" if claimed else "TENANT_MEMBERSHIP_REQUIRED")
    capabilities = set(PLATFORM_CAPABILITIES) if platform else set()
    tenant_role = members.get(tid, {}).get("role") if valid else None
    if valid:
        capabilities |= MEMBER_CAPABILITIES
        # Platform admins operate every tenant by default (no support-mode
        # gate); tenant admins keep their membership-based grant.
        if platform or tenant_role == "tenant_admin":
            capabilities |= ADMIN_CAPABILITIES
    resolved = {
        **user, "tenant_id": tid if valid else None, "tenant_role": tenant_role,
        "role": "admin" if "tenant.manage" in capabilities else "user",
        "capabilities": sorted(capabilities),
    }
    request.state.console_user = resolved
    request.state.identity_store = store
    return ctx if valid else None


def authorize_target(request, tenant_id, *, manage=True):
    """Path tenant is authoritative; header and path must not disagree."""
    user = getattr(request.state, "console_user", None) or {}
    if user.get("tenant_id") != tenant_id:
        raise HTTPException(403, "TENANT_MISMATCH")
    if manage:
        require_tenant_admin(user)
    elif "team.read" not in user.get("capabilities", []):
        raise HTTPException(403, "TENANT_MEMBERSHIP_REQUIRED")


def enforce_platform_routes(request):
    path = request.url.path
    if path.startswith(("/api/langfuse-tracing-config", "/api/logging/", "/api/openviking-accounts")):
        require_platform_admin(getattr(request.state, "console_user", None))
    # Infrastructure account bindings and Root Keys must never be editable by
    # tenant admins: changing the Account would route their data to another tenant.
    if path == "/api/sharing-config" and request.method != "GET":
        require_platform_admin(getattr(request.state, "console_user", None))
