"""Tests for the PostgreSQL object-store backend (multi-tenancy plan Phase 0).

Unit tests run against an in-memory fake asyncpg double — no database needed.
The real-database integration test is gated behind ``TE_PG_TEST_DSN``.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from teamEvolver.storage import PgObjectStore, build_object_store, normalize_backend
from teamEvolver.storage.pg_pool import (
    build_pg_dsn,
    close_pg_runtimes,
    dsn_from_env,
    init_pg_schema,
    validate_schema_name,
)
from teamEvolver.storage.pg_store import validate_tenant_id


# --------------------------------------------------------------------------- #
# Fake asyncpg: dict-backed rows + transactional rollback                      #
# --------------------------------------------------------------------------- #


class FakeConn:
    """Interprets the fixed-shape SQL emitted by PgObjectStore."""

    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool
        self.executed: list[tuple[str, tuple]] = []

    @property
    def rows(self) -> dict[tuple[str, str], bytes]:
        return self._pool.rows

    async def execute(self, sql: str, *args) -> str:
        self.executed.append((sql, args))
        if "set_config" in sql:
            return ""
        if sql.lstrip().startswith("INSERT"):
            tenant, key, body = args
            self.rows[(tenant, key)] = bytes(body)
            return "INSERT 0 1"
        if sql.lstrip().startswith("DELETE"):
            tenant, key = args
            self.rows.pop((tenant, key), None)
            return "DELETE 0"
        return ""

    async def fetchrow(self, sql: str, *args):
        self.executed.append((sql, args))
        if sql.lstrip().startswith("SELECT content"):
            tenant, key = args
            content = self.rows.get((tenant, key))
            return {"content": content} if content is not None else None
        raise AssertionError(f"unexpected fetchrow: {sql!r}")

    async def fetch(self, sql: str, *args):
        self.executed.append((sql, args))
        if sql.lstrip().startswith("SELECT key"):
            tenant, prefix = args
            keys = sorted(k for (t, k) in self.rows if t == tenant and k.startswith(prefix))
            return [{"key": k} for k in keys]
        raise AssertionError(f"unexpected fetch: {sql!r}")

    def transaction(self):
        return _FakeTransaction(self._pool)


class _FakeTransaction:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool

    async def __aenter__(self):
        self._snapshot = dict(self._pool.rows)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None:
            # All-or-nothing: restore the pre-transaction row set.
            self._pool.rows.clear()
            self._pool.rows.update(self._snapshot)
        return False


class FakePool:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], bytes] = {}
        self.closed = False

    def acquire(self):
        # Matches asyncpg: acquire() returns an async context manager.
        return _FakeAcquire(FakeConn(self))

    async def close(self) -> None:
        self.closed = True


class _FakeAcquire:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeRuntime:
    """PgRuntime double: runs coroutines inline on a fresh loop per call."""

    def __init__(self, schema: str = "teamevolver") -> None:
        self.schema = schema
        self.pool = FakePool()
        self.closed = False

    def run(self, coro, timeout=None):
        return asyncio.run(coro)

    def pool_status(self, *, timeout: float = 5.0):
        return {"reachable": True, "pool_size": 2, "pool_idle": 1, "ping_ms": 0.5}

    def close(self, timeout: float = 10.0) -> None:
        self.closed = True

    def tenant_conn(self, tenant_id: str):
        runtime = self

        class _TenantConn:
            async def __aenter__(self):
                runtime.last_tenant_id = tenant_id
                cm = runtime.pool.acquire()
                conn = await cm.__aenter__()
                runtime._active_cm = cm
                return conn

            async def __aexit__(self, exc_type, exc, tb):
                return await runtime._active_cm.__aexit__(exc_type, exc, tb)

        return _TenantConn()


def make_store(tenant_id: str = "default") -> tuple[PgObjectStore, FakeRuntime]:
    runtime = FakeRuntime()
    store = PgObjectStore(dsn="postgresql://u:p@localhost:5432/db", tenant_id=tenant_id, runtime=runtime)
    return store, runtime


# --------------------------------------------------------------------------- #
# DSN / naming helpers                                                         #
# --------------------------------------------------------------------------- #


def test_build_pg_dsn_percent_encodes_reserved_password():
    # 'Example#password' is the documented real-world truncation case (PG guide §4.1)
    dsn = build_pg_dsn(
        host="openviking-pgm.dbsit.sfcloud.local",
        port=5660,
        database="openviking",
        username="openviking",
        password="Example#password",
    )
    assert dsn == (
        "postgresql://openviking:Example%23password"
        "@openviking-pgm.dbsit.sfcloud.local:5660/openviking"
    )


def test_dsn_from_env(monkeypatch):
    monkeypatch.setenv("OV_PG_HOST", "pg.example.local")
    monkeypatch.setenv("OV_PG_PORT", "5660")
    monkeypatch.setenv("OV_PG_DATABASE", "openviking")
    monkeypatch.setenv("OV_PG_USERNAME", "openviking")
    monkeypatch.setenv("OV_PG_PASSWORD", "p@ss")
    assert dsn_from_env() == "postgresql://openviking:p%40ss@pg.example.local:5660/openviking"


def test_dsn_from_env_empty_without_host(monkeypatch):
    monkeypatch.delenv("OV_PG_HOST", raising=False)
    assert dsn_from_env() == ""


def test_validate_schema_name_rejects_injection():
    assert validate_schema_name("teamevolver") == "teamevolver"
    for bad in ("teamevolver; DROP SCHEMA x", "team evolver", "TeamEvolver-1", "", "1abc"):
        with pytest.raises(ValueError):
            validate_schema_name(bad)


def test_validate_tenant_id():
    assert validate_tenant_id("default") == "default"
    assert validate_tenant_id("t_a1b2c3") == "t_a1b2c3"
    for bad in ("", "../escape", "a b", "x" * 200):
        with pytest.raises(ValueError):
            validate_tenant_id(bad)


# --------------------------------------------------------------------------- #
# Backend wiring                                                               #
# --------------------------------------------------------------------------- #


def test_normalize_backend_pg_aliases():
    assert normalize_backend("postgres") == "postgres"
    assert normalize_backend("pg") == "postgres"
    assert normalize_backend("postgresql") == "postgres"
    assert normalize_backend("viking") == "viking"
    assert normalize_backend("local") == "local"
    # Unknown names still collapse to viking, and empty stays unconfigured.
    assert normalize_backend("whatever") == "viking"
    assert normalize_backend("") == ""


def test_build_object_store_postgres(monkeypatch):
    monkeypatch.setenv("OV_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("OV_PG_PORT", "5660")
    monkeypatch.setenv("OV_PG_DATABASE", "openviking")
    monkeypatch.setenv("OV_PG_USERNAME", "openviking")
    monkeypatch.setenv("OV_PG_PASSWORD", "Example#password")
    try:
        store = build_object_store(backend="postgres", pg_schema="teamevolver", tenant_id="default")
        assert isinstance(store, PgObjectStore)
        assert store.tenant_id == "default"
    finally:
        close_pg_runtimes()


def test_build_object_store_postgres_requires_dsn(monkeypatch):
    monkeypatch.delenv("OV_PG_HOST", raising=False)
    with pytest.raises(ValueError, match="DSN"):
        build_object_store(backend="postgres")


# --------------------------------------------------------------------------- #
# Object-store contract (fake runtime)                                         #
# --------------------------------------------------------------------------- #


def test_put_get_roundtrip():
    store, runtime = make_store()
    store.put_object("skills/demo/SKILL.md", "# hello")
    obj = store.get_object("skills/demo/SKILL.md")
    assert obj.read() == b"# hello"
    assert obj.key == "skills/demo/SKILL.md"


def test_get_missing_raises_file_not_found():
    store, _ = make_store()
    with pytest.raises(FileNotFoundError):
        store.get_object("nope.json")


def test_put_normalizes_key():
    store, runtime = make_store()
    store.put_object("/skills/a//b.md", "x")
    assert store.get_object("skills/a/b.md").read() == b"x"


def test_delete_is_idempotent():
    store, _ = make_store()
    store.put_object("k.json", b"v")
    store.delete_object("k.json")
    store.delete_object("k.json")  # no error
    with pytest.raises(FileNotFoundError):
        store.get_object("k.json")


def test_iter_objects_prefix_sorted():
    store, _ = make_store()
    for key in ("sessions/b.json", "sessions/a.json", "skills/x/SKILL.md", "sessions/deep/c.json"):
        store.put_object(key, b"v")
    keys = [obj.key for obj in store.iter_objects(prefix="sessions/")]
    assert keys == ["sessions/a.json", "sessions/b.json", "sessions/deep/c.json"]
    assert [obj.key for obj in store.iter_objects()] == sorted(
        ["sessions/b.json", "sessions/a.json", "skills/x/SKILL.md", "sessions/deep/c.json"]
    )


def test_empty_key_rejected():
    store, _ = make_store()
    with pytest.raises(ValueError):
        store.get_object("")
    with pytest.raises(ValueError):
        store.put_object("  ", b"v")


# --------------------------------------------------------------------------- #
# Conditional batch write                                                      #
# --------------------------------------------------------------------------- #


def test_batch_write_success():
    store, _ = make_store()
    result = store.batch_write(
        {"manifest.json": "m", "skills/x/SKILL.md": "s"},
        preconditions={
            "manifest.json": {"kind": "create_if_absent"},
            "skills/x/SKILL.md": {"kind": "create_if_absent"},
        },
    )
    assert result["mode"] == "transactional"
    assert result["succeeded"] == ["manifest.json", "skills/x/SKILL.md"]
    assert result["failed"] == []
    assert store.get_object("manifest.json").read() == b"m"


def test_batch_write_create_if_absent_conflict_rolls_back():
    store, runtime = make_store()
    store.put_object("manifest.json", b"existing")
    before = dict(runtime.pool.rows)
    with pytest.raises(RuntimeError, match="create_if_absent"):
        store.batch_write(
            {"manifest.json": "m", "skills/x/SKILL.md": "s"},
            preconditions={"manifest.json": {"kind": "create_if_absent"}},
        )
    # All-or-nothing: the second object must not have been written.
    assert runtime.pool.rows == before


def test_batch_write_replace_if_hash_mismatch():
    store, _ = make_store()
    store.put_object("manifest.json", b"v1")
    precondition = {"kind": "replace_if_hash", "base_hash": "sha256:" + "0" * 64}
    with pytest.raises(RuntimeError, match="replace_if_hash"):
        store.batch_write(
            {"manifest.json": "v2"},
            preconditions={"manifest.json": precondition},
        )


def test_batch_write_replace_if_hash_success():
    store, _ = make_store()
    store.put_object("manifest.json", b"v1")
    precondition = store.object_precondition("manifest.json")
    store.batch_write(
        {"manifest.json": "v2"},
        preconditions={"manifest.json": precondition},
    )
    assert store.get_object("manifest.json").read() == b"v2"


def test_batch_write_default_mode_snapshots_inside_transaction():
    store, _ = make_store()
    store.put_object("manifest.json", b"v1")
    store.batch_write({"manifest.json": "v2"})
    assert store.get_object("manifest.json").read() == b"v2"


def test_batch_write_validates_size_limits():
    store, _ = make_store()
    with pytest.raises(ValueError, match="at least one"):
        store.batch_write({})
    big = b"x" * (8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="8 MiB"):
        store.batch_write({"big.bin": big})


# --------------------------------------------------------------------------- #
# Schema bootstrap DDL                                                         #
# --------------------------------------------------------------------------- #


class RecordingConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, sql: str, *args) -> str:
        self.statements.append(sql)
        return ""


def test_init_pg_schema_ddl_is_complete_and_idempotent():
    conn = RecordingConn()
    asyncio.run(init_pg_schema(conn, "teamevolver"))
    joined = "\n".join(conn.statements)
    assert "CREATE SCHEMA IF NOT EXISTS teamevolver" in joined
    assert "CREATE TABLE IF NOT EXISTS teamevolver.tenants" in joined
    assert "CREATE TABLE IF NOT EXISTS teamevolver.objects" in joined
    assert "ENABLE ROW LEVEL SECURITY" in joined
    assert "FORCE ROW LEVEL SECURITY" in joined
    assert "tenant_isolation" in joined
    assert "current_setting('app.tenant_id', true)" in joined
    assert "VALUES ('default'" in joined
    # Running twice must not raise (CREATE ... IF NOT EXISTS + guarded policy).
    asyncio.run(init_pg_schema(conn, "teamevolver"))


# --------------------------------------------------------------------------- #
# Config → backend wiring                                                      #
# --------------------------------------------------------------------------- #


class _StubConfig:
    """Attribute stub mirroring the fields SkillHub._build / settings read."""

    def __init__(self, **overrides):
        attrs = dict(
            sharing_backend="viking",
            sharing_session_backend="postgres",
            sharing_skill_backend="",
            sharing_endpoint="",
            sharing_viking_endpoint="",
            sharing_viking_api_key="",
            sharing_viking_personal_api_key="",
            sharing_viking_team_api_key="",
            sharing_viking_account="default",
            sharing_viking_user="team",
            sharing_viking_agent="team-skill-evolver",
            sharing_viking_agent_id="",
            sharing_viking_customer_id="",
            sharing_viking_root_prefix="team-skill-evolver",
            sharing_viking_group_id="",
            sharing_user_alias="",
            sharing_enabled=True,
            sharing_local_fallback_enabled=True,
            sharing_local_root="",
            sharing_skill_mirror_enabled=False,
            sharing_skill_mirror_spool_dir="",
            storage_pg_dsn="postgresql://u:p%40ss@localhost:5432/db",
            storage_pg_schema="teamevolver",
            storage_pg_pool_min=2,
            storage_pg_pool_max=20,
            storage_pg_command_timeout_seconds=30.0,
        )
        attrs.update(overrides)
        for key, value in attrs.items():
            setattr(self, key, value)


def test_skillhub_session_backend_postgres_wiring(monkeypatch):
    from teamEvolver.skills.hub import SkillHub

    captured: dict = {}
    monkeypatch.setattr(
        "teamEvolver.storage.pg_store.get_pg_runtime",
        lambda **kwargs: (captured.update(kwargs), FakeRuntime())[-1],
    )
    config = _StubConfig(storage_pg_schema="custom_schema")
    hub = SkillHub.object_storage_from_config(config)
    try:
        assert isinstance(hub._bucket, PgObjectStore)
        assert captured["dsn"] == "postgresql://u:p%40ss@localhost:5432/db"
        assert captured["schema"] == "custom_schema"
        assert hub._bucket.tenant_id == "default"
    finally:
        close_pg_runtimes()


def test_skillhub_local_backend_unaffected_by_pg_config(monkeypatch):
    from teamEvolver.skills.hub import SkillHub
    from teamEvolver.storage import LocalObjectStore

    monkeypatch.setattr(
        "teamEvolver.storage.pg_store.get_pg_runtime",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("runtime must not be built")),
    )
    config = _StubConfig(
        sharing_session_backend="local", sharing_local_root="/tmp/te_hub_local_root"
    )
    hub = SkillHub.object_storage_from_config(config)
    assert isinstance(hub._bucket, LocalObjectStore)
    close_pg_runtimes()


def test_evolve_server_config_postgres_bucket():
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    config = EvolveServerConfig(
        storage_backend="postgres",
        pg_dsn="postgresql://u:p@localhost:5432/db",
        pg_schema="teamevolver",
        pg_tenant_id="default",
    )
    bucket = EvolveServer._build_skill_bucket(config)
    try:
        assert isinstance(bucket, PgObjectStore)
        assert bucket.tenant_id == "default"
    finally:
        close_pg_runtimes()


# --------------------------------------------------------------------------- #
# pool_status (Phase 3: /storage/status PG health + pool metrics)              #
# --------------------------------------------------------------------------- #

def test_store_pool_status_delegates_and_tags_tenant():
    store, _ = make_store(tenant_id="t_acme")
    status = store.pool_status()
    assert status["reachable"] is True
    assert status["tenant_id"] == "t_acme"
    assert status["pool_size"] == 2


def test_runtime_pool_status_unreachable_never_raises():
    from teamEvolver.storage.pg_pool import PgRuntime

    # Port 1 is never listening: connection refused fast, no PG needed.
    runtime = PgRuntime(
        dsn="postgresql://u:p@127.0.0.1:1/db", schema="teamevolver", pool_min=1, pool_max=2
    )
    try:
        status = runtime.pool_status(timeout=5.0)
        assert status["reachable"] is False
        assert status["schema"] == "teamevolver"
        assert status["pool_min"] == 1
        assert status["pool_max"] == 2
        assert status["reason"]
    finally:
        runtime.close()


def test_runtime_pool_status_after_close():
    from teamEvolver.storage.pg_pool import PgRuntime

    runtime = PgRuntime(
        dsn="postgresql://u:p@127.0.0.1:1/db", schema="teamevolver", pool_min=1, pool_max=2
    )
    runtime.close()
    status = runtime.pool_status()
    assert status["reachable"] is False
    assert status["reason"] == "runtime_closed"


# --------------------------------------------------------------------------- #
# Real-database integration (gated)                                            #
# --------------------------------------------------------------------------- #

_REAL_DSN = os.environ.get("TE_PG_TEST_DSN", "")


@pytest.mark.skipif(not _REAL_DSN, reason="TE_PG_TEST_DSN not set")
def test_real_pg_roundtrip_and_tenant_isolation():
    tenant_a = "t_itest_a"
    tenant_b = "t_itest_b"
    store_a = PgObjectStore(dsn=_REAL_DSN, tenant_id=tenant_a)
    store_b = PgObjectStore(dsn=_REAL_DSN, tenant_id=tenant_b)
    try:
        key = "itest/hello.txt"
        store_a.put_object(key, "pg hello")
        assert store_a.get_object(key).read() == b"pg hello"
        keys = [obj.key for obj in store_a.iter_objects(prefix="itest/")]
        assert key in keys

        # RLS: tenant B must not see (or overwrite via plain get) tenant A's row.
        with pytest.raises(FileNotFoundError):
            store_b.get_object(key)
        assert [obj.key for obj in store_b.iter_objects(prefix="itest/")] == []

        store_a.delete_object(key)
        with pytest.raises(FileNotFoundError):
            store_a.get_object(key)
    finally:
        close_pg_runtimes()
