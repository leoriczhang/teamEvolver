"""Console authorization with separate platform and tenant scopes."""
from __future__ import annotations

from starlette.exceptions import HTTPException

from ..tenants.registry import current_tenant_id

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



def available_tenants_for(registry, store, user):
    """List active tenants visible to a platform identity or its active memberships."""
    if is_platform_admin(user):
        tenants = registry.list_tenants()
    else:
        memberships = store.memberships(user_id=user["id"])
        tenants = [registry.get(m["tenant_id"]) for m in memberships if m["status"] == "active"]
    return [{"tenant_id": t.tenant_id, "display_name": t.display_name, "status": t.status}
            for t in tenants if t is not None and t.status == "active"]



def resolve_console_membership(registry, store, user, claimed, *, control_path):
    """Resolve the selected tenant and its current membership capabilities.

    Returns the resolved user and TenantContext. An invalid selector raises 403
    unless the request is a control-plane operation such as signing out or
    accepting membership."""
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
    if not valid and not control_path:
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
    return resolved, (ctx if valid else None)


def authorize_target(request, tenant_id, *, manage=True):
    """Path tenant is authoritative; header and path must not disagree."""
    user = getattr(request.state, "console_user", None) or {}
    if user.get("tenant_id") != tenant_id:
        raise HTTPException(403, "TENANT_MISMATCH")
    if manage:
        require_tenant_admin(user)
    elif "team.read" not in user.get("capabilities", []):
        raise HTTPException(403, "TENANT_MEMBERSHIP_REQUIRED")



