"""real PG: an empty database and a new tenant have every module config row."""

import os
import time

import pytest

from teamEvolver.config_runtime import TENANT_CONFIG_MODULES, ConfigRepository
from teamEvolver.database import SchemaRunner, get_database_pool
from teamEvolver.database.transaction import TransactionManager
from teamEvolver.tenants.registry import TenantRegistry

DSN = os.environ.get("TEAMEVOLVER_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="requires PostgreSQL")


@pytest.fixture
def empty_schema():
    schema = f"te_p9_{os.getpid()}_{time.time_ns() % 10**9}"
    pool = get_database_pool(dsn=DSN, schema=schema, ssl="disable")

    async def setup():
        async with pool.admin_conn() as conn:
            await conn.execute(f"CREATE SCHEMA {schema}")
            await SchemaRunner(schema=schema).apply(conn)

    pool.run(setup())
    try:
        yield pool, TransactionManager(pool)
    finally:

        async def drop():
            async with pool.admin_conn() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

        pool.run(drop())
        pool.close()


def _modules(tx, tenant):
    async def body(conn):
        repo = ConfigRepository(tx)
        return {m: (await repo.read_tenant_module(conn, m)).revision for m in TENANT_CONFIG_MODULES}

    return tx.run_tenant_tx(tenant, body, timeout=30)


@pytest.mark.postgres
def test_schema_apply_creates_default_tenant_module_rows(empty_schema):
    _, tx = empty_schema
    assert _modules(tx, "default") == {m: 1 for m in TENANT_CONFIG_MODULES}


@pytest.mark.postgres
def test_created_tenant_has_every_module_row_and_is_isolated(empty_schema):
    pool, tx = empty_schema
    registry = TenantRegistry(runtime=pool)
    ctx, _token = registry.create_tenant("tenant", "new-tenant")
    assert _modules(tx, ctx.tenant_id) == {m: 1 for m in TENANT_CONFIG_MODULES}

    async def visible(conn):
        return await conn.fetchval(f"SELECT count(*) FROM {pool.schema}.tenant_configs")

    assert tx.run_tenant_tx("default", visible, timeout=30) == len(TENANT_CONFIG_MODULES)
