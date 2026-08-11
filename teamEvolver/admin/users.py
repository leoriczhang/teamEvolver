"""User registry and role helpers for the unified console."""

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

from fastapi import HTTPException

from teamEvolver.config import VOLCENGINE_OPENVIKING_ENDPOINT
from teamEvolver.skills.bundle import write_skill_bundle
from teamEvolver.skills.hub import SkillHub

DEFAULT_USERS_PATH = Path.home() / ".teamEvolver" / "users.json"
DEFAULT_OPENVIKING_ENDPOINT = VOLCENGINE_OPENVIKING_ENDPOINT
DEFAULT_ACCOUNT = "default"
DEFAULT_USER = "default"
DEFAULT_AGENT = "teamEvolver"
DEFAULT_ROOT_PREFIX = "teamEvolver"
PASSWORD_ITERATIONS = 260_000

ROLES = {"user", "admin"}
SPACES = {"personal", "team"}
DIRECTIONS = {"personal_to_team", "team_to_personal"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def slug(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value or "").strip())
    out = out.strip(".-_")
    if not out:
        raise HTTPException(status_code=400, detail="user id must not be empty")
    return out


def registry_path(config) -> Path:
    path = str(getattr(config, "users_registry_path", "") or "").strip()
    return Path(path).expanduser() if path else DEFAULT_USERS_PATH


def local_root(config, user_id: str, *, team: bool) -> str:
    base = registry_path(config).parent / "skill_spaces"
    if team:
        return str(base / "team")
    return str(base / "users" / slug(user_id) / "personal")


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"users": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to read users registry: {exc}") from exc
    if not isinstance(data, dict):
        return {"users": []}
    if not isinstance(data.get("users"), list):
        data["users"] = []
    return data


def save_registry(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def hash_password(password: str) -> str:
    raw = str(password or "")
    if not raw:
        return ""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
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


def normalize_role(value: Any, existing: str = "user") -> str:
    role = str(value or existing or "user").strip().lower()
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"unsupported role: {role}")
    return role


def normalize_space(raw: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    incoming = raw if isinstance(raw, dict) else {}
    current = existing if isinstance(existing, dict) else {}
    if incoming.get("clear_viking_api_key"):
        api_key = ""
    else:
        key_value = incoming.get("viking_api_key", None)
        if key_value not in (None, ""):
            api_key = str(key_value)
        else:
            api_key = str(current.get("viking_api_key") or "")
    return {"backend": "viking" if api_key else "local", "viking_api_key": api_key}


def public_space_secret(space: dict[str, Any]) -> dict[str, Any]:
    api_key = str((space or {}).get("viking_api_key") or "")
    if api_key:
        return {"backend": "viking", "viking_api_key": api_key, "api_key_present": True}
    return {"backend": "local", "viking_api_key": "", "api_key_present": False}


def public_space(space: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend": str(space.get("backend") or "local"),
        "api_key_present": bool(space.get("viking_api_key")),
    }


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id", ""),
        "display_name": user.get("display_name", ""),
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "password_set": bool(user.get("password_hash")),
        "personal_space": public_space(user.get("personal_space") or {}),
        "team_space": public_space(user.get("team_space") or {}),
        "created_at": user.get("created_at", ""),
        "updated_at": user.get("updated_at", ""),
    }


def find_user(data: dict[str, Any], user_id: str) -> tuple[int, dict[str, Any]]:
    for idx, user in enumerate(data.get("users") or []):
        if str(user.get("id") or "") == user_id:
            return idx, user
    raise HTTPException(status_code=404, detail=f"user not found: {user_id}")


def upsert_user(data: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    raw_id = body.get("id") or body.get("user_id") or body.get("name") or body.get("email")
    user_id = slug(str(raw_id or ""))
    existing: dict[str, Any] | None = None
    idx: int | None = None
    for i, user in enumerate(data.get("users") or []):
        if str(user.get("id") or "") == user_id:
            existing = user
            idx = i
            break

    created_at = str((existing or {}).get("created_at") or now_iso())
    user = {
        "id": user_id,
        "display_name": str(body.get("display_name", (existing or {}).get("display_name", user_id)) or user_id),
        "email": str(body.get("email", (existing or {}).get("email", "")) or ""),
        "role": normalize_role(body.get("role"), str((existing or {}).get("role") or "user")),
        "password_hash": str((existing or {}).get("password_hash") or ""),
        "personal_space": normalize_space(body.get("personal_space"), (existing or {}).get("personal_space")),
        "team_space": normalize_space(body.get("team_space"), (existing or {}).get("team_space")),
        "created_at": created_at,
        "updated_at": now_iso(),
    }
    if str(body.get("password") or ""):
        user["password_hash"] = hash_password(str(body.get("password") or ""))
    if idx is None:
        data.setdefault("users", []).append(user)
    else:
        data["users"][idx] = user
    data["users"] = sorted(data.get("users") or [], key=lambda item: str(item.get("id") or ""))
    return user


def hub_from_user(config, user: dict[str, Any], *, space: str) -> SkillHub:
    if space not in SPACES:
        raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
    is_team = space == "team"
    space_cfg = (user.get("team_space") if is_team else user.get("personal_space")) or {}
    backend = str(space_cfg.get("backend") or "local")
    user_id = str(user.get("id") or "")
    if backend == "viking":
        return SkillHub(
            backend="viking",
            endpoint="",
            local_root="",
            customer_id="" if is_team else user_id,
            user_alias=str(user.get("display_name") or user_id or "anonymous"),
            viking_endpoint=DEFAULT_OPENVIKING_ENDPOINT,
            viking_api_key=str(space_cfg.get("viking_api_key") or ""),
            viking_account=DEFAULT_ACCOUNT,
            viking_user=DEFAULT_USER,
            viking_agent=DEFAULT_AGENT,
            viking_agent_id="",
            viking_root_prefix=DEFAULT_ROOT_PREFIX,
            viking_group_id="",
            viking_namespace="resources",
        )
    return SkillHub(
        backend="local",
        endpoint="",
        local_root=local_root(config, user_id, team=is_team),
        customer_id="",
        user_alias=str(user.get("display_name") or user_id or "anonymous"),
    )


def copy_skills(*, source_hub: SkillHub, target_hub: SkillHub, requested: set[str]) -> dict[str, Any]:
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
