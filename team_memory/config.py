"""Memory configuration owned by this module."""

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TeamMemoryConfig:
    # ------------------------------------------------------------------ #
    # Team-memory maintenance                                             #
    # ------------------------------------------------------------------ #
    # Maintenance is a scheduled ``ov cp`` + ``ov compile`` run.
    memory_maintenance_enabled: bool = False
    memory_maintenance_auto_start: bool = False
    memory_maintenance_interval_seconds: int = 86400

    # ------------------------------------------------------------------ #
    # Cross-user memory aggregation (deterministic staging + ov compile)  #
    # ------------------------------------------------------------------ #
    aggregation_enabled: bool = False
    aggregation_shared_knowledge_prefix: str = "shared-knowledge"
    aggregation_okf_skill_uri: str = "viking://agent/skills/team-memory-okf"
    aggregation_maintenance_skill_uri: str = "viking://agent/skills/team-memory-maintenance"
    aggregation_staging_dir: str = "staging"
    aggregation_kinds: list[str] = field(default_factory=list)
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
    "memory": {
        "maintenance": {
            "enabled": False,
            "auto_start": False,
            "interval_seconds": 86400,
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
        # Scratch dir segment for deterministic per-user snapshots and
        # tree-reduce intermediates. Runtime resolves it below the merge
        # identity's private viking://user/<merge-user>/resources tree.
        "staging_dir": "staging",
        # Memory categories to aggregate (empty -> built-in default set).
        "kinds": [],
        # Large-account inventory is fetched in stable pages. The safety limit
        # prevents an accidental unbounded Account-wide run.
        "account_user_limit": 50_000,
        "account_user_page_size": 1_000,
        # deterministic snapshot copies run concurrently up to this
        # many at once.
        "phase1_concurrency": 6,
        # Tree-reduce fan-in width for merges. Each merge compile takes
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
        # When true, publishes in sequential batches directly onto the
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
    },
}

DEFAULT_MEMORY_MODULE_CONFIG: dict[str, Any] = {
    "maintenance": {
        "trigger_mode": "manual",
        "interval_seconds": 86400,
    },
    "aggregation": copy.deepcopy(MEMORY_DEFAULTS["memory"]["aggregation"]),
}


@dataclass(frozen=True)
class MemoryModuleConfig:
    revision: int
    config: dict[str, Any]

    @property
    def maintenance(self) -> dict[str, Any]:
        return self.config["maintenance"]

    @property
    def aggregation(self) -> dict[str, Any]:
        return self.config["aggregation"]


def resolve_memory_config(module) -> MemoryModuleConfig:
    """Merge nested Memory defaults without mutating the persisted JSON."""
    raw = module.config if isinstance(module.config, dict) else {}
    return MemoryModuleConfig(
        revision=module.revision,
        config={
            **copy.deepcopy(raw),
            "maintenance": {
                **DEFAULT_MEMORY_MODULE_CONFIG["maintenance"],
                **(raw.get("maintenance") if isinstance(raw.get("maintenance"), dict) else {}),
            },
            "aggregation": {
                **DEFAULT_MEMORY_MODULE_CONFIG["aggregation"],
                **(raw.get("aggregation") if isinstance(raw.get("aggregation"), dict) else {}),
            },
        },
    )


async def load_memory_config(config_repo, conn) -> MemoryModuleConfig:
    return resolve_memory_config(await config_repo.read_tenant_module(conn, "memory"))


def memory_config_values(data, _normalize_string_list):
    memory = data.get("memory", {}) if isinstance(data.get("memory"), dict) else {}
    maintenance = (
        memory.get("maintenance")
        if isinstance(memory.get("maintenance"), dict)
        else {}
    )
    aggregation = (
        memory.get("aggregation")
        if isinstance(memory.get("aggregation"), dict)
        else {}
    )
    return {
        "memory_maintenance_enabled": bool(maintenance.get("enabled", False)),
        "memory_maintenance_auto_start": bool(maintenance.get("auto_start", False)),
        "memory_maintenance_interval_seconds": max(
            60,
            int(maintenance.get("interval_seconds", 86400) or 86400),
        ),
        "aggregation_enabled": bool(aggregation.get("enabled", False)),
        "aggregation_shared_knowledge_prefix": str(
            aggregation.get("shared_knowledge_prefix", "") or "shared-knowledge"
        ),
        "aggregation_okf_skill_uri": str(
            aggregation.get("okf_skill_uri", "") or "viking://agent/skills/team-memory-okf"
        ),
        "aggregation_maintenance_skill_uri": str(
            aggregation.get("maintenance_skill_uri") or "viking://agent/skills/team-memory-maintenance"
        ),
        "aggregation_staging_dir": str(aggregation.get("staging_dir", "") or "staging"),
        "aggregation_kinds": _normalize_string_list(aggregation.get("kinds")),
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
