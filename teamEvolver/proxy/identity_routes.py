"""Platform identity administration and tenant membership governance."""
from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request

from ..admin.authorization import (
    authorize_target, available_tenants, require_platform_admin,
)
from ..admin.identity_store import IdentityStore


def register_identity_routes(owner, app):
    from .users_admin import _public_user

    def user(request):
        current = getattr(request.state, "console_user", None)
        if not current:
            raise HTTPException(401, "login required")
        return current

    @app.get("/api/identity/tenants")
    async def my_tenants(request: Request):
        return {"tenants": await asyncio.to_thread(available_tenants, owner, user(request))}

    @app.get("/api/identity/audit")
    async def identity_audit(request: Request, limit: int = 100):
        require_platform_admin(user(request))
        return {"events": await asyncio.to_thread(IdentityStore(owner.config).audit_events, limit)}

    @app.get("/api/tenants/{tenant_id}/members")
    async def members(tenant_id: str, request: Request):
        authorize_target(request, tenant_id)
        store = IdentityStore(owner.config)
        memberships = await asyncio.to_thread(store.memberships, tenant_id=tenant_id)
        accounts = (await asyncio.to_thread(store.load_registry))["users"]
        profiles = {u["id"]: u for u in accounts}
        return {"members": [
            {**m, "display_name": profiles.get(m["user_id"], {}).get("display_name", m["user_id"])}
            for m in memberships
        ]}

    @app.put("/api/tenants/{tenant_id}/members/{user_id}")
    async def set_member(tenant_id: str, user_id: str, body: dict, request: Request):
        authorize_target(request, tenant_id)
        if set(body) - {"role", "status"}:
            raise HTTPException(400, "Only membership role and status may be changed")
        return await asyncio.to_thread(
            IdentityStore(owner.config).set_member, tenant_id, user_id,
            body.get("role", "member"), body.get("status", "active"), user(request)["id"],
        )

    @app.put("/api/identity/users/{user_id}/access")
    async def update_access(user_id: str, request: Request, body: dict):
        actor = user(request)
        require_platform_admin(actor)
        if set(body) - {"platform_role", "status"}:
            raise HTTPException(400, "INVALID_USER_ACCESS")

        def edit(data):
            target = next((u for u in data["users"] if u["id"] == user_id), None)
            if target is None:
                raise HTTPException(404, "USER_NOT_FOUND")
            role = body.get("platform_role", target.get("platform_role", "user"))
            status = body.get("status", target.get("status", "active"))
            if role not in {"platform_admin", "user"} or status not in {"active", "disabled"}:
                raise HTTPException(400, "INVALID_USER_ACCESS")
            target.update(platform_role=role, status=status)
            return _public_user(target)

        result = await asyncio.to_thread(
            IdentityStore(owner.config).edit_users, edit, actor=actor["id"], action="user.access",
        )
        return result
