"""Real PostgreSQL coverage for the console identity control plane."""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi import HTTPException

from teamEvolver.admin.identity_store import IdentityStore
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.storage.pg_pool import PgRuntime


@pytest.fixture
def pg_identity():
    dsn = os.environ.get("TEAMEVOLVER_TEST_PG_DSN", "")
    if not dsn:
        pytest.skip("TEAMEVOLVER_TEST_PG_DSN is not configured")
    schema = f"te_identity_test_{os.getpid()}_{time.time_ns()}"
    runtime = PgRuntime(dsn=dsn, schema=schema, pool_min=1, pool_max=5, ssl="disable")
    try:
        config = TeamEvolverConfig(
            storage_pg_enabled=True, storage_pg_dsn=dsn,
            storage_pg_schema=schema, storage_pg_ssl="disable",
        )
        store = IdentityStore(config, runtime=runtime)

        def seed(data):
            data["users"] = [
                {"id": "root", "platform_role": "platform_admin"},
                {"id": "alice", "platform_role": "user"},
            ]

        store.edit_users(seed, actor="test", action="identity.seed")
        store.set_member("default", "root", "tenant_admin", "active", "test")
        store.set_member("default", "alice", "member", "active", "test")
        yield store
    finally:
        async def drop():
            pool = await runtime._ensure_pool()
            async with pool.acquire() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

        runtime.run(drop(), timeout=30)
        runtime.close()


def test_pg_identity_migration_concurrency_and_guards(pg_identity):
    store = pg_identity
    users = {user["id"]: user for user in store.load_registry()["users"]}
    assert users["root"]["platform_role"] == "platform_admin"
    assert users["alice"]["platform_role"] == "user"
    assert store.memberships(user_id="root")[0]["role"] == "tenant_admin"

    store.set_member("alpha", "alice", "tenant_admin", "active", "root")
    token = "browser-session"
    store.session(token, value={"user_id": "root", "expires_at": time.time() + 3600})
    support = store.support("root", token, "alpha", reason="integration test")
    assert store.support("root", token, "alpha")["id"] == support["id"]

    errors = []
    def read():
        try:
            for _ in range(10):
                assert len(store.load_registry()["users"]) == 2
                assert store.memberships(tenant_id="alpha")[0]["user_id"] == "alice"
        except Exception as exc:  # noqa: BLE001 - aggregate thread assertion failures
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    with pytest.raises(HTTPException) as exc:
        store.edit_users(
            lambda data: next(user for user in data["users"] if user["id"] == "root").update(
                platform_role="user"
            ),
            actor="root",
        )
    assert exc.value.detail == "LAST_PLATFORM_ADMIN"
    store.session(token, delete=True)
    assert store.support("root", token, "alpha") is None
