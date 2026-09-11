"""Real PostgreSQL acceptance tests. Set TE_PG_TEST_DSN to an isolated database."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from teamEvolver.session_store import SessionStore
from teamEvolver.storage.pg_pool import PgRuntime
from teamEvolver.storage.pg_store import PgObjectStore
from teamEvolver.tenants.registry import TenantRegistry

pytestmark = pytest.mark.skipif(not os.environ.get("TE_PG_TEST_DSN"), reason="TE_PG_TEST_DSN not set")


@pytest.fixture
def runtime():
    runtime = PgRuntime(
        dsn=os.environ["TE_PG_TEST_DSN"],
        schema="test_" + uuid.uuid4().hex[:12],
        pool_min=1,
        pool_max=8,
    )
    yield runtime
    runtime.close()


def store(runtime, tenant="account-a"):
    return PgObjectStore(
        dsn=os.environ["TE_PG_TEST_DSN"],
        schema=runtime.schema,
        tenant_id=tenant,
        runtime=runtime,
    )


def test_concurrent_session_ingest_is_durable_and_isolated(runtime):
    def ingest(i):
        SessionStore(store(runtime)).save_queued(
            {
                "session_id": f"session-{i}",
                "turns": [{"prompt_text": f"task {i}"}],
            }
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(ingest, range(100)))
    saved = SessionStore(store(runtime))
    assert len(saved.load_index_rows()) == 100
    assert len(list(saved._bucket.iter_objects("sessions/"))) == 100
    assert SessionStore(store(runtime, "account-b")).load_index_rows() == []
    assert len(SessionStore(store(runtime)).list_queue(limit=200)) == 100
    second = PgRuntime(dsn=os.environ["TE_PG_TEST_DSN"], schema=runtime.schema, pool_min=1, pool_max=2)
    try:
        assert len(SessionStore(store(second)).load_index_rows()) == 100
    finally:
        second.close()


def test_create_if_absent_has_exactly_one_winner(runtime):
    # Force both transactions to reach the absent-row check at the same time.
    barrier = threading.Barrier(2)

    def create(value):
        bucket = store(runtime)
        barrier.wait()
        try:
            bucket.batch_write({"new-key": value}, preconditions={"new-key": {"kind": "create_if_absent"}})
            return True
        except RuntimeError:
            return False

    store(runtime).put_object("bootstrap", "x")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ["one", "two"]))
    assert sum(results) == 1


def test_rls_fail_closed_without_context(runtime):
    store(runtime).put_object("secret", "a")
    store(runtime, "account-b").put_object("secret", "b")

    async def check():
        pool = await runtime._ensure_pool()
        async with pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT count(*) FROM {runtime.schema}.objects") == 0
        async with runtime.tenant_conn("account-a") as conn:
            rows = await conn.fetch(f"SELECT tenant_id FROM {runtime.schema}.objects")
            assert {row["tenant_id"] for row in rows} == {"account-a"}

    runtime.run(check(), timeout=5)


def test_cycle_lock_covers_real_registries_and_releases(runtime):
    other = PgRuntime(dsn=os.environ["TE_PG_TEST_DSN"], schema=runtime.schema, pool_min=1, pool_max=2)
    try:
        first = TenantRegistry(runtime=runtime)
        second = TenantRegistry(runtime=other)
        assert first.runtime.try_advisory_lock("account-a")
        assert not first.runtime.try_advisory_lock("account-a")
        assert not second.runtime.try_advisory_lock("account-a")
        first.runtime.release_advisory_lock("account-a")
        assert second.runtime.try_advisory_lock("account-a")
        second.runtime.release_advisory_lock("account-a")
    finally:
        other.close()


def test_config_updates_merge_without_lost_fields(runtime):
    registry = TenantRegistry(runtime=runtime)
    ctx, _ = registry.create_tenant("test")
    with ThreadPoolExecutor(max_workers=12) as executor:
        list(
            executor.map(
                lambda i: registry.update_tenant_config(ctx.tenant_id, {f"field_{i}": i}),
                range(24),
            )
        )
    assert len(registry.get(ctx.tenant_id).config_overrides) == 24
