"""Defaults and normalization helpers for the teamEvolver config store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import TEAM_SKILL_ROOT_PREFIX, VOLCENGINE_OPENVIKING_ENDPOINT

CONFIG_DIR = Path.home() / ".teamEvolver"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
_DEFAULT_SKILLS_DIR = CONFIG_DIR / "skills"
_DEFAULT_HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"
_FALLBACK_LLM_API_MODE = "chat"
_SKILL_RELOAD_MODES = {"off", "poll", "callback"}
_MIN_SKILL_RELOAD_INTERVAL_SECONDS = 5

_DEFAULTS: dict = {
    "llm": {
        "provider": "custom",
        "model_id": "doubao-seed-evolving",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "",
        "max_tokens": 100000,
        "temperature": 0.4,
    },
    "service": {
        "port": 52010,
        "host": "0.0.0.0",
    },
    "skills": {
        "enabled": True,
        "dir": str(_DEFAULT_SKILLS_DIR),
    },
    "openrouter": {
        "app_name": "teamEvolver",
        "app_url": "",
        "route": "fallback",
        "fallback_models": "",
        "data_policy": "",
    },
    "sharing": {
        "enabled": True,
        "backend": "viking",
        "endpoint": "",
        "local_root": "",
        "skill_backend": "",
        "session_backend": "",
        "viking_endpoint": VOLCENGINE_OPENVIKING_ENDPOINT,
        # Backward-compatible fallback. Prefer the scoped keys when the caller
        # has separate personal and team OpenViking credentials.
        "viking_api_key": "",
        "viking_personal_api_key": "",
        "viking_personal_api_keys": [],
        "viking_team_api_key": "",
        "viking_account": "default",
        "viking_user": "default",
        # wire constant: OpenViking agent namespace, do not rename
        "viking_agent": TEAM_SKILL_ROOT_PREFIX,
        "viking_agent_id": "",
        "viking_customer_id": "",
        "viking_root_prefix": TEAM_SKILL_ROOT_PREFIX,
        "viking_group_id": "",
        "user_alias": "",
        "auto_pull_on_start": True,
        "push_min_injections": 5,
        "push_min_effectiveness": 0.3,
        "session_upload_interval": 0,
        "skill_reload_mode": "poll",
        "skill_reload_interval_seconds": 30,
    },
    "evolve": {
        "server_url": "http://127.0.0.1:52010",
        "evidence_enabled": True,
        "evidence_max_entries": 200,
        "evidence_recent_limit": 12,
        "evidence_historical_limit": 12,
        "evidence_replay_cases_per_window": 1,
        "evidence_change_debt_threshold": 3,
        "candidate_coalesce_enabled": True,
    },
    "dreamcycle": {
        "enabled": False,
        "auto_start": False,
        "daemon_command": "dreamcycle --daemon",
        "trigger_command": "dreamcycle --once",
        "viking_agent": "dreamcycle",
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
    },
    "validation": {
        "enabled": True,
        "mode": "replay",
        "idle_after_seconds": 300,
        "poll_interval_seconds": 60,
        "max_jobs_per_day": 5,
        "max_concurrency": 1,
        "required_results": 3,
        "required_approvals": 2,
        "agentshub_url": "",
        "agentshub_api_key": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _coerce(value: Any) -> Any:
    """Auto-coerce string values to bool/int/float where obvious."""
    if not isinstance(value, str):
        return value
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _first_non_empty(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _infer_sharing_backend(sharing: dict[str, Any]) -> str:
    backend = str(sharing.get("backend", "") or "").strip().lower()
    if backend:
        return backend
    if sharing.get("local_root"):
        return "local"
    has_viking_key = any(
        sharing.get(key)
        for key in (
            "viking_api_key",
            "viking_personal_api_key",
            "viking_team_api_key",
            "viking_user_api_key",
            "viking_resources_api_key",
        )
    )
    if sharing.get("viking_endpoint") and (sharing.get("enabled") or has_viking_key):
        return "viking"
    return ""


def _normalize_validation_mode(value: Any) -> str:
    normalized = str(value or "replay").strip().lower().replace("-", "_")
    return normalized if normalized in {"replay", "true_replay"} else "replay"


def _normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _normalize_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    elif value in (None, ""):
        values = []
    else:
        values = str(value).replace("\n", ",").split(",")
    return list(
        dict.fromkeys(
            item
            for raw in values
            if (item := str(raw or "").strip())
        )
    )


def _normalize_reload_interval(value: Any) -> int:
    try:
        interval = int(value or 30)
    except (TypeError, ValueError):
        interval = 30
    return max(_MIN_SKILL_RELOAD_INTERVAL_SECONDS, interval)


def resolve_skills_dir(skills_dir: Any) -> str:
    """Normalize a configured skills dir, applying Hermes-native defaults."""
    raw = str(skills_dir or "").strip()
    generic_default = _DEFAULT_SKILLS_DIR.expanduser()

    if raw:
        expanded = Path(raw).expanduser()
        if expanded == generic_default:
            return str(_DEFAULT_HERMES_SKILLS_DIR)
        return str(expanded)

    return str(_DEFAULT_HERMES_SKILLS_DIR)
