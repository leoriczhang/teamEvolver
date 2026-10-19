"""Console user profile and personal Skill-space helpers.

User records contain their OpenViking identity and space bindings; trusted
OpenViking access uses the backend Root Key. Routers and directory bootstrap
reuse these helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request

_DEFAULT_ACCOUNT = "default"
_DEFAULT_USER = "team"
_ROLES = {"user", "admin"}
_SPACES = {"personal", "team"}
_DIRECTIONS = {"personal_to_team", "team_to_personal"}
_PASSWORD_ITERATIONS = 260_000


def _request_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "console_user", None)
    return user if isinstance(user, dict) else {}


def _is_admin_request(request: Request) -> bool:
    from .authorization import is_tenant_admin

    return is_tenant_admin(_request_user(request))


def _require_admin_request(request: Request) -> None:
    from .authorization import require_tenant_admin

    require_tenant_admin(_request_user(request))


def _require_self_or_admin(request: Request, user_id: str) -> None:
    from ..tenants.registry import current_tenant_id
    from .authorization import is_tenant_admin

    current = _request_user(request)
    if is_tenant_admin(current):
        store = getattr(request.state, "identity_store", None)
        if store is None or any(m["status"] == "active" for m in store.memberships(
            user_id=user_id, tenant_id=current_tenant_id()
        )):
            return
    if str(current.get("id") or "") != str(user_id or ""):
        raise HTTPException(status_code=403, detail="users can only access their own resources")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value or "").strip())
    out = out.strip(".-_")
    if not out:
        raise HTTPException(status_code=400, detail="user id must not be empty")
    return out








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

    Trusted mode keeps credentials at the service level. A user binding stores
    only the OpenViking user name; account and Root Key come from tenant config.
    """
    incoming = raw if isinstance(raw, dict) else {}
    current = existing if isinstance(existing, dict) else {}
    return {
        "backend": "viking",
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
        "viking_user": str(space.get("viking_user") or ""),
        "bound": bool(str(space.get("viking_user") or "").strip()),
    }


def _effective_team_key(config, data: dict[str, Any] | None = None) -> str:
    """Return the tenant-level Trusted Root Key."""
    del data
    return str(getattr(config, "sharing_viking_api_key", "") or "")


def _effective_public_space(config, user: dict[str, Any], *, space: str) -> dict[str, Any]:
    del config
    raw = user.get("team_space") if space == "team" else user.get("personal_space")
    return _public_space(raw or {})


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
        "platform_role": user.get("platform_role", "user"),
        "status": user.get("status", "active"),
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








def _default_agent_subjects(config, user_id: str) -> list[dict[str, str]]:
    return []


def _upsert_user(
    data: dict[str, Any],
    body: dict[str, Any],
    *,
    config=None,
) -> dict[str, Any]:
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
    agent_subjects = body.get("agent_subjects")
    if existing is None and "agent_subjects" not in body:
        agent_subjects = _default_agent_subjects(config, user_id)
    user = {
        "id": user_id,
        "display_name": str(body.get("display_name", (existing or {}).get("display_name", user_id)) or user_id),
        "email": str(body.get("email", (existing or {}).get("email", "")) or ""),
        "role": _normalize_role(body.get("role"), str((existing or {}).get("role") or "user")),
        "platform_role": (existing or {}).get("platform_role", "user"),
        "status": (existing or {}).get("status", "active"),
        "external_identity": (existing or {}).get("external_identity"),
        "agent_identities": _normalize_agent_identities(
            body.get("agent_identities"),
            (existing or {}).get("agent_identities"),
        ),
        "agent_subjects": _normalize_agent_subjects(
            agent_subjects,
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
    local_openviking = (
        str(getattr(config, "sharing_viking_deployment", "") or "").lower()
        == "local"
    )
    if local_openviking or not user["personal_space"].get("viking_user"):
        user["personal_space"]["viking_user"] = user_id
    if not user["team_space"].get("viking_user"):
        user["team_space"]["viking_user"] = str(
            getattr(config, "sharing_viking_user", "") or _DEFAULT_USER
        )
    # The team-resource name is fixed at registration. Account and Root Key are
    # tenant-level settings and never live in the user record.
    existing_team_user = str(
        ((existing or {}).get("team_space") or {}).get("viking_user") or ""
    ).strip()
    new_team_user = str(user["team_space"].get("viking_user") or "").strip()
    if existing_team_user and new_team_user and existing_team_user != new_team_user:
        raise HTTPException(
            status_code=409,
            detail=(
                f"team resource name is already bound to '{existing_team_user}'; "
                "the team resource name cannot be changed after registration"
            ),
        )
    _validate_unique_agent_identities(data, user)
    _validate_unique_agent_subjects(data, user)
    if idx is None:
        data.setdefault("users", []).append(user)
    else:
        data["users"][idx] = user
    data["users"] = sorted(data.get("users") or [], key=lambda item: str(item.get("id") or ""))
    return user
