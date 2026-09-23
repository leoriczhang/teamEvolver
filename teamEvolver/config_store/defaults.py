"""Defaults and normalization helpers for the teamEvolver config store."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from team_memory.config import MEMORY_DEFAULTS

from ..config import TEAM_SKILL_ROOT_PREFIX


def _resolve_config_dir() -> Path:
    """Resolve the config directory.

    Priority: ``TEAMEVOLVER_CONFIG_DIR`` env var > ``~/.teamEvolver``.
    """
    env_dir = os.environ.get("TEAMEVOLVER_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".teamEvolver"


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.yaml"
_DEFAULT_SKILLS_DIR = CONFIG_DIR / "skills"
_DEFAULT_HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"
_FALLBACK_LLM_API_MODE = "chat"
_SKILL_RELOAD_MODES = {"off", "poll", "callback"}
_MIN_SKILL_RELOAD_INTERVAL_SECONDS = 5

_DEFAULTS: dict = {
    "experience_sync": {
        "enabled": False,
        "target_directory": "viking://resources/agent_knowledge_workspace/input/proven_experiences",
        "interval_seconds": 30,
        "batch_size": 100,
        "full_scan_interval_seconds": 86400,
        "import_max_source_mb": 64,
    },
    "team": {
        "display_name": "Team",
    },
    "llm": {
        "provider": "custom",
        "model_id": "volcengine/glm-5.2-aicc",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "",
        "max_tokens": 100000,
        "temperature": 0.4,
        "max_concurrency": 8,
        "queue_capacity": 64,
    },
    "service": {
        "port": 52010,
        "host": "0.0.0.0",
    },
    "skills": {
        "enabled": True,
        "dir": str(_DEFAULT_SKILLS_DIR),
        "delivery_mode": "push",
    },
    "agent_protocol": {"identity_mode": "dual"},
    "replay": {"adapter": "", "adapters_dir": ""},
    # DEAP /skillopt/update rollout consumer (see integrations/skillopt_rollout.py).
    "skillopt_rollout": {
        "enabled": False,
        "endpoint": "",
        "workspace_prefix": "rollout-",
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
        # Deployment target for OpenViking: ``cloud`` (Volcengine-hosted) or
        # ``local`` (self-hosted openviking-server). Drives the endpoint when
        # ``viking_endpoint`` is left empty.
        "viking_deployment": "cloud",
        "endpoint": "",
        # Empty values resolve to PostgreSQL when storage_pg is enabled,
        # otherwise to the built-in local store.
        "skill_backend": "",
        "session_backend": "",
        # Async mirror of the team skill library to OpenViking: evolved/pushed
        # skills land on the built-in local backend first, then mirror into
        # viking://resources/{root_prefix}/skills/<name>/ so remote Agents can
        # keep reading them. Only that subtree mirrors; registry/manifest stay
        # local. Empty spool_dir means ~/.teamEvolver/skill_mirror_spool.
        "skill_mirror_enabled": True,
        "skill_mirror_spool_dir": "",
        # Built-in local-storage fallback: when the configured OpenViking is
        # unavailable (connection error / timeout / HTTP 5xx), object stores
        # fall back to teamEvolver's own filesystem store. Empty ``local_root``
        # means ~/.teamEvolver/local_store.
        "local_fallback_enabled": True,
        "local_root": "",
        # Dedicated root for uploaded/evolved Skill bundles and archived
        # versions. Empty inherits local_root; production may mount a NAS here.
        "skill_local_root": "",
        # Empty means "derive from viking_deployment"; set a value only to
        # override the cloud/local default endpoint.
        "viking_endpoint": "",
        # Backward-compatible fallback. Prefer the scoped keys when the caller
        # has separate personal and service OpenViking credentials.
        "viking_api_key": "",
        "viking_personal_api_key": "",
        "viking_personal_api_keys": [],
        # Service/admin OpenViking key used for team resources, skill sync, and
        # aggregation. The field name is kept for existing config files.
        "viking_team_api_key": "",
        "viking_account": "default",
        "viking_personal_user": "",
        "viking_user": "team",
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
    # PostgreSQL local-state storage (multi-tenancy plan §2.4): shares the
    # OpenViking PG instance; empty dsn derives from OV_PG_* env vars.
    "storage_pg": {
        "enabled": False,
        "dsn": "",
        "schema": "teamevolver",
        "pool_min": 2,
        "pool_max": 20,
        "command_timeout_seconds": 30.0,
        "ssl": "prefer",
        # Dedicated control-plane pool for admin/tenant queries so background
        # work (evolution + session judging) can never starve admin endpoints.
        "control_pool_max": 5,
    },
    # Single-tenant machine credential (env ``TEAMEVOLVER_TENANT_TOKEN``).
    # Multi-tenant deployments issue per-tenant credentials from the console.
    "tenant": {
        "machine_token": "",
    },
    "evolve": {
        "server_url": "http://127.0.0.1:52010",
        "use_session_judge": True,
        "publish_mode": "validated",
        "validation_max_rejections": 1,
        "human_review_enabled": True,
        "human_review_timeout_seconds": 86400,
        "interval_seconds": 600,
        "evidence_enabled": True,
        "evidence_max_entries": 400,
        "evidence_recent_limit": 20,
        "evidence_historical_limit": 20,
        "evidence_replay_cases_per_window": 1,
        "evidence_change_debt_threshold": 3,
        "dataset_synthesis_enabled": True,
        "dataset_test_cases": 2,
        "dataset_min_requirements": 12,
        "dataset_max_requirements": 24,
        "dataset_disclosure_batch_size": 4,
        "candidate_coalesce_enabled": True,
        "max_parallel_groups": 4,
        "agent_max_rounds": 12,
        "agent_max_tool_calls_per_round": 8,
        "min_group_sessions": 2,
        "min_group_users": 2,
        "bundle_text_extensions": [".py", ".sh"],
        "bundle_max_file_bytes": 262144,
        "bundle_max_prompt_bytes": 786432,
        "bundle_allow_delete": True,
        "bundle_static_checks_enabled": True,
    },
    "mining": {
        "pipeline": {
            "max_rounds": 3,
            "max_retries": 2,
            "retry_backoff_seconds": 0.8,
            "oneshot_timeout_seconds": 1800,
            "step1_validation_retries": 1,
            "strict_step1": True,
            "benchmark_target_total": 16,
            "benchmark_difficulty_dist": "easy:4,medium:7,hard:5",
            "benchmark_max_turns": 5,
        },
        "prompts": {},
    },
    **MEMORY_DEFAULTS,
    "validation": {
        "enabled": True,
        "mode": "true_replay",
        "runtimes": ["hermes", "deap", "agentshub", "langfuse", "doris"],
        "idle_after_seconds": 300,
        "poll_interval_seconds": 60,
        "max_jobs_per_day": 5,
        "max_concurrency": 1,
        "required_results": 3,
        "required_approvals": 2,
        "agentshub_url": "",
        "agentshub_api_key": "",
    },
    # Live source selection uses adapter only. type/options are migration-only.
    "datasource": {
        "adapter": "",
        "schedule": {
            "enabled": False,
            "time": "00:00",
            "timezone": "Asia/Shanghai",
            "window": "previous_day",
            "max_sessions": 1000,
        },
        "type": "langfuse",
        "adapters_dir": "",
        "options": {},
    },
    "langfuse": {
        "enabled": False,
        "host": "https://cloud.langfuse.com",
        "public_key": "",
        "secret_key": "",
        # Outbound observability is independent from inbound session pulls.
        # Tracing target is separate from the pull host/keys above.
        "tracing_enabled": False,
        "tracing_host": "",
        "tracing_public_key": "",
        "tracing_secret_key": "",
        "tracing_environment": "local",
        "tracing_release": "",
        "tracing_sample_rate": 1.0,
        "tracing_capture_content": True,
        "tracing_flush_at": 1,
        "tracing_flush_interval_seconds": 1.0,
        "timeout_seconds": 30,
        "page_limit": 50,
        "max_sessions": 100,
        # Default session-attribute filters. ``environment`` and ``tags``
        # accept multiple values (list or comma-separated string).
        "default_environment": [],
        "default_user_id": "",
        "default_tags": [],
        "default_release": "",
        "default_version": "",
        "default_trace_name": "",
        # Deprecated single trace mapper (migrated into ``mappers`` on first
        # read; see integrations.langfuse_mapper.normalize_mapper_entries).
        # NOTE: do NOT add a default ``mappers`` key here — defaults are
        # deep-merged under the user's YAML, so a default would make the
        # never-written-key check in the migration logic always see [].
        "mapper_enabled": False,
        "mapper_code": "",
    },
    # Semantic topic segmentation before value classification (see
    # session_ingestion/split.py): one LLM boundary call per session; no
    # turn-count/time-gap/size-cap heuristics.
    "session_split": {
        "enabled": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = {
        key: _deep_merge({}, value) if isinstance(value, dict) else value
        for key, value in base.items()
    }
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = _deep_merge({}, v) if isinstance(v, dict) else v
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
    """Resolve the sharing backend.

    ``local`` selects the built-in filesystem store (which may point at a NAS)
    and ``viking`` selects OpenViking. Unknown legacy values retain the
    historical OpenViking interpretation.
    """
    backend = (
        str(sharing.get("backend", "") or "")
        .strip()
        .lower()
        .replace("_", "-")
    )
    if backend in {
        "local",
        "localfs",
        "local-fs",
        "filesystem",
        "fs",
        "builtin",
        "built-in",
    }:
        return "local"
    if backend:
        return "viking"
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
    if sharing.get("enabled") or has_viking_key or sharing.get("viking_endpoint"):
        return "viking"
    return ""


def _normalize_viking_deployment(value: Any, default: str = "cloud") -> str:
    """Normalize the OpenViking deployment mode to ``cloud`` or ``local``."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"cloud", "local"} else default


def _normalize_validation_mode(value: Any) -> str:
    normalized = str(value or "true_replay").strip().lower().replace("-", "_")
    return normalized if normalized in {"replay", "true_replay"} else "true_replay"


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


def _normalize_extensions(value: Any) -> list[str]:
    extensions: list[str] = []
    for raw in _normalize_string_list(value):
        item = raw.lower().lstrip(".")
        if not item or "/" in item or "\\" in item:
            continue
        extension = f".{item}"
        if extension not in extensions:
            extensions.append(extension)
    return extensions or [".py", ".sh"]


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
