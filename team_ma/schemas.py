"""Connection validation and canonical hashes; credentials are opaque references."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from teamEvolver.managed_agents.client import validate_url
from teamEvolver.security.agent_principal import normalize_user_id

DEFAULTS = {
    "provider": "volcengine-managed-agents", "ma_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "team_evolver_url": "http://127.0.0.1:52010", "user_tag_prefix": "te-user:",
    "poll_interval_seconds": 60, "sync_interval_seconds": 30, "vault_ids": [],
    "replay_enabled": False, "replay_environment_id": "", "replay_vault_ids": [], "runtime_binding": {},
}
FIELDS = set(DEFAULTS) | {"display_name", "entry_agent_id", "environment_id", "default_user_id",
                         "ma_secret_ref", "tenant_token_ref", "replay_isolation", "enabled"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


class MAConflict(ValueError):
    pass


def validate_config(raw):
    if set(raw) - FIELDS:
        raise ValueError("Unknown MA connection fields: " + ", ".join(sorted(set(raw) - FIELDS)))
    result = {**DEFAULTS, **raw}
    if result["provider"] != DEFAULTS["provider"]:
        raise ValueError("Unsupported MA provider")
    for key in ("display_name", "entry_agent_id", "environment_id", "default_user_id",
                "ma_secret_ref", "tenant_token_ref"):
        if not isinstance(result.get(key), str) or not result[key].strip() or len(result[key]) > 200:
            raise ValueError(f"{key} is required (1–200 characters)")
        result[key] = result[key].strip()
    result["ma_base_url"] = validate_url(result["ma_base_url"])
    result["default_user_id"] = normalize_user_id(result["default_user_id"])
    target = urlsplit(result["team_evolver_url"])
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
        raise ValueError("Invalid team_evolver_url")
    if target.query or target.fragment:
        raise ValueError("team_evolver_url cannot contain query or fragment")
    for key in ("poll_interval_seconds", "sync_interval_seconds"):
        if type(result[key]) is not int or not 15 <= result[key] <= 86400:
            raise ValueError(f"{key} must be 15–86400")
    for key in ("vault_ids", "replay_vault_ids"):
        if not isinstance(result[key], list) or any(not isinstance(v, str) or not v for v in result[key]):
            raise ValueError(f"{key} must be a list of IDs")
    if type(result["replay_enabled"]) is not bool or type(result.get("enabled", True)) is not bool:
        raise ValueError("replay_enabled and enabled must be boolean")
    if not isinstance(result["user_tag_prefix"], str) or not 1 <= len(result["user_tag_prefix"]) <= 100:
        raise ValueError("user_tag_prefix must contain 1–100 characters")
    if not isinstance(result["replay_environment_id"], str) or len(result["replay_environment_id"]) > 200:
        raise ValueError("Invalid replay_environment_id")
    if result["replay_enabled"] and not result["replay_environment_id"]:
        raise ValueError("replay_environment_id is required for isolated Replay")
    if not isinstance(result["runtime_binding"], dict):
        raise ValueError("runtime_binding must be an object")
    isolation = result.get("replay_isolation") or {}
    if (not isinstance(isolation, dict) or set(isolation) - {"mcp", "custom_tools", "vaults"}
            or any(type(v) is not bool for v in isolation.values())):
        raise ValueError("Invalid replay_isolation")
    return result


def target_fingerprint(config):
    return digest([config["ma_base_url"], config["entry_agent_id"]])


def public_config(config):
    return {**{k: v for k, v in config.items() if k not in {"ma_secret_ref", "tenant_token_ref"}},
            "ma_credential_configured": bool(config.get("ma_secret_ref")),
            "tenant_credential_configured": bool(config.get("tenant_token_ref"))}
