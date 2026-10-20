"""Versioned schema runner.

Applies ``teamEvolver/database/schema/NNN_*.sql`` files in version order through
an explicit ``teamEvolver schema apply`` command. Each file is applied once,
inside its own transaction, and recorded in ``schema_versions`` with its
SHA-256 checksum and apply time. Re-running is idempotent: already-applied
versions are skipped, and a checksum mismatch on a recorded version aborts
(a committed migration must never be edited in place).

The runner never creates tables implicitly on import or app start; it is the
only sanctioned path for schema change.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
_FILENAME_RE = re.compile(r"^(\d+)_[a-z0-9_]+\.sql$")

# schema_versions is the runner's own ledger. It is bootstrapped before any
# migration so the very first apply can record itself.
_SCHEMA_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.schema_versions (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations(schema_dir: Path = SCHEMA_DIR) -> list[SchemaMigration]:
    """Load and version-sort every ``NNN_name.sql`` under *schema_dir*."""
    migrations: list[SchemaMigration] = []
    seen: set[int] = set()
    for path in sorted(schema_dir.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise ValueError(f"invalid schema filename (expect NNN_name.sql): {path.name}")
        version = int(m.group(1))
        if version in seen:
            raise ValueError(f"duplicate schema version {version}: {path.name}")
        seen.add(version)
        name = path.stem.split("_", 1)[1]
        migrations.append(
            SchemaMigration(version=version, name=name, path=path, sql=path.read_text(encoding="utf-8"))
        )
    migrations.sort(key=lambda mig: mig.version)
    return migrations


class SchemaRunner:
    """Apply pending migrations and record them in ``schema_versions``."""

    def __init__(self, *, schema: str, schema_dir: Path = SCHEMA_DIR) -> None:
        self._schema = schema
        self._schema_dir = schema_dir

    async def _ensure_ledger(self, conn: Any) -> None:
        await conn.execute(_SCHEMA_VERSIONS_DDL.format(schema=self._schema))

    async def _applied(self, conn: Any) -> dict[int, str]:
        rows = await conn.fetch(
            f"SELECT version, checksum FROM {self._schema}.schema_versions ORDER BY version"
        )
        return {int(r["version"]): str(r["checksum"]) for r in rows}

    async def apply(self, conn: Any, *, dry_run: bool = False) -> dict[str, Any]:
        """Apply all pending migrations on *conn* (a platform/admin connection).

        Returns a summary: ``{"applied": [...], "skipped": [...], "pending": [...]}``.
        Aborts (ValueError) if a recorded version's checksum differs from the
        on-disk file — committed migrations are immutable.
        """
        migrations = discover_migrations(self._schema_dir)
        await self._ensure_ledger(conn)
        applied = await self._applied(conn)

        result: dict[str, Any] = {"applied": [], "skipped": [], "pending": []}
        for mig in migrations:
            recorded = applied.get(mig.version)
            if recorded is not None:
                if recorded != mig.checksum:
                    raise ValueError(
                        f"schema version {mig.version} ({mig.name}) checksum mismatch: "
                        f"recorded {recorded[:12]}…, file {mig.checksum[:12]}…; "
                        "committed migrations must not be edited"
                    )
                result["skipped"].append(mig.version)
                continue
            if dry_run:
                result["pending"].append(mig.version)
                continue
            async with conn.transaction():
                await conn.execute(mig.sql.replace("{schema}", self._schema))
                await conn.execute(
                    f"INSERT INTO {self._schema}.schema_versions (version, name, checksum) "
                    "VALUES ($1, $2, $3)",
                    mig.version,
                    mig.name,
                    mig.checksum,
                )
            result["applied"].append(mig.version)
        return result
