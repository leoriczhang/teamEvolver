"""User management REST API for role-based skill-space operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..config import VOLCENGINE_OPENVIKING_ENDPOINT, resolve_viking_endpoint
from ..config_store import ConfigStore
from ..skills import editor
from ..skills.bundle import write_skill_bundle
from ..skills.hub import SkillHub

_LOG = logging.getLogger(__name__)

_DEFAULT_USERS_PATH = Path.home() / ".teamEvolver" / "users.json"
_DEFAULT_OPENVIKING_ENDPOINT = VOLCENGINE_OPENVIKING_ENDPOINT
_DEFAULT_ACCOUNT = "default"
_DEFAULT_USER = "team"
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


def sync_agent_subject_mappings(
    config,
    *,
    integration_id: str,
    runtime_type: str,
    mappings: list[dict[str, Any]],
    authoritative: bool = False,
) -> dict[str, Any]:
    """Apply control-plane subject mappings without provisioning local users."""
    integration = str(integration_id or "").strip()
    runtime = str(runtime_type or "").strip().lower()
    if not integration or not runtime:
        raise ValueError("integration_id and runtime_type are required")
    if len(mappings) > 500:
        raise ValueError("at most 500 subject mappings are allowed")

    desired: dict[str, str] = {}
    conflicts: list[dict[str, str]] = []
    invalid_count = 0
    for item in mappings:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        external_subject = str(item.get("external_subject") or "").strip()
        raw_user_id = str(
            item.get("team_evolver_user_id")
            or item.get("user_id")
            or ""
        ).strip()
        if not external_subject or not raw_user_id:
            invalid_count += 1
            continue
        try:
            user_id = _slug(raw_user_id)
        except HTTPException:
            invalid_count += 1
            continue
        previous = desired.get(external_subject)
        if previous and previous != user_id:
            conflicts.append(
                {
                    "external_subject": external_subject,
                    "existing_user_id": previous,
                    "requested_user_id": user_id,
                }
            )
            continue
        desired[external_subject] = user_id

    path = _registry_path(config)
    data = _load_registry(path)
    users = [
        item
        for item in data.get("users") or []
        if isinstance(item, dict)
    ]
    users_by_id = {
        str(item.get("id") or ""): item
        for item in users
        if str(item.get("id") or "")
    }
    available = {
        subject: user_id
        for subject, user_id in desired.items()
        if user_id in users_by_id
    }
    missing_user_ids = sorted(set(desired.values()) - set(users_by_id))

    removed_count = 0
    if authoritative:
        for user in users:
            user_id = str(user.get("id") or "")
            retained: list[dict[str, Any]] = []
            for item in user.get("agent_subjects") or []:
                if (
                    not isinstance(item, dict)
                    or str(item.get("integration_id") or "").strip()
                    != integration
                ):
                    retained.append(item)
                    continue
                subject = str(item.get("external_subject") or "").strip()
                target = available.get(subject)
                if target == user_id or (
                    subject in desired and subject not in available
                ):
                    retained.append(item)
                else:
                    removed_count += 1
            user["agent_subjects"] = retained

    added_count = 0
    unchanged_count = 0
    mapped_subjects: set[str] = set()
    for external_subject, user_id in available.items():
        owner_id = ""
        for candidate in users:
            for item in candidate.get("agent_subjects") or []:
                if (
                    isinstance(item, dict)
                    and str(item.get("integration_id") or "").strip()
                    == integration
                    and str(item.get("external_subject") or "").strip()
                    == external_subject
                ):
                    owner_id = str(candidate.get("id") or "")
                    break
            if owner_id:
                break
        if owner_id and owner_id != user_id:
            conflicts.append(
                {
                    "external_subject": external_subject,
                    "existing_user_id": owner_id,
                    "requested_user_id": user_id,
                }
            )
            continue

        user = users_by_id[user_id]
        existing = next(
            (
                item
                for item in user.get("agent_subjects") or []
                if isinstance(item, dict)
                and str(item.get("integration_id") or "").strip()
                == integration
                and str(item.get("external_subject") or "").strip()
                == external_subject
            ),
            None,
        )
        if existing is not None:
            unchanged_count += 1
        else:
            subjects = list(user.get("agent_subjects") or [])
            subjects.append(
                {
                    "integration_id": integration,
                    "runtime_type": runtime,
                    "external_subject": external_subject,
                }
            )
            user["agent_subjects"] = _normalize_agent_subjects(subjects)
            added_count += 1
        mapped_subjects.add(external_subject)

    changed = bool(added_count or removed_count)
    if changed:
        for user in users:
            _validate_unique_agent_subjects(data, user)
        data["users"] = sorted(
            users,
            key=lambda item: str(item.get("id") or ""),
        )
        _save_registry(path, data)

    return {
        "mapped_count": len(mapped_subjects),
        "added_count": added_count,
        "unchanged_count": unchanged_count,
        "removed_count": removed_count,
        "invalid_count": invalid_count,
        "missing_user_ids": missing_user_ids,
        "conflicts": conflicts,
    }


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
        from ..skills.mutations import (
            SkillMutationCommand,
            SkillMutationService,
        )

        service = SkillMutationService.from_hub(target_hub)
        commits = [
            service.execute(
                SkillMutationCommand(
                    action="publish",
                    name=name,
                    mutation_id=f"user-share-{uuid.uuid4().hex}",
                    skills_dir=tmp_skills,
                )
            )
            for name in names
        ]
        result = {
            "uploaded": len(commits),
            "skipped": 0,
            "filtered": 0,
            "total_local": len(names),
            "event_ids": [item["event_id"] for item in commits],
        }
        result["shared_names"] = names
        result["missing_names"] = missing
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _read_hub_skill(hub: SkillHub, name: str) -> dict[str, Any] | None:
    """Return ``{name, description, category, body, skill_md, files}`` or None.

    Reads a single skill's bundle straight from the hub's object storage so the
    personal-space editor can round-trip content without a local skills dir.
    """
    from ..skills.frontmatter import parse_skill_md_text

    manifest = hub._load_remote_manifest()
    record = manifest.get(name)
    if not isinstance(record, dict):
        return None
    bundle = hub._download_skill_bundle(name, record)
    skill_md = bundle.get("SKILL.md", b"").decode("utf-8", errors="replace")
    parsed = parse_skill_md_text(skill_md) or {}
    body = skill_md.split("---", 2)[-1].strip() if skill_md.count("---") >= 2 else skill_md
    return {
        "name": name,
        "description": str(parsed.get("description") or record.get("description") or ""),
        "category": str(record.get("category") or "general"),
        "body": body,
        "skill_md": skill_md,
        "files": sorted(bundle.keys()),
    }


def _publish_skill_to_hub(
    target_hub: SkillHub,
    *,
    name: str,
    skill_md: str,
    mutation_prefix: str,
) -> dict[str, Any]:
    """Write a single SKILL.md into ``target_hub`` via the mutation service."""
    from ..skills.mutations import SkillMutationCommand, SkillMutationService

    tmp_root = tempfile.mkdtemp(prefix="teamEvolver_skill_edit_")
    try:
        skill_dir = os.path.join(tmp_root, "skills", name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(skill_md)
        service = SkillMutationService.from_hub(target_hub)
        return service.execute(
            SkillMutationCommand(
                action="publish",
                name=name,
                mutation_id=f"{mutation_prefix}-{uuid.uuid4().hex}",
                skills_dir=os.path.join(tmp_root, "skills"),
            )
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _publish_request_bucket(config):
    """Return the shared object-store bucket for publish requests, or None."""
    hub = SkillHub.object_storage_from_config(config)
    return hub._bucket if hub is not None else None


def _publish_request_key(request_id: str) -> str:
    return f"skill_publish_requests/{_slug(request_id)}.json"


def _load_publish_requests(config) -> list[dict[str, Any]]:
    bucket = _publish_request_bucket(config)
    if bucket is None:
        return []
    from ..storage import is_not_found_error

    rows: list[dict[str, Any]] = []
    for obj in bucket.iter_objects(prefix="skill_publish_requests/"):
        key = str(getattr(obj, "key", "") or "")
        if not key.endswith(".json"):
            continue
        try:
            raw = bucket.get_object(key).read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - skip unreadable request blobs
            if is_not_found_error(exc):
                continue
            raise
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            rows.append(record)
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows


def _save_publish_request(config, record: dict[str, Any]) -> None:
    bucket = _publish_request_bucket(config)
    if bucket is None:
        raise HTTPException(
            status_code=503,
            detail="OpenViking object storage is not configured for publish requests",
        )
    bucket.put_object(
        _publish_request_key(str(record.get("request_id") or "")),
        json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _openviking_endpoint(config) -> str:
    return resolve_viking_endpoint(
        str(getattr(config, "sharing_viking_deployment", "") or "cloud"),
        str(getattr(config, "sharing_viking_endpoint", "") or ""),
    ).rstrip("/")


def _openviking_root_key(config) -> str:
    return (
        str(getattr(config, "sharing_viking_api_key", "") or "").strip()
        or str(getattr(config, "sharing_viking_team_api_key", "") or "").strip()
        or str(getattr(config, "sharing_viking_personal_api_key", "") or "").strip()
    )


def sync_openviking_user(config, user_id: str) -> dict[str, Any]:
    """Ensure a same-name OpenViking user exists under the default account.

    Fail-open: any transport / auth error is logged and swallowed so
    teamEvolver user creation is never blocked by OpenViking availability.
    Studio visibility is a downstream nice-to-have, not a hard dependency.
    """
    endpoint = _openviking_endpoint(config)
    api_key = _openviking_root_key(config)
    account = str(getattr(config, "sharing_viking_account", "") or "default").strip() or "default"
    slug = _slug(str(user_id or ""))
    result: dict[str, Any] = {
        "account_id": account,
        "user_id": slug,
        "synced": False,
        "already_exists": False,
        "endpoint": endpoint,
    }
    if not endpoint or not api_key:
        result["error"] = "OpenViking endpoint or API key not configured"
        _LOG.warning("openviking sync skipped for %s: %s", slug, result["error"])
        return result
    url = f"{endpoint}/api/v1/admin/accounts/{account}/users"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    body = {"user_id": slug, "role": "user"}
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        result["error"] = f"transport error: {exc}"
        _LOG.warning("openviking sync failed for %s: %s", slug, exc)
        return result
    if response.status_code in (200, 201):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        result["synced"] = True
        result["response"] = payload
        return result
    text = (response.text or "")[:400]
    if response.status_code == 409 or "already" in text.lower() or "exists" in text.lower():
        result["already_exists"] = True
        result["synced"] = True
        return result
    result["error"] = f"HTTP {response.status_code}: {text}"
    _LOG.warning("openviking sync failed for %s: %s", slug, result["error"])
    return result


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
            sync_report = sync_openviking_user(owner.config, str(user.get("id") or ""))
            payload = _public_user(user, owner.config)
            payload["openviking_sync"] = sync_report
            return JSONResponse(content=payload)

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

        @app.get("/api/users/{user_id}/skills/{name}")
        async def api_get_user_space_skill(
            user_id: str,
            name: str,
            request: Request,
            space: str = Query(default="personal"),
        ):
            _require_self_or_admin(request, user_id)
            if space not in _SPACES:
                raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            hub = _hub_from_user(owner.config, user, space=space)
            detail = _read_hub_skill(hub, str(name or "").strip())
            if detail is None:
                raise HTTPException(status_code=404, detail=f"skill not found: {name}")
            return JSONResponse(content=detail)

        @app.post("/api/users/{user_id}/skills")
        async def api_save_user_space_skill(
            user_id: str,
            body: dict[str, Any],
            request: Request,
        ):
            """Create/update a skill in the user's own personal space.

            Users may freely edit their personal skills. Editing the shared team
            space still requires admin (guarded below), keeping the team library
            behind the review gate.
            """
            _require_self_or_admin(request, user_id)
            space = str(body.get("space") or "personal")
            if space not in _SPACES:
                raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
            if space == "team" and not _is_admin_request(request):
                raise HTTPException(
                    status_code=403,
                    detail="only admin users can edit team skills",
                )
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
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            hub = _hub_from_user(owner.config, user, space=space)
            commit = _publish_skill_to_hub(
                hub,
                name=name,
                skill_md=skill_md,
                mutation_prefix=f"user-{user_id}-edit",
            )
            return JSONResponse(content={"name": name, "space": space, "commit": commit})

        @app.delete("/api/users/{user_id}/skills/{name}")
        async def api_delete_user_space_skill(
            user_id: str,
            name: str,
            request: Request,
            space: str = Query(default="personal"),
        ):
            _require_self_or_admin(request, user_id)
            if space not in _SPACES:
                raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
            if space == "team" and not _is_admin_request(request):
                raise HTTPException(
                    status_code=403,
                    detail="only admin users can delete team skills",
                )
            data = _load_registry(_registry_path(owner.config))
            _idx, user = _find_user(data, user_id)
            hub = _hub_from_user(owner.config, user, space=space)
            result = hub.delete_skill(str(name or "").strip())
            return JSONResponse(content={"space": space, **result})

        @app.get("/api/skill-publish-requests")
        async def api_list_publish_requests(request: Request):
            """List publish requests. Admins see all; users see only their own."""
            requests = _load_publish_requests(owner.config)
            if not _is_admin_request(request):
                current_id = str(_request_user(request).get("id") or "")
                requests = [
                    item
                    for item in requests
                    if str(item.get("requester_id") or "") == current_id
                ]
            pending = sum(1 for item in requests if str(item.get("status")) == "pending")
            return JSONResponse(content={"requests": requests, "pending_count": pending})

        @app.post("/api/users/{user_id}/publish-requests")
        async def api_submit_publish_request(
            user_id: str,
            body: dict[str, Any],
            request: Request,
        ):
            """Submit a personal→team publish request for admin approval."""
            _require_self_or_admin(request, user_id)
            requested = sorted(
                {
                    str(name or "").strip()
                    for name in (body.get("skill_names") or body.get("skills") or [])
                    if str(name or "").strip()
                }
            )
            if not requested:
                raise HTTPException(status_code=400, detail="skill_names must not be empty")
            data = _load_registry(_registry_path(owner.config))
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
            _save_publish_request(owner.config, record)
            return JSONResponse(content=record)

        @app.post("/api/skill-publish-requests/{request_id}/approve")
        async def api_approve_publish_request(
            request_id: str,
            request: Request,
            body: dict[str, Any] | None = None,
        ):
            """Admin approves a request: copy personal skills into the team space."""
            _require_admin_request(request)
            requests = {
                str(item.get("request_id") or ""): item
                for item in _load_publish_requests(owner.config)
            }
            record = requests.get(_slug(request_id))
            if record is None:
                raise HTTPException(status_code=404, detail=f"request not found: {request_id}")
            if str(record.get("status")) != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=f"request already {record.get('status')}",
                )
            data = _load_registry(_registry_path(owner.config))
            _idx, requester = _find_user(data, str(record.get("requester_id") or ""))
            result = _copy_skills(
                source_hub=_hub_from_user(owner.config, requester, space="personal"),
                target_hub=_hub_from_user(owner.config, requester, space="team"),
                requested=set(record.get("skill_names") or []),
            )
            record.update(
                {
                    "status": "approved",
                    "decided_by": str(_request_user(request).get("id") or "admin"),
                    "decided_at": _now(),
                    "updated_at": _now(),
                    "decision_note": str((body or {}).get("note") or "")[:2000],
                    "result": result,
                }
            )
            _save_publish_request(owner.config, record)
            return JSONResponse(content=record)

        @app.post("/api/skill-publish-requests/{request_id}/reject")
        async def api_reject_publish_request(
            request_id: str,
            request: Request,
            body: dict[str, Any] | None = None,
        ):
            _require_admin_request(request)
            requests = {
                str(item.get("request_id") or ""): item
                for item in _load_publish_requests(owner.config)
            }
            record = requests.get(_slug(request_id))
            if record is None:
                raise HTTPException(status_code=404, detail=f"request not found: {request_id}")
            if str(record.get("status")) != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=f"request already {record.get('status')}",
                )
            record.update(
                {
                    "status": "rejected",
                    "decided_by": str(_request_user(request).get("id") or "admin"),
                    "decided_at": _now(),
                    "updated_at": _now(),
                    "decision_note": str((body or {}).get("note") or "")[:2000],
                }
            )
            _save_publish_request(owner.config, record)
            return JSONResponse(content=record)
