"""Session Ingestion config owned by this module.

Reads the ``session`` slice of ``tenant_configs`` through the platform
:class:`~teamEvolver.config_runtime.ConfigRepository`. A missing row fails closed
(``TenantConfigMissing``) — there is no fallback to legacy YAML or local files
.

The slice is a plain dict; this module only names the keys the ingestion link
reads and supplies the module default used when a tenant is created. It performs
no I/O of its own beyond delegating to the platform ConfigRepository.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MODULE = "session"

# Default ``session`` module config row written when a tenant is created
# (teamEvolver/config_runtime.create_tenant_defaults). Pull concurrency and the
# Langfuse retry policy are tunable per tenant; the daily schedule is disabled by
# default (same as the baseline DEFAULT_SCHEDULE).
DEFAULT_SESSION_CONFIG: dict[str, Any] = {
    "pull_concurrency": 8,
    "pull_batch_size": 100,
    "pull_result_limit": 200,
    "langfuse_max_retries": 3,
    "datasource_adapter": "",
    "datasource_schedule": {
        "enabled": False,
        "time": "00:00",
        "timezone": "Asia/Shanghai",
        "window": "previous_day",
    },
}


@dataclass(frozen=True)
class SessionModuleConfig:
    """The resolved ``session`` module config for the bound tenant."""

    revision: int
    config: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, DEFAULT_SESSION_CONFIG.get(key, default))

    @property
    def pull_concurrency(self) -> int:
        return int(self.get("pull_concurrency", 8))

    @property
    def pull_batch_size(self) -> int:
        return int(self.get("pull_batch_size", 100))

    @property
    def pull_result_limit(self) -> int:
        return int(self.get("pull_result_limit", 200))

    @property
    def langfuse_max_retries(self) -> int:
        return int(self.get("langfuse_max_retries", 3))

    @property
    def datasource_adapter(self) -> str:
        return str(self.get("datasource_adapter", "") or "")


async def load_session_config(config_repo, conn: Any) -> SessionModuleConfig:
    """Read the bound tenant's ``session`` module config (fail-closed).

    ``config_repo`` is a :class:`~teamEvolver.config_runtime.ConfigRepository`;
    ``conn`` is a tenant-scoped connection (RLS already set). Raises
    ``TenantConfigMissing`` when the row is absent — no silent fallback.
    """
    module_config = await config_repo.read_tenant_module(conn, MODULE)
    return SessionModuleConfig(revision=module_config.revision, config=dict(module_config.config or {}))


__all__ = [
    "MODULE",
    "DEFAULT_SESSION_CONFIG",
    "SessionModuleConfig",
    "load_session_config",
]
