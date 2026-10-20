"""Versioned schema application, checksums and tenant transaction context."""

from __future__ import annotations

import re

import pytest

from teamEvolver.database import (
    DEFAULT_SCHEMA,
    SchemaRunner,
    apply_connection_context,
    discover_migrations,
)
from teamEvolver.database.rls import TENANT_GUC, reset_tenant_guc


class FakeTransaction:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn

    async def __aenter__(self):
        self._conn.tx_depth += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.tx_depth -= 1
        if exc_type is not None:
            self._conn.rolled_back += 1
        return False


class FakeConn:
    """Models just enough asyncpg surface for the schema runner + RLS GUC."""

    def __init__(self, catalog: dict) -> None:
        self.catalog = catalog  # shared across connections (one "database")
        self.settings: dict[str, str] = {"app.tenant_id": "", "search_path": ""}
        self.tx_depth = 0
        self.rolled_back = 0
        self.executed: list[str] = []

    def transaction(self):
        return FakeTransaction(self)

    async def execute(self, sql: str, *args):
        self.executed.append(sql)
        set_cfg = re.match(r"SELECT set_config\('([^']+)', \$1, false\)", sql)
        if set_cfg:
            self.settings[set_cfg.group(1)] = str(args[0]) if args else ""
            return "SELECT 1"
        set_cfg2 = re.match(r"SELECT set_config\('([^']+)', '([^']*)', false\)", sql)
        if set_cfg2:
            self.settings[set_cfg2.group(1)] = set_cfg2.group(2)
            return "SELECT 1"
        if "CREATE TABLE IF NOT EXISTS" in sql:
            for name in re.findall(r"CREATE TABLE IF NOT EXISTS\s+[\w]+\.(\w+)", sql):
                self.catalog.setdefault("tables", set()).add(name)
            return "CREATE TABLE"
        if sql.strip().startswith("INSERT INTO") and "schema_versions" in sql:
            self.catalog.setdefault("versions", {})[args[0]] = {"name": args[1], "checksum": args[2]}
            return "INSERT 0 1"
        return "OK"

    async def fetch(self, sql: str, *args):
        if "schema_versions" in sql and sql.strip().startswith("SELECT"):
            versions = self.catalog.get("versions", {})
            return [{"version": v, "checksum": d["checksum"]} for v, d in sorted(versions.items())]
        return []

    async def fetchval(self, sql: str, *args):
        return 1

    async def fetchrow(self, sql: str, *args):
        if "pg_roles" in sql:
            return {"rolsuper": False, "rolbypassrls": False}
        return None


@pytest.mark.asyncio
async def test_schema_runner_applies_once_and_is_idempotent():
    catalog: dict = {}
    conn = FakeConn(catalog)
    runner = SchemaRunner(schema=DEFAULT_SCHEMA)

    first = await runner.apply(conn)
    all_versions = [m.version for m in discover_migrations()]
    assert first["applied"] == all_versions
    assert "worker_tasks" in catalog["tables"]
    assert "teamevolver_config" in catalog["tables"]
    assert "schema_versions" in catalog["tables"]

    # Re-apply on the same catalogue: nothing new, versions are skipped.
    second = await runner.apply(FakeConn(catalog))
    assert second["applied"] == []
    assert second["skipped"] == all_versions


@pytest.mark.asyncio
async def test_schema_runner_dry_run_reports_pending_without_applying():
    catalog: dict = {}
    conn = FakeConn(catalog)
    runner = SchemaRunner(schema=DEFAULT_SCHEMA)
    result = await runner.apply(conn, dry_run=True)
    all_versions = [m.version for m in discover_migrations()]
    assert result["pending"] == all_versions
    # Dry run must not record a version.
    assert catalog.get("versions", {}) == {}


@pytest.mark.asyncio
async def test_schema_runner_rejects_checksum_drift_on_recorded_version():
    catalog: dict = {}
    runner = SchemaRunner(schema=DEFAULT_SCHEMA)
    await runner.apply(FakeConn(catalog))
    # Tamper with the recorded checksum to simulate an edited committed migration.
    catalog["versions"][1]["checksum"] = "deadbeef"
    with pytest.raises(ValueError, match="checksum mismatch"):
        await runner.apply(FakeConn(catalog))


@pytest.mark.asyncio
async def test_connection_context_sets_search_path_and_tenant_guc():
    conn = FakeConn({})
    await apply_connection_context(conn, schema="teamevolver", tenant_id="tenant-a")
    assert conn.settings["search_path"] == "teamevolver"
    assert conn.settings[TENANT_GUC] == "tenant-a"
    await reset_tenant_guc(conn)
    assert conn.settings[TENANT_GUC] == ""


def test_every_migration_filename_is_versioned_and_unique():
    migrations = discover_migrations()
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1
