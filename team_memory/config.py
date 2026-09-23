"""Memory configuration owned by this module; old field names remain compatible."""

from dataclasses import dataclass, field


@dataclass
class TeamMemoryConfig:
    # ------------------------------------------------------------------ #
    # DreamCycle team-memory maintenance                                  #
    # ------------------------------------------------------------------ #
    # Full DreamCycle runs natively in teamEvolver: five maintenance jobs,
    # ReAct tool calls, policy enforcement, reports, scheduling and history.
    # Legacy command/two-prompt fields remain readable for config migration.
    dreamcycle_enabled: bool = False
    dreamcycle_auto_start: bool = False
    dreamcycle_daemon_command: str = "dreamcycle --daemon"
    dreamcycle_trigger_command: str = "dreamcycle --once"
    dreamcycle_viking_agent: str = "dreamcycle"
    dreamcycle_dedup_merge_threshold: float = 0.86
    dreamcycle_dedup_warn_threshold: float = 0.72
    dreamcycle_active_start_hour: int = 0
    dreamcycle_active_end_hour: int = 6
    dreamcycle_rounds_per_window: int = 3
    dreamcycle_round_interval_minutes: int = 90
    dreamcycle_max_turns_per_job: int = 25
    dreamcycle_max_consecutive_errors: int = 3
    dreamcycle_retry_delay_seconds: int = 300
    dreamcycle_customer_id: str = ""
    dreamcycle_state_dir: str = ""
    dreamcycle_log_level: str = "INFO"
    dreamcycle_enabled_jobs: list[str] = field(
        default_factory=lambda: [
            "team_overview",
            "deduplication",
            "cleanup",
            "onboarding_check",
            "consolidate",
        ]
    )
    dreamcycle_job_prompts: dict[str, str] = field(default_factory=dict)
    dreamcycle_job_settings: dict[str, dict] = field(default_factory=dict)
    # Deprecated simplified-engine compatibility.
    dreamcycle_interval_seconds: int = 86400
    dreamcycle_max_source_items: int = 100
    dreamcycle_max_source_chars: int = 120000
    dreamcycle_extract_prompt: str = ""
    dreamcycle_consolidate_prompt: str = ""

    # ------------------------------------------------------------------ #
    # Cross-user memory aggregation (deterministic staging + ov compile)  #
    # ------------------------------------------------------------------ #
    aggregation_enabled: bool = False
    aggregation_shared_knowledge_prefix: str = "shared-knowledge"
    aggregation_okf_skill_uri: str = "viking://agent/skills/team-memory-okf"
    aggregation_maintenance_skill_uri: str = "viking://agent/skills/team-memory-maintenance"
    aggregation_insight_skill_uri: str = ""
    aggregation_key_seed: str = "teamevolver-aggregation"
    aggregation_staging_dir: str = "staging"
    aggregation_kinds: list[str] = field(default_factory=list)
    aggregation_max_users_per_batch: int = 12
    aggregation_account_user_limit: int = 50_000
    aggregation_account_user_page_size: int = 1_000
    aggregation_phase1_concurrency: int = 6
    aggregation_merge_fan_in: int = 4
    aggregation_merge_concurrency: int = 4
    aggregation_partition_threshold: int = 512
    aggregation_partition_count: int = 256
    aggregation_preserve_manual_edits: bool = False
    aggregation_incremental_publish: bool = False
    aggregation_publish_batch_users: int = 8
    aggregation_run_detail_limit: int = 2_000
    aggregation_compile_runtime_timeout_seconds: int = 3000
    aggregation_merge_group_max_attempts: int = 2
    aggregation_state_dir: str = ""


MEMORY_DEFAULTS = {
    "dreamcycle": {
        "enabled": False,
        "auto_start": False,
        "daemon_command": "dreamcycle --daemon",
        "trigger_command": "dreamcycle --once",
        "viking_agent": "dreamcycle",
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
        "maintenance_skill_uri": "viking://agent/skills/team-memory-maintenance",
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
}


def memory_config_values(data, _normalize_string_list):
    dreamcycle = data.get("dreamcycle", {})
    aggregation = data.get("aggregation", {}) if isinstance(data.get("aggregation"), dict) else {}
    dreamcycle_prompts = dreamcycle.get("prompts") if isinstance(dreamcycle.get("prompts"), dict) else {}
    dreamcycle_job_prompts = dreamcycle.get("job_prompts") if isinstance(dreamcycle.get("job_prompts"), dict) else {}
    dreamcycle_job_settings = dreamcycle.get("job_settings") if isinstance(dreamcycle.get("job_settings"), dict) else {}
    return {
        "dreamcycle_enabled": bool(dreamcycle.get("enabled", False)),
        "dreamcycle_auto_start": bool(dreamcycle.get("auto_start", False)),
        "dreamcycle_daemon_command": str(dreamcycle.get("daemon_command", "") or "dreamcycle --daemon"),
        "dreamcycle_trigger_command": str(dreamcycle.get("trigger_command", "") or "dreamcycle --once"),
        "dreamcycle_viking_agent": str(dreamcycle.get("viking_agent", "") or "dreamcycle"),
        "dreamcycle_dedup_merge_threshold": max(
            -1.0,
            min(
                1.0,
                float(
                    dreamcycle.get("dedup_merge_threshold", 0.86)
                    if dreamcycle.get("dedup_merge_threshold") is not None
                    else 0.86
                ),
            ),
        ),
        "dreamcycle_dedup_warn_threshold": max(
            -1.0,
            min(
                1.0,
                float(
                    dreamcycle.get("dedup_warn_threshold", 0.72)
                    if dreamcycle.get("dedup_warn_threshold") is not None
                    else 0.72
                ),
            ),
        ),
        "dreamcycle_active_start_hour": max(0, min(23, int(dreamcycle.get("active_start_hour", 0) or 0))),
        "dreamcycle_active_end_hour": max(
            0,
            min(23, int(dreamcycle.get("active_end_hour", 6) if dreamcycle.get("active_end_hour") is not None else 6)),
        ),
        "dreamcycle_rounds_per_window": max(1, int(dreamcycle.get("rounds_per_window", 3) or 3)),
        "dreamcycle_round_interval_minutes": max(1, int(dreamcycle.get("round_interval_minutes", 90) or 90)),
        "dreamcycle_max_turns_per_job": max(1, int(dreamcycle.get("max_turns_per_job", 25) or 25)),
        "dreamcycle_max_consecutive_errors": max(1, int(dreamcycle.get("max_consecutive_errors", 3) or 3)),
        "dreamcycle_retry_delay_seconds": max(1, int(dreamcycle.get("retry_delay_seconds", 300) or 300)),
        "dreamcycle_customer_id": str(dreamcycle.get("customer_id") or dreamcycle.get("peer_id") or ""),
        "dreamcycle_state_dir": str(dreamcycle.get("state_dir", "") or ""),
        "dreamcycle_log_level": str(dreamcycle.get("log_level", "") or "INFO"),
        "dreamcycle_enabled_jobs": _normalize_string_list(dreamcycle.get("enabled_jobs"))
        if "enabled_jobs" in dreamcycle
        else ["team_overview", "deduplication", "cleanup", "onboarding_check", "consolidate"],
        "dreamcycle_job_prompts": {
            str(key): str(value)
            for key, value in dreamcycle_job_prompts.items()
            if str(key).strip() and str(value).strip()
        },
        "dreamcycle_job_settings": {
            str(key): dict(value)
            for key, value in dreamcycle_job_settings.items()
            if str(key).strip() and isinstance(value, dict)
        },
        "dreamcycle_interval_seconds": max(60, int(dreamcycle.get("interval_seconds", 86400) or 86400)),
        "dreamcycle_max_source_items": max(1, int(dreamcycle.get("max_source_items", 100) or 100)),
        "dreamcycle_max_source_chars": max(1000, int(dreamcycle.get("max_source_chars", 120000) or 120000)),
        "dreamcycle_extract_prompt": str(dreamcycle_prompts.get("extract", "")),
        "dreamcycle_consolidate_prompt": str(dreamcycle_prompts.get("consolidate", "")),
        "aggregation_enabled": bool(aggregation.get("enabled", False)),
        "aggregation_shared_knowledge_prefix": str(
            aggregation.get("shared_knowledge_prefix", "") or "shared-knowledge"
        ),
        "aggregation_okf_skill_uri": str(
            aggregation.get("okf_skill_uri", "") or "viking://agent/skills/team-memory-okf"
        ),
        "aggregation_insight_skill_uri": str(aggregation.get("insight_skill_uri", "") or ""),
        "aggregation_key_seed": str(aggregation.get("key_seed", "") or "teamevolver-aggregation"),
        "aggregation_maintenance_skill_uri": str(
            aggregation.get("maintenance_skill_uri") or "viking://agent/skills/team-memory-maintenance"
        ),
        "aggregation_staging_dir": str(aggregation.get("staging_dir", "") or "staging"),
        "aggregation_kinds": _normalize_string_list(aggregation.get("kinds")),
        "aggregation_max_users_per_batch": max(1, int(aggregation.get("max_users_per_batch", 12) or 12)),
        "aggregation_account_user_limit": max(1, int(aggregation.get("account_user_limit", 50000) or 50000)),
        "aggregation_account_user_page_size": max(
            1, min(1000, int(aggregation.get("account_user_page_size", 1000) or 1000))
        ),
        "aggregation_phase1_concurrency": max(1, int(aggregation.get("phase1_concurrency", 6) or 6)),
        "aggregation_merge_fan_in": max(2, min(15, int(aggregation.get("merge_fan_in", 4) or 4))),
        "aggregation_merge_concurrency": max(1, int(aggregation.get("merge_concurrency", 4) or 4)),
        "aggregation_partition_threshold": max(16, int(aggregation.get("partition_threshold", 512) or 512)),
        "aggregation_partition_count": max(16, min(1024, int(aggregation.get("partition_count", 256) or 256))),
        "aggregation_preserve_manual_edits": bool(aggregation.get("preserve_manual_edits", False)),
        "aggregation_incremental_publish": bool(aggregation.get("incremental_publish", False)),
        "aggregation_publish_batch_users": max(1, int(aggregation.get("publish_batch_users", 8) or 8)),
        "aggregation_run_detail_limit": max(100, int(aggregation.get("run_detail_limit", 2000) or 2000)),
        "aggregation_compile_runtime_timeout_seconds": max(
            60, int(aggregation.get("compile_runtime_timeout_seconds", 3000) or 3000)
        ),
        "aggregation_merge_group_max_attempts": max(
            1, int(aggregation.get("merge_group_max_attempts", 2) or 2)
        ),
        "aggregation_state_dir": str(aggregation.get("state_dir", "") or ""),
    }
