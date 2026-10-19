"""PostgreSQL global and tenant module configuration.

Writes use revision compare-and-swap. Tenant creation writes the complete module
set; missing module rows fail closed. Effective configuration is frozen when a
long-running job is accepted so later changes cannot alter that job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .database.transaction import TransactionManager

# Fixed module set written on tenant creation.
TENANT_CONFIG_MODULES = ("session", "skills", "replay", "memory", "miner")


class ConfigRevisionConflict(RuntimeError):
    """Raised when a CAS update loses the race (``expected_revision`` stale)."""


class TenantConfigMissing(RuntimeError):
    """Raised when a required ``(tenant_id, module)`` config row is absent.

    Fail closed: never fall back to legacy YAML or local files.
    """


@dataclass(frozen=True)
class GlobalConfig:
    revision: int
    config: dict[str, Any]


@dataclass(frozen=True)
class TenantModuleConfig:
    tenant_id: str
    module: str
    revision: int
    config: dict[str, Any]


def _loads(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value  # asyncpg may already decode jsonb
    return json.loads(value)


class ConfigRepository:
    """Reads/writes the global and per-tenant config tables with CAS."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    # -- global single-row config ---------------------------------------- #

    async def read_global(self, conn: Any) -> GlobalConfig:
        row = await conn.fetchrow(
            f"SELECT revision, config FROM {self._schema}.teamevolver_config WHERE id = TRUE"
        )
        if row is None:
            raise TenantConfigMissing("teamevolver_config single row is missing; run schema apply")
        return GlobalConfig(revision=int(row["revision"]), config=_loads(row["config"]))

    async def write_global(self, conn: Any, *, expected_revision: int, config: dict[str, Any]) -> int:
        """CAS update: bump revision only if ``expected_revision`` still current."""
        new_revision = int(expected_revision) + 1
        updated = await conn.fetchval(
            f"UPDATE {self._schema}.teamevolver_config "
            "SET config = $1::jsonb, revision = $2, updated_at = now() "
            "WHERE id = TRUE AND revision = $3 RETURNING revision",
            json.dumps(config),
            new_revision,
            int(expected_revision),
        )
        if updated is None:
            raise ConfigRevisionConflict(
                f"global config revision {expected_revision} is stale"
            )
        return int(updated)

    # -- per-tenant module config ---------------------------------------- #

    async def read_tenant_module(self, conn: Any, module: str) -> TenantModuleConfig:
        row = await conn.fetchrow(
            f"SELECT tenant_id, module, revision, config FROM {self._schema}.tenant_configs "
            "WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '') AND module = $1",
            module,
        )
        if row is None:
            raise TenantConfigMissing(f"tenant config for module {module!r} is missing")
        return TenantModuleConfig(
            tenant_id=str(row["tenant_id"]),
            module=str(row["module"]),
            revision=int(row["revision"]),
            config=_loads(row["config"]),
        )

    async def write_tenant_module(
        self, conn: Any, *, module: str, expected_revision: int, config: dict[str, Any]
    ) -> int:
        new_revision = int(expected_revision) + 1
        updated = await conn.fetchval(
            f"UPDATE {self._schema}.tenant_configs "
            "SET config = $1::jsonb, revision = $2, updated_at = now() "
            "WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '') "
            "AND module = $3 AND revision = $4 RETURNING revision",
            json.dumps(config),
            new_revision,
            module,
            int(expected_revision),
        )
        if updated is None:
            raise ConfigRevisionConflict(
                f"tenant config {module!r} revision {expected_revision} is stale"
            )
        return int(updated)

    async def create_tenant_defaults(
        self, conn: Any, *, tenant_id: str, defaults: dict[str, dict[str, Any]]
    ) -> None:
        """Write all five module default rows for a new tenant in one call.

        Caller provides the ``defaults`` map (module -> config). Every module in
      :data:`TENANT_CONFIG_MODULES` must be present; a missing module is a
        programming error, not a silent partial create.
        """
        missing = [m for m in TENANT_CONFIG_MODULES if m not in defaults]
        if missing:
            raise ValueError(f"tenant defaults missing modules: {missing}")
        for module in TENANT_CONFIG_MODULES:
            await conn.execute(
                f"INSERT INTO {self._schema}.tenant_configs (tenant_id, module, revision, config) "
                "VALUES ($1, $2, 1, $3::jsonb) "
                "ON CONFLICT (tenant_id, module) DO NOTHING",
                tenant_id,
                module,
                json.dumps(defaults[module]),
            )


def freeze_effective_config(
    *, global_config: dict[str, Any], module_configs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build the immutable effective-config payload pinned into a worker task.

    A long task fixes its ``config_revision`` and carries the complete effective
    config ; the payload is a plain, sorted-key dict so its
    hash is stable.
    """
    return {
        "global": global_config,
        "modules": {m: module_configs.get(m, {}) for m in TENANT_CONFIG_MODULES},
    }
