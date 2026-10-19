"""Console login, identity, users, OpenViking accounts and publish requests.

The application root injects configuration, console sessions and the tenant
registry. Identity state is read through IdentityStore across API replicas;
request authorization is installed by the shared HTTP runtime."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ...tenants.registry import current_tenant_id
from ..authn import clear_console_cookies, set_console_cookie
from ..authorization import authorize_target, is_platform_admin, require_platform_admin
from ..console_session import ConsoleSessionManager
from ..identity_store import IdentityStore
from ..openviking_accounts import (
    _copy_skills,
    _hub_from_user,
    _load_publish_requests,
    _publish_skill_to_hub,
    _read_hub_skill,
    _save_publish_request,
    import_openviking_account_users,
    list_openviking_account_users,
    list_openviking_accounts,
)
from ..users import (
    _DIRECTIONS,
    _SPACES,
    _find_user,
    _is_admin_request,
    _now,
    _public_user,
    _request_user,
    _require_admin_request,
    _require_self_or_admin,
    _slug,
    _upsert_user,
    _verify_password,
)

try:  # editor is only needed for the save-skill endpoint; import eagerly for parity
    from team_skills.library import editor
except Exception:  # pragma: no cover - defensive; team_skills always present
    editor = None  # type: ignore[assignment]


def create_router(services=None, *, lifecycle=None, **kwargs) -> APIRouter:
    """Return the admin ``APIRouter`` for the composition root to mount.

    ``services.extras`` must carry ``config`` (effective config provider) and
    ``console_sessions`` (:class:`ConsoleSessionManager`). Without them the
    factory returns a valid empty router (offline route inspection).
    """
    router = APIRouter(tags=["admin"])
    extras = getattr(services, "extras", None) or {}
    config = extras.get("config")
    sessions: ConsoleSessionManager | None = extras.get("console_sessions")
    if config is None or sessions is None:
        return router

    def _effective_config():
        """Current tenant-effective config (same contract as proxy workspace)."""
        from ...tenants.registry import effective_config, get_current_tenant

        return effective_config(None, get_current_tenant(), config)

    _register_auth_routes(router, config, sessions, _tenant_registry_provider(extras, config))
    _register_users_routes(router, config, sessions, _effective_config)
    _register_openviking_account_routes(router, config, _effective_config)
    _register_publish_request_routes(router, config)
    _register_identity_member_routes(router, config, _tenant_registry_provider(extras, config))
    if extras.get("config_repo"):
        from .settings import create_router as settings_router
        router.include_router(settings_router(services))
    return router


def _tenant_registry_provider(extras, config):
    """Zero-arg provider for the process-wide ``TenantRegistry``.

    Mirrors ``app._tenant_registry_provider``: prefers an injected registry
    (tests / shared control pool) and otherwise builds one lazily from config,
    so ``/api/auth/status`` reports the same ``available_tenants`` the authorizer
    resolved membership against — without importing ``proxy``.
    """
    injected = extras.get("tenant_registry")
    if injected is not None:
        return lambda: injected

    cache: dict[str, Any] = {}

    def provider():
        registry = cache.get("registry")
        if registry is None:
            from ...tenants.registry import TenantRegistry

            registry = TenantRegistry(config)
            cache["registry"] = registry
        return registry

    return provider


# --------------------------------------------------------------------------- #
# Authentication + console session (/api/auth/*)
# --------------------------------------------------------------------------- #


def _register_auth_routes(router: APIRouter, config, sessions: ConsoleSessionManager, registry_provider) -> None:
    def _pg_enabled() -> bool:
        return bool(getattr(config, "storage_pg_enabled", False))

    @router.get("/api/auth/status")
    async def auth_status(request: Request):  # noqa: ANN202
        import os

        from ..authn import console_available_tenants

        user = getattr(request.state, "console_user", None)
        tenants = await asyncio.to_thread(
            console_available_tenants, request, config, registry_provider(),
        )
        return {
            "customer_mode": os.environ.get("TEAMEVOLVER_CUSTOMER_MODE") == "1",
            "authenticated": bool(user),
            "needs_setup": await asyncio.to_thread(sessions.users_empty),
            "self_registration_enabled": config.self_registration_enabled,
            "user": user,
            "available_tenants": tenants,
            "active_tenant_id": (user or {}).get("tenant_id"),
        }

    @router.post("/api/auth/bootstrap")
    async def auth_bootstrap(request: Request):  # noqa: ANN202
        if _pg_enabled() and not await asyncio.to_thread(sessions.users_empty):
            require_platform_admin(getattr(request.state, "console_user", None))
        if not await asyncio.to_thread(sessions.users_empty):
            raise HTTPException(status_code=409, detail="users already exist")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="bootstrap body must be an object")
        password = str(body.get("password") or "admin")
        if _pg_enabled() and len(password) < 12:
            raise HTTPException(status_code=400, detail="password must contain at least 12 characters")
        payload = {
            "id": body.get("username") or body.get("id") or "admin",
            "display_name": body.get("display_name") or body.get("username") or "admin",
            "email": body.get("email") or "",
            "role": "admin",
            "password": password,
        }

        def bootstrap(data):
            if data["users"]:
                raise HTTPException(409, "users already exist")
            result = _upsert_user(data, payload, config=config)
            result["platform_role"] = "platform_admin"
            return result

        user = await asyncio.to_thread(
            IdentityStore(config).edit_users, bootstrap, actor="bootstrap", action="user.bootstrap",
        )
        token = await asyncio.to_thread(sessions.issue, str(user.get("id") or ""))
        resp = JSONResponse(content={
            "authenticated": True, "needs_setup": False, "user": _public_user(user, config),
        })
        set_console_cookie(resp, request, token)
        return resp

    @router.post("/api/auth/login")
    async def auth_login(request: Request):  # noqa: ANN202
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="login body must be an object")
        username = str(body.get("username") or body.get("id") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password are required")
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        try:
            _idx, user = _find_user(data, username)
        except HTTPException as exc:
            raise HTTPException(status_code=401, detail="invalid username or password") from exc
        if not user.get("password_hash") or not _verify_password(password, str(user.get("password_hash") or "")):
            raise HTTPException(status_code=401, detail="invalid username or password")
        if user.get("status", "active") != "active":
            raise HTTPException(status_code=403, detail="USER_DISABLED")
        token = await asyncio.to_thread(sessions.issue, str(user.get("id") or ""))
        resp = JSONResponse(content={
            "authenticated": True, "needs_setup": False, "user": _public_user(user, config),
        })
        set_console_cookie(resp, request, token)
        return resp

    @router.post("/api/auth/register")
    async def auth_register(request: Request):  # noqa: ANN202
        # Only the authorizer can grant this bypass. Neither an admin cookie nor
        # a supplied (but unverified) key is sufficient.
        if not config.self_registration_enabled and not getattr(
            request.state, "service_root_authenticated", False
        ):
            raise HTTPException(status_code=403, detail="SELF_REGISTRATION_DISABLED")
        if await asyncio.to_thread(sessions.users_empty):
            raise HTTPException(status_code=409, detail="setup required; initialize the admin account first")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="register body must be an object")
        username = str(body.get("username") or body.get("id") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password are required")
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        if any(str(user.get("id") or "") == username for user in data.get("users") or []):
            raise HTTPException(status_code=409, detail="user already exists")
        payload = {
            "id": username,
            "display_name": body.get("display_name") or username,
            "email": body.get("email") or "",
            "role": "user",
            "password": password,
        }

        def register(data):
            if any(u["id"] == _slug(username) for u in data["users"]):
                raise HTTPException(409, "user already exists")
            return _upsert_user(data, payload, config=config)

        user = await asyncio.to_thread(
            IdentityStore(config).edit_users, register, actor=username, action="user.register",
        )
        token = await asyncio.to_thread(sessions.issue, str(user.get("id") or ""))
        resp = JSONResponse(content={
            "authenticated": True, "needs_setup": False, "user": _public_user(user, config),
        })
        set_console_cookie(resp, request, token)
        return resp

    @router.post("/api/auth/logout")
    async def auth_logout(request: Request):  # noqa: ANN202
        from ..authn import console_session_token

        token = console_session_token(request)
        if token:
            await asyncio.to_thread(sessions.revoke, token)
        resp = JSONResponse(content={"authenticated": False})
        clear_console_cookies(resp, request)
        return resp


# --------------------------------------------------------------------------- #
# Users (/api/users*)
# --------------------------------------------------------------------------- #


async def _ensure_user_directories(cfg, user=None):
    from ..openviking_dirs import ensure_openviking_dirs

    personal = str(((user or {}).get("personal_space") or {}).get("viking_user") or "")
    await asyncio.to_thread(
        ensure_openviking_dirs,
        cfg,
        account_id=str(getattr(cfg, "sharing_viking_account", "") or "default"),
        extra_users=[personal] if personal else (),
    )


def _register_users_routes(router: APIRouter, config, sessions, effective_config) -> None:

    @router.get("/api/users")
    async def api_list_users(request: Request):  # noqa: ANN202
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        users = data.get("users") or []
        cfg = effective_config()
        if is_platform_admin(_request_user(request)):
            pass
        elif _is_admin_request(request):
            members = await asyncio.to_thread(
                IdentityStore(config).memberships, tenant_id=current_tenant_id(),
            )
            ids = {m["user_id"] for m in members if m["status"] == "active"}
            users = [u for u in users if u["id"] in ids]
        else:
            current_id = str(_request_user(request).get("id") or "")
            users = [user for user in users if str(user.get("id") or "") == current_id]
        return JSONResponse(content={
            "users": [_public_user(u, cfg) for u in users],
            "storage_deployment": cfg.sharing_viking_deployment,
            "openviking_account": cfg.sharing_viking_account or "default",
            "default_team_user": cfg.sharing_viking_user or "team",
        })

    @router.get("/api/users/{user_id}")
    async def api_get_user(user_id: str, request: Request):  # noqa: ANN202
        if _request_user(request).get("id") != user_id:
            require_platform_admin(_request_user(request))
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        return JSONResponse(content=_public_user(user, config))

    @router.post("/api/users")
    async def api_upsert_user(body: dict[str, Any], request: Request):  # noqa: ANN202
        require_platform_admin(_request_user(request))
        if body.get("role", "user") != "user" or "platform_role" in body:
            raise HTTPException(400, "Use the platform access or tenant membership endpoint to assign roles")
        cfg = effective_config()
        user = await asyncio.to_thread(
            IdentityStore(config).edit_users,
            lambda data: _upsert_user(data, body, config=cfg),
            actor=_request_user(request)["id"],
        )
        await _ensure_user_directories(cfg, user)
        return JSONResponse(content=_public_user(user, cfg))

    @router.put("/api/users/{user_id}/profile")
    async def api_update_own_profile(user_id: str, body: dict[str, Any], request: Request):  # noqa: ANN202
        actor = _request_user(request)
        if actor.get("id") != user_id:
            require_platform_admin(actor)
        cfg = effective_config()

        def edit(data):
            _idx, existing = _find_user(data, user_id)
            allowed = {"display_name", "email", "password"}
            if is_platform_admin(actor):
                allowed |= {"personal_space", "agent_identities"}
            payload = {k: v for k, v in body.items() if k in allowed}
            payload["id"] = user_id
            return _upsert_user(data, payload, config=cfg)

        user = await asyncio.to_thread(
            IdentityStore(config).edit_users, edit, actor=actor["id"], action="user.profile",
        )
        await _ensure_user_directories(cfg, user)
        return JSONResponse(content=_public_user(user, cfg))

    @router.delete("/api/users/{user_id}")
    async def api_delete_user(user_id: str, request: Request):  # noqa: ANN202
        require_platform_admin(_request_user(request))

        def remove(data):
            idx, user = _find_user(data, user_id)
            data["users"].pop(idx)
            return user

        user = await asyncio.to_thread(
            IdentityStore(config).edit_users, remove, actor=_request_user(request)["id"], action="user.delete",
        )
        return JSONResponse(content={"deleted": True, "id": user.get("id")})

    @router.get("/api/users/{user_id}/skills")
    async def api_list_user_space_skills(user_id: str, request: Request,
                                         space: str = Query(default="personal")):  # noqa: ANN202
        _require_self_or_admin(request, user_id)
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        hub = _hub_from_user(config, user, space=space)
        return JSONResponse(content={"space": space, "skills": await asyncio.to_thread(hub.list_remote)})

    @router.post("/api/users/{user_id}/share")
    async def api_share_skills(user_id: str, request: Request,
                               body: dict[str, Any] | None = None):  # noqa: ANN202
        _require_self_or_admin(request, user_id)
        payload = body if isinstance(body, dict) else {}
        direction = str(payload.get("direction") or "personal_to_team")
        if direction not in _DIRECTIONS:
            raise HTTPException(status_code=400, detail=f"unsupported share direction: {direction}")
        requested = {
            str(name or "").strip()
            for name in (payload.get("skill_names") or payload.get("skills") or [])
            if str(name or "").strip()
        }
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        if direction == "personal_to_team" and not _is_admin_request(request):
            raise HTTPException(status_code=403, detail="only admin users can publish personal skills to team space")
        source_space = "personal" if direction == "personal_to_team" else "team"
        target_space = "team" if direction == "personal_to_team" else "personal"
        result = await asyncio.to_thread(
            _copy_skills,
            source_hub=_hub_from_user(config, user, space=source_space),
            target_hub=_hub_from_user(config, user, space=target_space),
            requested=requested,
        )
        result["direction"] = direction
        return JSONResponse(content=result)

    @router.get("/api/users/{user_id}/skills/{name}")
    async def api_get_user_space_skill(user_id: str, name: str, request: Request,
                                       space: str = Query(default="personal")):  # noqa: ANN202
        _require_self_or_admin(request, user_id)
        if space not in _SPACES:
            raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        hub = _hub_from_user(config, user, space=space)
        detail = await asyncio.to_thread(_read_hub_skill, hub, str(name or "").strip())
        if detail is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        return JSONResponse(content=detail)

    @router.post("/api/users/{user_id}/skills")
    async def api_save_user_space_skill(user_id: str, body: dict[str, Any], request: Request):  # noqa: ANN202
        _require_self_or_admin(request, user_id)
        space = str(body.get("space") or "personal")
        if space not in _SPACES:
            raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
        if space == "team" and not _is_admin_request(request):
            raise HTTPException(status_code=403, detail="only admin users can edit team skills")
        name = _slug(str(body.get("name") or ""))
        raw_md = str(body.get("skill_md") or "").strip()
        if raw_md:
            skill_md = raw_md
        else:
            skill_md = editor.build_skill_md(
                name=name,
                description=str(body.get("description") or ""),
                category=str(body.get("category") or "general"),
                body=str(body.get("body") or ""),
            )
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        hub = _hub_from_user(config, user, space=space)
        commit = await asyncio.to_thread(
            _publish_skill_to_hub, hub, name=name, skill_md=skill_md, mutation_prefix=f"user-{user_id}-edit",
        )
        return JSONResponse(content={"name": name, "space": space, "commit": commit})

    @router.delete("/api/users/{user_id}/skills/{name}")
    async def api_delete_user_space_skill(user_id: str, name: str, request: Request,
                                          space: str = Query(default="personal")):  # noqa: ANN202
        _require_self_or_admin(request, user_id)
        if space not in _SPACES:
            raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
        if space == "team" and not _is_admin_request(request):
            raise HTTPException(status_code=403, detail="only admin users can delete team skills")
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        hub = _hub_from_user(config, user, space=space)
        result = await asyncio.to_thread(hub.delete_skill, str(name or "").strip())
        return JSONResponse(content={"space": space, **result})


# --------------------------------------------------------------------------- #
# OpenViking accounts (/api/openviking-accounts*)
# --------------------------------------------------------------------------- #


def _register_openviking_account_routes(router: APIRouter, config, effective_config) -> None:
    @router.get("/api/openviking-accounts")
    async def api_list_openviking_accounts(request: Request):  # noqa: ANN202
        require_platform_admin(_request_user(request))
        return JSONResponse(content=await asyncio.to_thread(list_openviking_accounts, effective_config()))

    @router.get("/api/openviking-accounts/{account}/users")
    async def api_list_openviking_account_users(account: str, request: Request):  # noqa: ANN202
        require_platform_admin(_request_user(request))
        cfg = effective_config()
        payload = await asyncio.to_thread(list_openviking_account_users, cfg, account)
        registry = await asyncio.to_thread(IdentityStore(config).load_registry)
        identities = [u.get("external_identity") for u in registry.get("users") or []]
        for row in payload.get("users") or []:
            row["imported"] = {"provider": "openviking", "account": account, "user_id": row["user_id"]} in identities
        return JSONResponse(content=payload)

    @router.post("/api/openviking-accounts/{account}/import-users")
    async def api_import_openviking_account_users(account: str, body: dict[str, Any],
                                                  request: Request):  # noqa: ANN202
        _require_admin_request(request)
        raw_ids = body.get("user_ids") or body.get("users") or []
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="user_ids must be a list")
        cfg = effective_config()
        if account != cfg.sharing_viking_account:
            raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
        report = await asyncio.to_thread(import_openviking_account_users, cfg, account, raw_ids)
        await _ensure_user_directories(cfg)
        return JSONResponse(content=report)


# --------------------------------------------------------------------------- #
# Skill publish requests (/api/skill-publish-requests*)
# --------------------------------------------------------------------------- #


def _register_publish_request_routes(router: APIRouter, config) -> None:
    @router.get("/api/skill-publish-requests")
    async def api_list_publish_requests(request: Request):  # noqa: ANN202
        requests = await asyncio.to_thread(_load_publish_requests, config)
        if not _is_admin_request(request):
            current_id = str(_request_user(request).get("id") or "")
            requests = [item for item in requests if str(item.get("requester_id") or "") == current_id]
        pending = sum(1 for item in requests if str(item.get("status")) == "pending")
        return JSONResponse(content={"requests": requests, "pending_count": pending})

    @router.post("/api/users/{user_id}/publish-requests")
    async def api_submit_publish_request(user_id: str, body: dict[str, Any], request: Request):  # noqa: ANN202
        _require_self_or_admin(request, user_id)
        requested = sorted({
            str(name or "").strip()
            for name in (body.get("skill_names") or body.get("skills") or [])
            if str(name or "").strip()
        })
        if not requested:
            raise HTTPException(status_code=400, detail="skill_names must not be empty")
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, user = _find_user(data, user_id)
        request_id = f"pubreq-{uuid.uuid4().hex[:16]}"
        record = {
            "schema_version": "teamevolver.skill-publish-request.v1",
            "request_id": request_id,
            "requester_id": user_id,
            "requester_name": str(user.get("display_name") or user_id),
            "skill_names": requested,
            "note": str(body.get("note") or "")[:2000],
            "status": "pending",
            "created_at": _now(),
            "updated_at": _now(),
            "decided_by": "",
            "decided_at": "",
            "decision_note": "",
            "result": {},
        }
        await asyncio.to_thread(_save_publish_request, config, record)
        return JSONResponse(content=record)

    @router.post("/api/skill-publish-requests/{request_id}/approve")
    async def api_approve_publish_request(request_id: str, request: Request,
                                          body: dict[str, Any] | None = None):  # noqa: ANN202
        _require_admin_request(request)
        requests = {
            str(item.get("request_id") or ""): item
            for item in await asyncio.to_thread(_load_publish_requests, config)
        }
        record = requests.get(_slug(request_id))
        if record is None:
            raise HTTPException(status_code=404, detail=f"request not found: {request_id}")
        if str(record.get("status")) != "pending":
            raise HTTPException(status_code=409, detail=f"request already {record.get('status')}")
        data = await asyncio.to_thread(IdentityStore(config).load_registry)
        _idx, requester = _find_user(data, str(record.get("requester_id") or ""))
        result = await asyncio.to_thread(
            _copy_skills,
            source_hub=_hub_from_user(config, requester, space="personal"),
            target_hub=_hub_from_user(config, requester, space="team"),
            requested=set(record.get("skill_names") or []),
        )
        record.update({
            "status": "approved",
            "decided_by": str(_request_user(request).get("id") or "admin"),
            "decided_at": _now(),
            "updated_at": _now(),
            "decision_note": str((body or {}).get("note") or "")[:2000],
            "result": result,
        })
        await asyncio.to_thread(_save_publish_request, config, record)
        return JSONResponse(content=record)

    @router.post("/api/skill-publish-requests/{request_id}/reject")
    async def api_reject_publish_request(request_id: str, request: Request,
                                         body: dict[str, Any] | None = None):  # noqa: ANN202
        _require_admin_request(request)
        requests = {
            str(item.get("request_id") or ""): item
            for item in await asyncio.to_thread(_load_publish_requests, config)
        }
        record = requests.get(_slug(request_id))
        if record is None:
            raise HTTPException(status_code=404, detail=f"request not found: {request_id}")
        if str(record.get("status")) != "pending":
            raise HTTPException(status_code=409, detail=f"request already {record.get('status')}")
        record.update({
            "status": "rejected",
            "decided_by": str(_request_user(request).get("id") or "admin"),
            "decided_at": _now(),
            "updated_at": _now(),
            "decision_note": str((body or {}).get("note") or "")[:2000],
        })
        await asyncio.to_thread(_save_publish_request, config, record)
        return JSONResponse(content=record)


# --------------------------------------------------------------------------- #
# Identity + tenant membership governance (/api/identity/*, members)
# --------------------------------------------------------------------------- #


def _register_identity_member_routes(router: APIRouter, config, registry_provider) -> None:
    """Migrated from ``proxy/identity_routes.py``.

    Platform identity administration and per-tenant membership governance,
    consuming the injected :class:`IdentityStore` + ``TenantRegistry`` so there
    is no back-dependency on ``ProxyServer``.
    """

    def _user(request: Request) -> dict[str, Any]:
        current = getattr(request.state, "console_user", None)
        if not current:
            raise HTTPException(401, "login required")
        return current

    @router.get("/api/identity/tenants")
    async def my_tenants(request: Request):  # noqa: ANN202
        from ...admin.authorization import available_tenants_for

        return {"tenants": await asyncio.to_thread(
            available_tenants_for, registry_provider(), IdentityStore(config), _user(request),
        )}

    @router.get("/api/identity/audit")
    async def identity_audit(request: Request, limit: int = 100):  # noqa: ANN202
        require_platform_admin(_user(request))
        return {"events": await asyncio.to_thread(IdentityStore(config).audit_events, limit)}

    @router.get("/api/tenants/{tenant_id}/members")
    async def members(tenant_id: str, request: Request):  # noqa: ANN202
        authorize_target(request, tenant_id)
        store = IdentityStore(config)
        memberships = await asyncio.to_thread(store.memberships, tenant_id=tenant_id)
        accounts = (await asyncio.to_thread(store.load_registry))["users"]
        profiles = {u["id"]: u for u in accounts}
        return {"members": [
            {**m, "display_name": profiles.get(m["user_id"], {}).get("display_name", m["user_id"])}
            for m in memberships
        ]}

    @router.put("/api/tenants/{tenant_id}/members/{user_id}")
    async def set_member(tenant_id: str, user_id: str, body: dict, request: Request):  # noqa: ANN202
        authorize_target(request, tenant_id)
        if set(body) - {"role", "status"}:
            raise HTTPException(400, "Only membership role and status may be changed")
        return await asyncio.to_thread(
            IdentityStore(config).set_member, tenant_id, user_id,
            body.get("role", "member"), body.get("status", "active"), _user(request)["id"],
        )

    @router.put("/api/identity/users/{user_id}/access")
    async def update_access(user_id: str, request: Request, body: dict):  # noqa: ANN202
        actor = _user(request)
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

        return await asyncio.to_thread(
            IdentityStore(config).edit_users, edit, actor=actor["id"], action="user.access",
        )
