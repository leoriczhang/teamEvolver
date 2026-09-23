"""Schema inheritance and real least-privilege PostgreSQL startup/regression."""

import os
import secrets
from urllib.parse import quote

import asyncpg
import pytest
import pytest_asyncio

from team_ontology.control import ControlStore

ADMIN_DSN = os.environ.get("ONTOLOGY_SCHEMA_TEST_ADMIN_DSN")


def test_default_schema_is_shared_te_schema():
    store = ControlStore("postgresql://unused")
    assert store.schema == "teamevolver"
    assert store.jobs == '"teamevolver".ontology_jobs'


@pytest.mark.parametrize("schema", ["x;drop schema public", "x.y", 'x"', "", "a" * 64])
def test_schema_identifiers_rejected_before_connect(schema):
    with pytest.raises(ValueError):
        ControlStore("postgresql://unused", schema=schema)


@pytest_asyncio.fixture
async def restricted_database():
    if not ADMIN_DSN:
        pytest.skip("requires isolated PG admin DSN (CREATEDB and CREATEROLE)")
    name = "ont_schema_" + secrets.token_hex(6)
    role = "ont_role_" + secrets.token_hex(6)
    admin = await asyncpg.connect(ADMIN_DSN)
    owner = None
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN')
        await admin.execute(f'CREATE DATABASE "{name}"')
        settings = await admin.fetchrow("SELECT current_user AS owner, current_setting('port') AS port")
        # Reuse the admin DSN host/auth configuration while selecting an isolated
        # database. Tests use a trusted local Unix socket, not enterprise PG.
        owner = await asyncpg.connect(ADMIN_DSN, database=name)
        await owner.execute(f'REVOKE CREATE ON DATABASE "{name}" FROM PUBLIC')
        await owner.execute("CREATE SCHEMA teamevolver")
        await owner.execute("CREATE SCHEMA custom_te")
        await owner.execute("CREATE SCHEMA te_ontology")
        for schema in ("teamevolver", "custom_te", "te_ontology"):
            await owner.execute(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO "{role}"')
        # Tests use connection settings explicitly; never change the application
        # role's database CREATE privilege to make the test pass.
        socket_dir = await admin.fetchval("SHOW unix_socket_directories")
        dsn = (
            f"postgresql://{role}@/{name}?host={quote(socket_dir.split(',')[0].strip(), safe='')}"
            f"&port={settings['port']}"
        )
        check = await asyncpg.connect(dsn)
        try:
            assert not await check.fetchval("SELECT has_database_privilege(current_user,current_database(),'CREATE')")
        finally:
            await check.close()
        yield owner, dsn
    finally:
        if owner:
            await owner.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", ["teamevolver", "custom_te"])
async def test_start_and_queue_in_existing_schema_without_database_create(restricted_database, schema):
    owner, dsn = restricted_database
    # A non-ontology table with the generic old name must remain untouched.
    await owner.execute(f'CREATE TABLE "{schema}".jobs(note text)')
    await owner.execute(f"INSERT INTO \"{schema}\".jobs VALUES('unrelated')")
    store = ControlStore(dsn, schema=schema)
    p = {"tenant": "tenant", "subject": "frank"}
    try:
        await store.start()
        job = await store.submit(p, "same-key", {"manifest": {}})
        assert (await store.submit(p, "same-key", {"manifest": {}}))["id"] == job["id"]
        claimed = await store.claim()
        assert claimed["id"] == job["id"]
        assert await store.finish(claimed, {"candidate": {}})
        assert (await store.get(p, job["id"]))["state"] == "review_ready"
        assert len(await store.list(p)) == 1
        assert await owner.fetchval(f'SELECT note FROM "{schema}".jobs') == "unrelated"
        assert await store.pool.fetchval(f"SELECT count(*) FROM {store.audit_table}") == 1
        await store.close()
        await store.start()
        assert (await store.get(p, job["id"]))["state"] == "review_ready"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_schema_gives_actionable_error_and_closes_pool(restricted_database):
    _, dsn = restricted_database
    store = ControlStore(dsn, schema="missing_schema")
    with pytest.raises(RuntimeError, match="configure storage_pg.schema"):
        await store.start()
    assert store.pool is None


async def seed_legacy(dsn):
    old = ControlStore(dsn, schema="te_ontology")
    await old.start()
    p = {"tenant": "tenant", "subject": "frank"}
    job = await old.submit(p, "old-job", {"manifest": {}})
    # Reproduce the pre-upgrade names with real FKs and serial ownership.
    for name in ("migrations", "jobs", "outbox", "audit"):
        await old.pool.execute(f"ALTER TABLE te_ontology.ontology_{name} RENAME TO {name}")
    await old.close()
    return p, job


@pytest.mark.asyncio
async def test_legacy_history_moves_atomically_and_audit_sequence_survives(restricted_database):
    owner, dsn = restricted_database
    p, job = await seed_legacy(dsn)
    store = ControlStore(dsn, schema="teamevolver")
    try:
        await store.start()
        assert (await store.get(p, job["id"]))["key"] == "old-job"
        assert (await store.claim())["id"] == job["id"]
        await store.audit(p, "after-migration", {})
        assert await store.pool.fetchval(f"SELECT count(*) FROM {store.audit_table}") == 2
        assert await owner.fetchval("SELECT to_regclass('te_ontology.jobs')") is None
        assert (
            await store.pool.fetchval(f"SELECT count(*) FROM {store.migrations} WHERE id='v5-002-configurable-schema'")
            == 1
        )
        await store.close()
        await store.start()
        assert (await store.get(p, job["id"]))["attempt"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_existing_target_blocks_legacy_migration_without_data_loss(restricted_database):
    owner, dsn = restricted_database
    p, job = await seed_legacy(dsn)
    await owner.execute("CREATE TABLE teamevolver.ontology_jobs(note text)")
    store = ControlStore(dsn)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        await store.start()
    assert store.pool is None
    assert await owner.fetchval("SELECT id FROM te_ontology.jobs WHERE tenant=$1", p["tenant"]) == job["id"]
    assert await owner.fetchval("SELECT to_regclass('te_ontology.outbox')") is not None


@pytest.mark.asyncio
async def test_partial_migration_failure_rolls_back_all_table_moves(restricted_database):
    owner, dsn = restricted_database
    p, job = await seed_legacy(dsn)
    await owner.execute('CREATE TABLE teamevolver.unrelated(id text)')
    # The source jobs index follows the table into the new schema. Make it
    # collide after the migrations table has already moved inside the transaction.
    await owner.execute('CREATE INDEX ontology_jobs_pkey ON teamevolver.unrelated(id)')
    store = ControlStore(dsn)
    with pytest.raises(asyncpg.PostgresError):
        await store.start()
    assert store.pool is None
    assert await owner.fetchval("SELECT to_regclass('teamevolver.ontology_migrations')") is None
    assert await owner.fetchval("SELECT to_regclass('te_ontology.migrations')") is not None
    assert await owner.fetchval("SELECT id FROM te_ontology.jobs WHERE tenant=$1", p['tenant']) == job['id']
