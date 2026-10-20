"""Immutable datasource adapter versions and tenant bindings.

Accepted pulls pin their adapter revision; editing a binding or source cannot
alter an in-flight task."""

from __future__ import annotations

import hashlib
from typing import Any

from teamEvolver.database.transaction import TransactionManager


def source_hash(source: str) -> str:
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()


class AdapterConflict(RuntimeError):
    """Raised when a concurrent save loses the revision race."""


class AdapterRepository:
    """Persistence for adapter definitions, versions and tenant bindings."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    # -- definition ------------------------------------------------------- #

    async def ensure_adapter(
        self, conn: Any, *, tenant_id: str, adapter_id: str, kind: str = "python"
    ) -> None:
        await conn.execute(
            f"-- te:op=adapter_ensure\n"
            f"INSERT INTO {self._schema}.adapters (tenant_id, adapter_id, kind) "
            "VALUES ($1, $2, $3) ON CONFLICT (tenant_id, adapter_id) DO NOTHING",
            tenant_id,
            adapter_id,
            kind,
        )

    # -- immutable versions ---------------------------------------------- #

    async def latest_revision(self, conn: Any, *, adapter_id: str) -> int:
        row = await conn.fetchval(
            f"-- te:op=adapter_latest_rev\n"
            f"SELECT max(revision) FROM {self._schema}.adapter_versions "
            "WHERE adapter_id = $1",
            adapter_id,
        )
        return int(row or 0)

    async def save_source(
        self, conn: Any, *, tenant_id: str, adapter_id: str, source: str, kind: str = "python"
    ) -> int:
        """Append a new immutable adapter version; returns the new revision.

        Idempotent on identical content: if the latest version already has the
        same ``source_hash`` the existing revision is returned (no new row), so a
        repeated save does not churn revisions.
        """
        await self.ensure_adapter(conn, tenant_id=tenant_id, adapter_id=adapter_id, kind=kind)
        digest = source_hash(source)
        current = await conn.fetchrow(
            f"-- te:op=adapter_head\n"
            f"SELECT revision, source_hash FROM {self._schema}.adapter_versions "
            "WHERE adapter_id = $1 ORDER BY revision DESC LIMIT 1",
            adapter_id,
        )
        if current is not None and str(current["source_hash"]) == digest:
            return int(current["revision"])
        next_revision = int(current["revision"]) + 1 if current is not None else 1
        inserted = await conn.fetchval(
            f"-- te:op=adapter_version_insert\n"
            f"INSERT INTO {self._schema}.adapter_versions "
            "(tenant_id, adapter_id, revision, source_hash, source) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (tenant_id, adapter_id, revision) DO NOTHING "
            "RETURNING revision",
            tenant_id,
            adapter_id,
            next_revision,
            digest,
            source,
        )
        if inserted is None:
            raise AdapterConflict(
                f"adapter {adapter_id!r} revision {next_revision} already exists (concurrent save)"
            )
        return int(inserted)

    async def get_version(
        self, conn: Any, *, adapter_id: str, revision: int
    ) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"-- te:op=adapter_version_get\n"
            f"SELECT adapter_id, revision, source_hash, source, created_at "
            f"FROM {self._schema}.adapter_versions WHERE adapter_id = $1 AND revision = $2",
            adapter_id,
            int(revision),
        )
        return dict(row) if row is not None else None

    # -- tenant binding --------------------------------------------------- #

    async def bind(
        self, conn: Any, *, tenant_id: str, binding_key: str, adapter_id: str, revision: int
    ) -> None:
        """Point a binding key at a specific immutable adapter revision."""
        await conn.execute(
            f"-- te:op=adapter_bind\n"
            f"INSERT INTO {self._schema}.adapter_bindings "
            "(tenant_id, binding_key, adapter_id, revision) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tenant_id, binding_key) DO UPDATE SET "
            "  adapter_id = EXCLUDED.adapter_id, revision = EXCLUDED.revision, "
            "  updated_at = now()",
            tenant_id,
            binding_key,
            adapter_id,
            int(revision),
        )

    async def binding(self, conn: Any, *, binding_key: str) -> dict[str, Any] | None:
        """Resolve the adapter + pinned revision for a binding key."""
        row = await conn.fetchrow(
            f"-- te:op=adapter_binding_get\n"
            f"SELECT binding_key, adapter_id, revision FROM {self._schema}.adapter_bindings "
            "WHERE binding_key = $1",
            binding_key,
        )
        return dict(row) if row is not None else None


__all__ = ["AdapterRepository", "AdapterConflict", "source_hash"]
