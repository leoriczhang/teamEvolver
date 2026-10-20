"""Team Skills config owned by this module.

Reads the ``skills`` slice of ``tenant_configs`` through the platform
:class:`~teamEvolver.config_runtime.ConfigRepository`. A missing row fails closed
(``TenantConfigMissing``) — there is no fallback to legacy YAML, ``config_kv`` or
local files.

The slice is a plain dict; this module only names the keys the Skill library
reads and supplies the module default written when a tenant is created. It
performs no I/O of its own beyond delegating to the platform ConfigRepository.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MODULE = "skills"

# Default ``skills`` module config row written when a tenant is created
# (teamEvolver/config_runtime.create_tenant_defaults). Bundle limits bound every
# publish path (console edit, candidate publish, rollback is a copy and is
# already bounded) so a single request cannot bloat the PG blob store.
DEFAULT_SKILLS_CONFIG: dict[str, Any] = {
    "max_bundle_files": 1024,
    "max_bundle_bytes": 64 * 1024 * 1024,
    "max_file_bytes": 16 * 1024 * 1024,
    "runtime_cache_enabled": True,
    "evolution_max_sessions": 100,
    "evolution_parallel_groups": 4,
    "min_group_sessions": 2,
    "min_group_users": 2,
    "evolution_trigger_mode": "manual",
    "evolution_interval_seconds": 600,
    "auto_evaluate": True,
    "publish_mode": "manual",
    "bundle_text_extensions": [".py", ".sh", ".md"],
    "bundle_edit_max_bytes": 262144,
    "bundle_prompt_max_bytes": 786432,
    "bundle_allow_delete": True,
    "bundle_static_checks_enabled": True,
    "experience_sync": {"enabled": False, "interval_seconds": 30},
}


@dataclass(frozen=True)
class BundleLimits:
    """Upper bounds a published Skill bundle must respect."""

    max_files: int = DEFAULT_SKILLS_CONFIG["max_bundle_files"]
    max_bytes: int = DEFAULT_SKILLS_CONFIG["max_bundle_bytes"]
    max_file_bytes: int = DEFAULT_SKILLS_CONFIG["max_file_bytes"]


@dataclass(frozen=True)
class SkillsModuleConfig:
    """The resolved ``skills`` module config for the bound tenant."""

    revision: int
    config: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, DEFAULT_SKILLS_CONFIG.get(key, default))

    @property
    def bundle_limits(self) -> BundleLimits:
        return BundleLimits(
            max_files=max(1, int(self.get("max_bundle_files"))),
            max_bytes=max(1, int(self.get("max_bundle_bytes"))),
            max_file_bytes=max(1, int(self.get("max_file_bytes"))),
        )

    @property
    def runtime_cache_enabled(self) -> bool:
        return bool(self.get("runtime_cache_enabled"))


async def load_skills_config(config_repo, conn: Any) -> SkillsModuleConfig:
    """Read the bound tenant's ``skills`` module config (fail-closed).

    ``config_repo`` is a :class:`~teamEvolver.config_runtime.ConfigRepository`;
    ``conn`` is a tenant-scoped connection (RLS already set). Raises
    ``TenantConfigMissing`` when the row is absent — no silent fallback.
    """
    module_config = await config_repo.read_tenant_module(conn, MODULE)
    return SkillsModuleConfig(
        revision=module_config.revision, config={**DEFAULT_SKILLS_CONFIG, **(module_config.config or {})},
    )


__all__ = [
    "MODULE",
    "DEFAULT_SKILLS_CONFIG",
    "BundleLimits",
    "SkillsModuleConfig",
    "load_skills_config",
]
