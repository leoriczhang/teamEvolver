"""User management REST API for role-based skill-space operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..config import VOLCENGINE_OPENVIKING_ENDPOINT
from ..config_store import ConfigStore
from ..skills.bundle import write_skill_bundle
from ..skills.hub import SkillHub

_DEFAULT_USERS_PATH = Path.home() / ".teamEvolver" / "users.json"
_DEFAULT_OPENVIKING_ENDPOINT = VOLCENGINE_OPENVIKING_ENDPOINT
_DEFAULT_ACCOUNT = "default"
_DEFAULT_USER = "default"
_DEFAULT_AGENT = "teamEvolver"
_DEFAULT_ROOT_PREFIX = "teamEvolver"
_ROLES = {"user", "admin"}
_SPACES = {"personal", "team"}
_DIRECTIONS = {"personal_to_team", "team_to_personal"}
_PASSWORD_ITERATIONS = 260_000


def _request_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "console_user", None)
    return user if isinstance(user, dict) else {}


def _is_admin_request(request: Request) -> bool:
    return str(_request_user(request).get("role") or "user") == "admin"


def _require_admin_request(request: Request) -> None:
    if not _is_admin_request(request):
        raise HTTPException(status_code=403, detail="only admin users can perform this operation")


def _require_self_or_admin(request: Request, user_id: str) -> None:
    current = _request_user(request)
    if str(current.get("role") or "user") == "admin":
        return
    if str(current.get("id") or "") != str(user_id or ""):
        raise HTTPException(status_code=403, detail="users can only access their own resources")


def _space_key(space: dict[str, Any]) -> str:
    return str((space or {}).get("viking_api_key") or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value or "").strip())
    out = out.strip(".-_")
    if not out:
        raise HTTPException(status_code=400, detail="user id must not be empty")
    return out


def _registry_path(config) -> Path:
    path = str(getattr(config, "users_registry_path", "") or "").strip()
    return Path(path).expanduser() if path else _DEFAULT_USERS_PATH


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"users": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 - corrupt local admin file
        raise HTTPException(status_code=500, detail=f"failed to read users registry: {exc}") from exc
    if not isinstance(data, dict):
        return {"users": []}
    if not isinstance(data.get("users"), list):
        data["users"] = []
    return data


def _save_registry(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _normalize_role(value: Any, existing: str = "user") -> str:
    role = str(value or existing or "user").strip().lower()
    if role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"unsupported role: {role}")
    return role


def _normalize_agent_identities(
    raw: Any,
    existing: dict[str, Any] | None = None,
) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else (existing or {})
    identities: dict[str, str] = {}
    for runtime_type, username in source.items():
        raw_runtime = str(runtime_type or "").strip()
        if not raw_runtime:
            continue
        runtime = _slug(raw_runtime).lower()
        external_username = str(username or "").strip()
        if runtime and external_username:
            identities[runtime] = external_username[:200]
    return identities


def _normalize_agent_subjects(
    raw: Any,
    existing: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    source = raw if isinstance(raw, list) else (existing or [])
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        integration_id = str(item.get("integration_id") or "").strip()[:160]
        external_subject = str(item.get("external_subject") or "").strip()[:300]
        runtime_type = str(item.get("runtime_type") or "").strip().lower()[:80]
        if not integration_id or not external_subject:
            continue
        key = (integration_id, external_subject)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "integration_id": integration_id,
                "runtime_type": runtime_type,
                "external_subject": external_subject,
            }
        )
    return normalized


def _normalize_space(raw: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize a skill space.

    Only the OpenViking key is configurable. All endpoint/account/user/agent
    routing fields are derived internally, and the backend is always
    OpenViking (``viking``) — cloud or local self-hosted.
    """
    incoming = raw if isinstance(raw, dict) else {}
    current = existing if isinstance(existing, dict) else {}
    if incoming.get("clear_viking_api_key"):
        return {
            "backend": "viking",
            "viking_api_key": "",
            "viking_user": str(
                incoming.get("viking_user", current.get("viking_user", "")) or ""
            ).strip(),
        }
    key_value = incoming.get("viking_api_key", None)
    if key_value not in (None, ""):
        api_key = str(key_value)
    else:
        api_key = str(current.get("viking_api_key") or "")
    return {
        "backend": "viking",
        "viking_api_key": api_key,
        "viking_user": str(
            incoming.get("viking_user", current.get("viking_user", "")) or ""
        ).strip(),
    }


def _hash_password(password: str) -> str:
    raw = str(password or "")
    if not raw:
        return ""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = str(encoded or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _public_space(space: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend": "viking",
        "api_key_present": bool(space.get("viking_api_key")),
        "viking_user": str(space.get("viking_user") or ""),
    }


def _effective_team_key(config, data: dict[str, Any] | None = None) -> str:
    """Return the inherited team OpenViking key.

    The team space is a shared asset. Regular users should default to the
    administrator's team OpenViking key, but the key must not be copied into
    every user profile or exposed through the normal secret endpoint.
    """
    registry = data if isinstance(data, dict) else _load_registry(_registry_path(config))
    users = registry.get("users") or []
    admins = sorted(
        [user for user in users if str(user.get("role") or "user") == "admin"],
        key=lambda item: 0 if str(item.get("id") or "") == "admin" else 1,
    )
    for user in admins:
        key = _space_key(user.get("team_space") or {})
        if key:
            return key
    return (
        str(getattr(config, "sharing_viking_team_api_key", "") or "")
        or str(getattr(config, "sharing_viking_api_key", "") or "")
    )


def _effective_public_space(config, user: dict[str, Any], *, space: str) -> dict[str, Any]:
    raw = user.get("team_space") if space == "team" else user.get("personal_space")
    public = _public_space(raw or {})
    if space == "team" and not public["api_key_present"] and _effective_team_key(config):
        public["backend"] = "viking"
        public["api_key_present"] = True
        public["inherited_from_admin"] = True
    return public


def _public_space_secret(space: dict[str, Any], *, inherited: bool = False) -> dict[str, Any]:
    key = str(space.get("viking_api_key") or "")
    return {
        "backend": "viking",
        "api_key_present": bool(key) or inherited,
        "viking_api_key": "" if inherited else key,
        "viking_user": str(space.get("viking_user") or ""),
        "inherited_from_admin": bool(inherited),
    }


def _public_user(user: dict[str, Any], config=None) -> dict[str, Any]:
    if config is None:
        personal_space = _public_space(user.get("personal_space") or {})
        team_space = _public_space(user.get("team_space") or {})
    else:
        personal_space = _effective_public_space(config, user, space="personal")
        team_space = _effective_public_space(config, user, space="team")
    return {
        "id": user.get("id", ""),
        "display_name": user.get("display_name", ""),
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "agent_identities": dict(user.get("agent_identities") or {}),
        "agent_subjects": [
            dict(item)
            for item in user.get("agent_subjects") or []
            if isinstance(item, dict)
        ],
        "password_set": bool(user.get("password_hash")),
        "personal_space": personal_space,
        "team_space": team_space,
        "created_at": user.get("created_at", ""),
        "updated_at": user.get("updated_at", ""),
    }


def _find_user(data: dict[str, Any], user_id: str) -> tuple[int, dict[str, Any]]:
    for idx, user in enumerate(data.get("users") or []):
        if str(user.get("id") or "") == user_id:
            return idx, user
    raise HTTPException(status_code=404, detail=f"user not found: {user_id}")


def _validate_unique_agent_identities(
    data: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    candidate_id = str(candidate.get("id") or "")
    identities = candidate.get("agent_identities") or {}
    for user in data.get("users") or []:
        if str(user.get("id") or "") == candidate_id:
            continue
        existing = (
            user.get("agent_identities")
            if isinstance(user.get("agent_identities"), dict)
            else {}
        )
        for runtime, username in identities.items():
            if str(existing.get(runtime) or "").strip() == username:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{runtime} username {username!r} is already mapped "
                        f"to user {user.get('id')!r}"
                    ),
                )


def _validate_unique_agent_subjects(
    data: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    candidate_id = str(candidate.get("id") or "")
    subjects = {
        (
            str(item.get("integration_id") or ""),
            str(item.get("external_subject") or ""),
        )
        for item in candidate.get("agent_subjects") or []
        if isinstance(item, dict)
    }
    for user in data.get("users") or []:
        if str(user.get("id") or "") == candidate_id:
            continue
        for item in user.get("agent_subjects") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("integration_id") or ""),
                str(item.get("external_subject") or ""),
            )
            if key in subjects:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Agent subject {key[0]} / {key[1]!r} is already "
                        f"mapped to user {user.get('id')!r}"
                    ),
                )


def resolve_registered_user_id(
    config,
    *,
    runtime_type: str,
    external_username: str,
    preferred_user_id: str = "",
) -> str:
    """Resolve an Agent-side username to a teamEvolver user id."""
    data = _load_registry(_registry_path(config))
    users = [item for item in data.get("users") or [] if isinstance(item, dict)]
    runtime = str(runtime_type or "").strip().lower()
    external = str(external_username or "").strip()
    resolved = ""
    if runtime and external:
        for user in users:
            identities = (
                user.get("agent_identities")
                if isinstance(user.get("agent_identities"), dict)
                else {}
            )
            if str(identities.get(runtime) or "").strip() == external:
                resolved = str(user.get("id") or "")
                break
    if not resolved and external and any(
        str(user.get("id") or "") == external for user in users
    ):
        resolved = external
    preferred = str(preferred_user_id or "").strip()
    if preferred and resolved and preferred != resolved:
        return ""
    return resolved


def resolve_agent_subject_user_id(
    config,
    *,
    integration_id: str,
    runtime_type: str,
    external_subject: str,
    allow_legacy_runtime_mapping: bool = True,
) -> str:
    """Resolve a V1 subject without trusting caller-supplied local user ids."""
    integration = str(integration_id or "").strip()
    runtime = str(runtime_type or "").strip().lower()
    subject = str(external_subject or "").strip()
    if not integration or not runtime or not subject:
        return ""
    data = _load_registry(_registry_path(config))
    users = [item for item in data.get("users") or [] if isinstance(item, dict)]
    for user in users:
        subjects = (
            user.get("agent_subjects")
            if isinstance(user.get("agent_subjects"), list)
            else []
        )
        for item in subjects:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("integration_id") or "").strip() == integration
                and str(item.get("external_subject") or "").strip() == subject
            ):
                return str(user.get("id") or "")
    if not allow_legacy_runtime_mapping:
        return ""
    for user in users:
        identities = (
            user.get("agent_identities")
            if isinstance(user.get("agent_identities"), dict)
            else {}
        )
        if str(identities.get(runtime) or "").strip() == subject:
            return str(user.get("id") or "")
    return ""


def _upsert_user(data: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    raw_id = body.get("id") or body.get("user_id") or body.get("name") or body.get("email")
    user_id = _slug(str(raw_id or ""))
    existing: dict[str, Any] | None = None
    idx: int | None = None
    for i, user in enumerate(data.get("users") or []):
        if str(user.get("id") or "") == user_id:
            existing = user
            idx = i
            break

    created_at = str((existing or {}).get("created_at") or _now())
    user = {
        "id": user_id,
        "display_name": str(body.get("display_name", (existing or {}).get("display_name", user_id)) or user_id),
        "email": str(body.get("email", (existing or {}).get("email", "")) or ""),
        "role": _normalize_role(body.get("role"), str((existing or {}).get("role") or "user")),
        "agent_identities": _normalize_agent_identities(
            body.get("agent_identities"),
            (existing or {}).get("agent_identities"),
        ),
        "agent_subjects": _normalize_agent_subjects(
            body.get("agent_subjects"),
            (existing or {}).get("agent_subjects"),
        ),
        "password_hash": str((existing or {}).get("password_hash") or ""),
        "personal_space": _normalize_space(body.get("personal_space"), (existing or {}).get("personal_space")),
        "team_space": _normalize_space(body.get("team_space"), (existing or {}).get("team_space")),
        "created_at": created_at,
        "updated_at": _now(),
    }
    if str(body.get("password") or ""):
        user["password_hash"] = _hash_password(str(body.get("password") or ""))
    if not user["personal_space"].get("viking_user"):
        user["personal_space"]["viking_user"] = user_id
    _validate_unique_agent_identities(data, user)
    _validate_unique_agent_subjects(data, user)
    if idx is None:
        data.setdefault("users", []).append(user)
    else:
        data["users"][idx] = user
    data["users"] = sorted(data.get("users") or [], key=lambda item: str(item.get("id") or ""))
    return user


def _hub_from_user(config, user: dict[str, Any], *, space: str) -> SkillHub:
    if space not in _SPACES:
        raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
    is_team = space == "team"
    space_cfg = (user.get("team_space") if is_team else user.get("personal_space")) or {}
    inherited_team_key = _effective_team_key(config) if is_team else ""
    effective_key = _space_key(space_cfg) or (inherited_team_key if is_team else "")
    user_id = str(user.get("id") or "")
    fallback_key = (
        inherited_team_key
        if is_team
        else str(getattr(config, "sharing_viking_personal_api_key", "") or "")
    ) or str(getattr(config, "sharing_viking_api_key", "") or "")
    return SkillHub(
        backend="viking",
        endpoint="",
        customer_id="" if is_team else user_id,
        user_alias=str(user.get("display_name") or user_id or "anonymous"),
        viking_endpoint=str(getattr(config, "sharing_viking_endpoint", "") or _DEFAULT_OPENVIKING_ENDPOINT),
        viking_api_key=effective_key or fallback_key,
        viking_account=str(getattr(config, "sharing_viking_account", "") or _DEFAULT_ACCOUNT),
        viking_user=str(getattr(config, "sharing_viking_user", "") or _DEFAULT_USER),
        viking_agent=str(getattr(config, "sharing_viking_agent", "") or _DEFAULT_AGENT),
        viking_agent_id=str(getattr(config, "sharing_viking_agent_id", "") or ""),
        viking_root_prefix=str(getattr(config, "sharing_viking_root_prefix", "") or _DEFAULT_ROOT_PREFIX),
        viking_group_id=str(getattr(config, "sharing_viking_group_id", "") or ""),
        viking_namespace="resources",
    )


def _copy_skills(
    *,
    source_hub: SkillHub,
    target_hub: SkillHub,
    requested: set[str],
) -> dict[str, Any]:
    manifest = source_hub._load_remote_manifest()
    if requested:
        manifest = {name: rec for name, rec in manifest.items() if name in requested}
    missing = sorted(requested - set(manifest))
    if not manifest:
        return {
            "uploaded": 0,
            "skipped": 0,
            "filtered": 0,
            "total_local": 0,
            "shared_names": [],
            "missing_names": missing,
        }

    tmp_root = tempfile.mkdtemp(prefix="teamEvolver_user_share_")
    try:
        tmp_skills = os.path.join(tmp_root, "skills")
        os.makedirs(tmp_skills, exist_ok=True)
        names: list[str] = []
        for name, rec in sorted(manifest.items()):
            bundle = source_hub._download_skill_bundle(name, rec)
            write_skill_bundle(os.path.join(tmp_skills, name), bundle, clean=True)
            names.append(name)
        result = target_hub.push_skills(tmp_skills, include_names=names)
        result["shared_names"] = names
        result["missing_names"] = missing
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _sync_user_space_keys_to_config(config, user: dict[str, Any]) -> Any:
    """Mirror the selected user's OpenViking space keys into global config.

    Team skill sync reads ``sharing.viking_*_api_key`` from the service config.
    User management stores the same credentials in the registry for per-user
    operations, so keep the runtime config in sync whenever a user is saved.
    """
    config_file = str(getattr(config, "_config_file", "") or "").strip()
    if str(user.get("role") or "user") != "admin":
        return config

    personal_key = _space_key(user.get("personal_space") or {})
    team_key = _space_key(user.get("team_space") or {})
    if not config_file:
        if personal_key:
            config.sharing_viking_personal_api_key = personal_key
        else:
            config.sharing_viking_personal_api_key = ""
        if team_key:
            config.sharing_viking_team_api_key = team_key
        else:
            config.sharing_viking_team_api_key = ""
        if personal_key or team_key:
            config.sharing_enabled = True
            config.sharing_backend = "viking"
        return config

    store = ConfigStore(config_file=Path(config_file)) if config_file else ConfigStore()
    data = store.load()
    sharing = data.setdefault("sharing", {})
    if personal_key:
        sharing["viking_personal_api_key"] = personal_key
    else:
        sharing["viking_personal_api_key"] = ""
    if team_key:
        sharing["viking_team_api_key"] = team_key
    else:
        sharing["viking_team_api_key"] = ""
    if personal_key or team_key:
        sharing["enabled"] = True
        sharing["backend"] = "viking"
    store.save(data)
    return store.to_config()


class UsersAdminMixin:
    """CRUD, role and sharing routes for registered teamEvolver users."""

    def _register_users_admin_routes(self, app: FastAPI) -> None:
        owner = self

        @app.get("/api/users")
        async def api_list_users(request: Request):
            data = _load_registry(_registry_path(owner.config))
            users = data.get("users") or []
            if not _is_admin_request(request):
                current_id = str(_request_user(request).get("id") or "")
                users = [user for user in users if str(user.get("id") or "") == current_id]
            return JSONResponse(content={"users": [_public_user(u, owner.config) for u in users]})

        @app.get("/api/users/{user_id}")
        async def api_get_user(user_id: str, request: Request):
            _require_self_or_admin(request, user_id)
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            return JSONResponse(content=_public_user(user, owner.config))

        @app.get("/api/users/{user_id}/spaces/{space}/secret")
        async def api_get_user_space_secret(user_id: str, space: str, request: Request):
            _require_self_or_admin(request, user_id)
            if space not in _SPACES:
                raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            key = "team_space" if space == "team" else "personal_space"
            inherited = (
                space == "team"
                and not _space_key(user.get("team_space") or {})
                and bool(_effective_team_key(owner.config, data))
            )
            return JSONResponse(content=_public_space_secret(user.get(key) or {}, inherited=inherited))

        @app.post("/api/users")
        async def api_upsert_user(body: dict[str, Any], request: Request):
            _require_admin_request(request)
            path = _registry_path(owner.config)
            data = _load_registry(path)
            user = _upsert_user(data, body)
            _save_registry(path, data)
            owner.config = _sync_user_space_keys_to_config(owner.config, user)
            return JSONResponse(content=_public_user(user, owner.config))

        @app.put("/api/users/{user_id}/profile")
        async def api_update_own_profile(
            user_id: str,
            body: dict[str, Any],
            request: Request,
        ):
            _require_self_or_admin(request, user_id)
            path = _registry_path(owner.config)
            data = _load_registry(path)
            _idx, existing = _find_user(data, user_id)
            payload = {
                "id": user_id,
                "display_name": body.get(
                    "display_name", existing.get("display_name", user_id)
                ),
                "email": body.get("email", existing.get("email", "")),
                "role": existing.get("role", "user"),
                "password": body.get("password", ""),
                "personal_space": body.get(
                    "personal_space", existing.get("personal_space", {})
                ),
                "team_space": existing.get("team_space", {}),
                "agent_identities": body.get(
                    "agent_identities", existing.get("agent_identities", {})
                ),
                "agent_subjects": existing.get("agent_subjects", []),
            }
            user = _upsert_user(data, payload)
            _save_registry(path, data)
            return JSONResponse(content=_public_user(user, owner.config))

        @app.delete("/api/users/{user_id}")
        async def api_delete_user(user_id: str, request: Request):
            _require_admin_request(request)
            path = _registry_path(owner.config)
            data = _load_registry(path)
            idx, user = _find_user(data, user_id)
            data["users"].pop(idx)
            _save_registry(path, data)
            return JSONResponse(content={"deleted": True, "id": user.get("id")})

        @app.get("/api/users/{user_id}/skills")
        async def api_list_user_space_skills(
            user_id: str,
            request: Request,
            space: str = Query(default="personal"),
        ):
            _require_self_or_admin(request, user_id)
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            hub = _hub_from_user(owner.config, user, space=space)
            return JSONResponse(content={"space": space, "skills": hub.list_remote()})

        @app.post("/api/users/{user_id}/share")
        async def api_share_skills(user_id: str, request: Request, body: dict[str, Any] | None = None):
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
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            if direction == "personal_to_team" and str(user.get("role") or "user") != "admin":
                raise HTTPException(
                    status_code=403,
                    detail="only admin users can publish personal skills to team space",
                )

            source_space = "personal" if direction == "personal_to_team" else "team"
            target_space = "team" if direction == "personal_to_team" else "personal"
            result = _copy_skills(
                source_hub=_hub_from_user(owner.config, user, space=source_space),
                target_hub=_hub_from_user(owner.config, user, space=target_space),
                requested=requested,
            )
            result["direction"] = direction
            return JSONResponse(content=result)
