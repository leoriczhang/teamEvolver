"""TeamEvolver platform database layer.

This package is platform infrastructure: it owns the PostgreSQL connection
runtime, the transaction boundary, the RLS session context, and the versioned
schema runner. Per it MUST NOT import any business module
(``session_ingestion``, ``team_skills``, ``team_replay``, ``team_memory``,
``team_miner``).

Unlike the legacy ``teamEvolver.storage.pg_pool`` runtime, this layer never
creates tables on import or on first use. Schema changes are applied only
through the explicit ``teamEvolver schema apply`` command (see
:mod:`teamEvolver.database.schema_runner`).
"""

from __future__ import annotations

from .pool import (
    DEFAULT_SCHEMA,
    DatabasePool,
    build_pg_dsn,
    dsn_from_env,
    get_database_pool,
    validate_schema_name,
)
from .rls import TENANT_GUC, apply_connection_context, reset_tenant_guc
from .schema_runner import (
    SchemaMigration,
    SchemaRunner,
    discover_migrations,
)
from .transaction import TransactionManager

__all__ = [
    "DEFAULT_SCHEMA",
    "DatabasePool",
    "TransactionManager",
    "SchemaRunner",
    "SchemaMigration",
    "discover_migrations",
    "TENANT_GUC",
    "apply_connection_context",
    "reset_tenant_guc",
    "build_pg_dsn",
    "dsn_from_env",
    "get_database_pool",
    "validate_schema_name",
]
