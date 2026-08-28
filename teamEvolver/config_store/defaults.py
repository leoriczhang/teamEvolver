"""Defaults and normalization helpers for the teamEvolver config store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import TEAM_SKILL_ROOT_PREFIX

CONFIG_DIR = Path.home() / ".teamEvolver"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
_DEFAULT_SKILLS_DIR = CONFIG_DIR / "skills"
_DEFAULT_HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"
_FALLBACK_LLM_API_MODE = "chat"
_SKILL_RELOAD_MODES = {"off", "poll", "callback"}
_MIN_SKILL_RELOAD_INTERVAL_SECONDS = 5

_DEFAULTS: dict = {
    "team": {
        "display_name": "Team",
    },
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
        # Deployment target for OpenViking: ``cloud`` (Volcengine-hosted) or
        # ``local`` (self-hosted openviking-server). Drives the endpoint when
        # ``viking_endpoint`` is left empty.
        "viking_deployment": "cloud",
        "endpoint": "",
        "skill_backend": "",
        "session_backend": "",
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
    "dreamcycle": {
        "enabled": False,
        "auto_start": False,
        "daemon_command": "dreamcycle --daemon",
        "trigger_command": "dreamcycle --once",
        "viking_agent": "dreamcycle",
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
        "llm_max_tokens": 4096,
        "temperature": 0.3,
        "embed_model": "",
        "embed_base_url": "",
        "embed_api_key": "",
        "dedup_merge_threshold": 0.86,
        "dedup_warn_threshold": 0.72,
        "active_start_hour": 0,
        "active_end_hour": 6,
        "rounds_per_window": 3,
        "round_interval_minutes": 90,
        "max_turns_per_job": 25,
        "max_consecutive_errors": 3,
        "retry_delay_seconds": 300,
        "customer_id": "",
        "state_dir": "",
        "log_level": "INFO",
        "enabled_jobs": [
            "team_overview",
            "deduplication",
            "cleanup",
            "onboarding_check",
            "consolidate",
        ],
        "job_prompts": {},
        "job_settings": {},
        # Deprecated simplified-engine fields retained for migration.
        "interval_seconds": 86400,
        "max_source_items": 100,
        "max_source_chars": 120000,
        "prompts": {
            "extract": "",
            "consolidate": "",
        },
    },
    "aggregation": {
        # Deterministic staging + ov compile cross-user memory aggregation.
        "enabled": False,
        # Output lands under viking://resources/<prefix>/<kind>. resources is
        # account-shared and supports a full artifact tree, unlike a user's
        # memory root.
        "shared_knowledge_prefix": "shared-knowledge",
        # User-editable OKF Skill consumed by ov compile.
        "okf_skill_uri": "viking://agent/skills/team-memory-okf",
        "insight_skill_uri": "",
        # Compatibility field; the current runtime does not derive user keys.
        "key_seed": "teamevolver-aggregation",
        # Scratch dir segment for deterministic per-user snapshots and
        # tree-reduce intermediates. Runtime resolves it below the merge
        # identity's private viking://user/<merge-user>/resources tree.
        "staging_dir": "staging",
        # Memory categories to aggregate (empty -> built-in default set).
        "kinds": [],
        # Compatibility field retained for existing configuration files.
        "max_users_per_batch": 12,
        # Large-account inventory is fetched in stable pages. The safety limit
        # prevents an accidental unbounded Account-wide run.
        "account_user_limit": 50_000,
        "account_user_page_size": 1_000,
        # Phase 1 deterministic snapshot copies run concurrently up to this
        # many at once.
        "phase1_concurrency": 6,
        # Tree-reduce fan-in width for Phase 2 merges. Each merge compile takes
        # at most this many sources; groups of groups cascade until one root
        # remains. Kept < 16 to respect the ov compile source ceiling.
        "merge_fan_in": 4,
        "merge_concurrency": 4,
        # Above this user count, publish fixed hash partitions instead of
        # collapsing all team memory into one 128-page compile output.
        "partition_threshold": 512,
        "partition_count": 256,
        # When true, the final merge treats the current team-memory target as an
        # authoritative baseline source so manual edits are preserved and new
        # material is de-duplicated/merged on top instead of overwritten.
        "preserve_manual_edits": False,
        # When true, Phase 2 publishes in sequential batches directly onto the
        # target (relying on ov compile's upsert + target-checkout to merge onto
        # existing pages) instead of one whole-tree final rewrite. This removes
        # the 128-page directory ceiling: 128 only limits a single batch.
        "incremental_publish": False,
        # Users per publish batch when incremental_publish is on. Kept small so a
        # batch's compile output stays under the 128-page ceiling; oversized
        # batches are auto-bisected at runtime.
        "publish_batch_users": 8,
        # Keep live/status payloads bounded while retaining aggregate counters.
        "run_detail_limit": 2_000,
        "compile_runtime_timeout_seconds": 3000,
        "state_dir": "",
    },
    "validation": {
        "enabled": True,
        "mode": "true_replay",
        "idle_after_seconds": 300,
        "poll_interval_seconds": 60,
        "max_jobs_per_day": 5,
        "max_concurrency": 1,
        "required_results": 3,
        "required_approvals": 2,
        "agentshub_url": "",
        "agentshub_api_key": "",
    },
    "langfuse": {
        "enabled": False,
        "host": "https://cloud.langfuse.com",
        "public_key": "",
        "secret_key": "",
        # Outbound observability is independent from inbound session pulls.
        "tracing_enabled": False,
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
        # Operator-authored trace mapper. When ``mapper_enabled`` is true and
        # ``mapper_code`` defines ``map_trace(trace, observations)``, that
        # function produces the evolution turn (deep-merged over the built-in
        # mapping). Empty/disabled falls back to the built-in converter.
        "mapper_enabled": False,
        "mapper_code": "",
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

    Only ``viking`` (OpenViking) is supported. A legacy ``local`` value (or any
    other) collapses to ``viking``; the empty string is returned only when
    sharing is neither enabled nor carries any viking credential/endpoint.
    """
    backend = str(sharing.get("backend", "") or "").strip().lower()
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
