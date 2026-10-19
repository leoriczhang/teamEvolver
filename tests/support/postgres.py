"""Isolated PostgreSQL schemas and tenant transaction helpers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import pytest

from team_skills.config import DEFAULT_SKILLS_CONFIG
from teamEvolver.config_runtime import TENANT_CONFIG_MODULES, ConfigRepository
from teamEvolver.database import SchemaRunner, get_database_pool
from teamEvolver.database.transaction import TransactionManager

DSN = os.environ.get("TEAMEVOLVER_TEST_PG_DSN", "")


@dataclass
class _Env:
    tx: TransactionManager
    schema: str


@pytest.fixture
def pg_env():
    if not DSN:
        pytest.skip("TEAMEVOLVER_TEST_PG_DSN requires an isolated PostgreSQL database")
    schema = f"te_test_{os.getpid()}_{time.time_ns()}"
    pool = get_database_pool(dsn=DSN, schema=schema, ssl="disable")

    async def _setup():
        async with pool.admin_conn() as conn:
            await conn.execute(f"CREATE SCHEMA {schema}")
            await SchemaRunner(schema=schema).apply(conn)
            for tenant in ("alpha", "beta"):
                await conn.execute(f"INSERT INTO {schema}.tenants (tenant_id, display_name) VALUES ($1, $1)", tenant)
        tx = TransactionManager(pool)
        repo = ConfigRepository(tx)
        defaults = {m: {} for m in TENANT_CONFIG_MODULES}
        defaults["skills"] = dict(DEFAULT_SKILLS_CONFIG)
        for tenant in ("default", "alpha", "beta"):
            async with tx.tenant_tx(tenant) as conn:
                await repo.create_tenant_defaults(conn, tenant_id=tenant, defaults=defaults)

    pool.run(_setup())
    try:
        yield _Env(tx=TransactionManager(pool), schema=schema)
    finally:

        async def _drop():
            async with pool.admin_conn() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

        pool.run(_drop())
        pool.close()


def _run(env: _Env, tenant: str, body):
    return env.tx.run_tenant_tx(tenant, body, timeout=30)
