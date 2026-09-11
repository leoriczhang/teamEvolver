"""Tests for the tenant registry (multi-tenancy plan Phase 1).

Runs the PG mode against an in-memory fake (no database); the SQL shapes are
the same ones verified against real PG in test_pg_object_store.py.
"""

from __future__ import annotations

import asyncio

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.tenants.registry import (
    AGENT_TOKEN_PREFIX,
    DEFAULT_TENANT_ID,
    QUOTA_MAX_CONCURRENT_SESSIONS,
    QUOTA_MAX_EVOLVE_PER_DAY,
    TenantContext,
    TenantRegistry,
    apply_tenant_config_overrides,
    current_tenant_id,
    hash_agent_token,
    set_current_tenant,
    reset_current_tenant,
    tenant_quotas,
)


# --------------------------------------------------------------------------- #
# Fake PG runtime (tenants table semantics)                                    #
# --------------------------------------------------------------------------- #


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeTenantConn:
    def __init__(self, runtime):
        self._runtime = runtime

    async def execute(self, sql, *args):
        return ""

    async def fetchrow(self, sql, *args):
        rows = self._runtime.rows
        if "WHERE agent_token_hash" in sql:
            for record in rows.values():
                if record["agent_token_hash"] == args[0] and record["status"] == "active":
                    return dict(record)
            return None
        if "INSERT INTO" in sql:
            tid, display, token_hash = args
            record = rows.setdefault(
                tid,
                {
                    "tenant_id": tid,
                    "display_name": display,
                    "status": "active",
                    "config": {},
                    "agent_token_hash": "",
                    "created_at": 0,
                },
            )
            record["display_name"] = display
            record["agent_token_hash"] = token_hash
            return dict(record)
        if "SET agent_token_hash" in sql:
            record = rows.get(args[0])
            if record is None or record["status"] != "active":
                return None
            record["agent_token_hash"] = args[1]
            return dict(record)
        if "SET status" in sql:
            record = rows.get(args[0])
            if record is None:
                return None
            record["status"] = args[1]
            return dict(record)
        if "SET config" in sql:
            import json as _json

            record = rows.get(args[0])
            if record is None:
                return None
            # Real SQL uses atomic jsonb operators (no read-modify-write):
            #   config || $n::jsonb      → merge upserts
            #   config - $n::text[]       → delete keys
            #   (config - $n::text[]) || $m::jsonb  → both
            config = dict(record.get("config") or {})
            if "::text[]" in sql and "::jsonb" in sql:
                # Combined: (config - $2::text[]) || $3::jsonb
                for key in args[1]:
                    config.pop(key, None)
                config.update(_json.loads(args[2]))
            elif "::jsonb" in sql:
                # config || $2::jsonb (upsert only)
                config.update(_json.loads(args[1]))
            elif "::text[]" in sql:
                # config - $2::text[] (delete only)
                for key in args[1]:
                    config.pop(key, None)
            else:
                record["config"] = _json.loads(args[1])
                return dict(record)
            record["config"] = config
            return dict(record)
        if "WHERE tenant_id" in sql:
            record = rows.get(args[0])
            return dict(record) if record else None
        raise AssertionError(f"unexpected fetchrow: {sql!r}")

    async def fetch(self, sql, *args):
        if "ORDER BY created_at" in sql:
            return [
                dict(r)
                for r in sorted(self._runtime.rows.values(), key=lambda r: (r["created_at"], r["tenant_id"]))
            ]
        raise AssertionError(f"unexpected fetch: {sql!r}")


class FakeTenantRuntime:
    """PgRuntime double: schema attr + run()/tenant_conn() like the real one."""

    def __init__(self, schema: str = "teamevolver"):
        self.schema = schema
        self.rows: dict[str, dict] = {}

    def run(self, coro, timeout=None):
        return asyncio.run(coro)

    def tenant_conn(self, tenant_id: str):
        runtime = self
        runtime.last_tenant_id = tenant_id

        class _CM:
            async def __aenter__(self):
                return FakeTenantConn(runtime)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _CM()

    def close(self, timeout: float = 10.0) -> None:
        pass


def make_pg_registry(schema: str = "teamevolver") -> tuple[TenantRegistry, FakeTenantRuntime]:
    runtime = FakeTenantRuntime(schema=schema)
    return TenantRegistry(runtime=runtime), runtime


# --------------------------------------------------------------------------- #
# Single-tenant compat mode                                                    #
# --------------------------------------------------------------------------- #


def test_single_mode_is_default_without_pg():
    registry = TenantRegistry(None)
    assert registry.mode == "single"
    assert registry.default_context().tenant_id == DEFAULT_TENANT_ID
    assert registry.get("default").tenant_id == DEFAULT_TENANT_ID
    assert registry.get("t_other") is None
    assert registry.resolve_by_agent_token("tevt_abc") is None
    assert registry.list_tenants() == [registry.default_context()]


def test_single_mode_refuses_admin_mutations():
    registry = TenantRegistry(None)
    with pytest.raises(RuntimeError, match="storage_pg"):
        registry.create_tenant("acme")
    with pytest.raises(RuntimeError, match="storage_pg"):
        registry.rotate_token("t_x")


def test_pg_config_without_enabled_stays_single(monkeypatch):
    monkeypatch.delenv("OV_PG_HOST", raising=False)
    config = TeamEvolverConfig(storage_pg_enabled=False, storage_pg_dsn="postgresql://u:p@h/db")
    assert TenantRegistry(config).mode == "single"
    config_enabled = TeamEvolverConfig(storage_pg_enabled=True, storage_pg_dsn="postgresql://u:p@h/db")
    registry = TenantRegistry(config_enabled)
    assert registry.mode == "postgres"
    registry._runtime.close()


def test_pg_config_derives_dsn_from_env(monkeypatch):
    monkeypatch.setenv("OV_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("OV_PG_PORT", "5660")
    monkeypatch.setenv("OV_PG_DATABASE", "openviking")
    monkeypatch.setenv("OV_PG_USERNAME", "openviking")
    monkeypatch.setenv("OV_PG_PASSWORD", "Example#password")
    registry = TenantRegistry(TeamEvolverConfig(storage_pg_enabled=True))
    assert registry.mode == "postgres"
    registry._runtime.close()


# --------------------------------------------------------------------------- #
# Token + tenant lifecycle (PG mode, fake runtime)                             #
# --------------------------------------------------------------------------- #


def test_create_resolve_rotate_disable_lifecycle():
    registry, runtime = make_pg_registry()
    ctx, token = registry.create_tenant("acme corp")
    assert token.startswith(AGENT_TOKEN_PREFIX)
    assert ctx.tenant_id.startswith("t_")
    assert ctx.display_name == "acme corp"

    resolved = registry.resolve_by_agent_token(token)
    assert resolved is not None and resolved.tenant_id == ctx.tenant_id

    # rotate: old token dies immediately, new one works
    new_token = registry.rotate_token(ctx.tenant_id)
    assert new_token is not None and new_token != token
    assert registry.resolve_by_agent_token(token) is None
    assert registry.resolve_by_agent_token(new_token).tenant_id == ctx.tenant_id

    # disable: token no longer resolves, row still fetchable by id
    assert registry.set_status(ctx.tenant_id, "disabled") is True
    assert registry.resolve_by_agent_token(new_token) is None
    assert registry.get(ctx.tenant_id).status == "disabled"

    # unknown tenant rotations 404 upstream
    assert registry.rotate_token("t_missing") is None


def test_update_tenant_config_merges_overrides():
    registry, runtime = make_pg_registry()
    ctx, _token = registry.create_tenant("acme corp")

    updated = registry.update_tenant_config(
        ctx.tenant_id,
        {"langfuse_host": "https://lf.example.com", "langfuse_enabled": True},
    )
    assert updated is not None
    assert updated.config_overrides["langfuse_host"] == "https://lf.example.com"
    assert updated.config_overrides["langfuse_enabled"] is True

    # Second update merges: earlier keys survive, same-key values win.
    registry.update_tenant_config(ctx.tenant_id, {"langfuse_host": "https://lf2.example.com"})
    fetched = registry.get(ctx.tenant_id)
    assert fetched.config_overrides["langfuse_host"] == "https://lf2.example.com"
    assert fetched.config_overrides["langfuse_enabled"] is True

    # Unknown tenant / empty payload are rejected without SQL side effects.
    assert registry.update_tenant_config("t_missing", {"langfuse_host": "x"}) is None
    with pytest.raises(ValueError):
        registry.update_tenant_config(ctx.tenant_id, {})


def test_update_tenant_config_null_deletes_key():
    """JSON null removes an override so the tenant falls back to global config."""
    registry, _runtime = make_pg_registry()
    ctx, _token = registry.create_tenant("acme corp")

    registry.update_tenant_config(
        ctx.tenant_id, {"langfuse_host": "https://lf.example.com", "langfuse_enabled": True}
    )
    registry.update_tenant_config(ctx.tenant_id, {"langfuse_host": None})
    fetched = registry.get(ctx.tenant_id)
    assert "langfuse_host" not in fetched.config_overrides
    # Other keys survive the deletion merge.
    assert fetched.config_overrides["langfuse_enabled"] is True
    # Deleting an absent key is a no-op, not an error.
    registry.update_tenant_config(ctx.tenant_id, {"langfuse_host": None})
    assert "langfuse_host" not in registry.get(ctx.tenant_id).config_overrides


def test_update_tenant_config_requires_pg():
    registry = TenantRegistry(None)
    with pytest.raises(RuntimeError):
        registry.update_tenant_config("default", {"langfuse_host": "x"})


def test_non_tevt_tokens_never_resolve():
    registry, _ = make_pg_registry()
    assert registry.resolve_by_agent_token("tev1_legacy_agent_token") is None
    assert registry.resolve_by_agent_token("") is None


def test_negative_lookup_is_cached():
    registry, runtime = make_pg_registry()
    calls = {"n": 0}
    original_fetch = registry._fetch_by_token

    async def counting_fetch(token_hash):
        calls["n"] += 1
        return await original_fetch(token_hash)

    registry._fetch_by_token = counting_fetch
    for _ in range(3):
        assert registry.resolve_by_agent_token("tevt_nope") is None
    assert calls["n"] == 1  # negative result cached


def test_schema_follows_runtime():
    registry, runtime = make_pg_registry(schema="custom_schema")
    registry.create_tenant("acme")
    # rows landed through the fake regardless of schema; assert the schema the
    # registry would qualify SQL with matches its runtime
    assert registry._schema == "custom_schema"


def test_list_tenants_reflects_creates():
    registry, _ = make_pg_registry()
    registry.create_tenant("one")
    registry.create_tenant("two")
    names = sorted(t.display_name for t in registry.list_tenants())
    assert names == ["one", "two"]


# --------------------------------------------------------------------------- #
# Config override merge                                                        #
# --------------------------------------------------------------------------- #


def test_apply_tenant_config_overrides():
    base = TeamEvolverConfig(llm_model_id="volcengine/glm-5.2-aicc", evolve_interval_seconds=600)
    merged = apply_tenant_config_overrides(base, {"llm_model_id": "other/model"})
    assert merged.llm_model_id == "other/model"
    assert merged.evolve_interval_seconds == 600
    # base untouched (dataclasses.replace returns a copy)
    assert base.llm_model_id == "volcengine/glm-5.2-aicc"


def test_apply_tenant_config_overrides_ignores_unknown_fields():
    base = TeamEvolverConfig()
    merged = apply_tenant_config_overrides(base, {"not_a_field": 1, "llm_model_id": "m"})
    assert merged.llm_model_id == "m"
    assert not hasattr(merged, "not_a_field")


def test_apply_tenant_config_overrides_ignores_service_wide_tracing_fields():
    base = TeamEvolverConfig(
        langfuse_tracing_enabled=True,
        langfuse_tracing_host="https://global-observability.example.com",
    )
    merged = apply_tenant_config_overrides(
        base,
        {
            "langfuse_host": "https://tenant-source.example.com",
            "langfuse_tracing_enabled": False,
            "langfuse_tracing_host": "https://tenant-observability.example.com",
        },
    )

    assert merged.langfuse_host == "https://tenant-source.example.com"
    assert merged.langfuse_tracing_enabled is True
    assert (
        merged.langfuse_tracing_host
        == "https://global-observability.example.com"
    )


def test_apply_tenant_config_overrides_noop():
    base = TeamEvolverConfig()
    assert apply_tenant_config_overrides(base, None) is base
    assert apply_tenant_config_overrides(base, {}) is base


# --------------------------------------------------------------------------- #
# Contextvar helpers                                                           #
# --------------------------------------------------------------------------- #


def test_contextvar_roundtrip():
    from teamEvolver.tenants.registry import TenantContext

    assert current_tenant_id() == DEFAULT_TENANT_ID
    token = set_current_tenant(TenantContext(tenant_id="t_x"))
    try:
        assert current_tenant_id() == "t_x"
    finally:
        reset_current_tenant(token)
    assert current_tenant_id() == DEFAULT_TENANT_ID


# --------------------------------------------------------------------------- #
# Per-tenant quotas (plan Phase 3)                                             #
# --------------------------------------------------------------------------- #


def test_tenant_quotas_parsing():
    ctx = TenantContext(
        tenant_id="t_q1",
        config_overrides={"max_concurrent_sessions": "3", "max_evolve_per_day": 10},
    )
    quotas = tenant_quotas(ctx)
    assert quotas[QUOTA_MAX_CONCURRENT_SESSIONS] == 3
    assert quotas[QUOTA_MAX_EVOLVE_PER_DAY] == 10


def test_tenant_quotas_defaults_and_garbage():
    assert tenant_quotas(None) == {
        QUOTA_MAX_CONCURRENT_SESSIONS: 0,
        QUOTA_MAX_EVOLVE_PER_DAY: 0,
    }
    bad = TenantContext(
        tenant_id="t_q2",
        config_overrides={"max_concurrent_sessions": "abc", "max_evolve_per_day": -5},
    )
    assert tenant_quotas(bad) == {
        QUOTA_MAX_CONCURRENT_SESSIONS: 0,
        QUOTA_MAX_EVOLVE_PER_DAY: 0,
    }


def test_quota_keys_not_applied_as_config_overrides(caplog):
    base = TeamEvolverConfig()
    with caplog.at_level("WARNING"):
        merged = apply_tenant_config_overrides(
            base, {"max_concurrent_sessions": 2, "max_evolve_per_day": 5}
        )
    assert merged is base
    assert "ignoring unknown config override" not in caplog.text
