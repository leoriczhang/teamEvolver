"""EnginePool + multi-tenant scheduler tests (multi-tenancy plan Phase 2).

No real PostgreSQL is needed: engines and registries are fakes, and the
advisory-lock surface is exercised through a stub PgRuntime-like object.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

import teamEvolver.evolve as evolve_pkg
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.proxy.engine_pool import EnginePool
from teamEvolver.proxy.server import ProxyServer
from teamEvolver.tenants.registry import DEFAULT_TENANT_ID, TenantContext


class _HeldLock:
    """Stand-in for an asyncio.Lock stuck in the locked state."""

    def locked(self) -> bool:
        return True


class FakeEngine:
    """Minimal EvolveServer stand-in: records its config, tracks cycles."""

    instances: list["FakeEngine"] = []

    def __init__(self, config):
        self.config = config
        self.stopped = False
        self.run_once_calls = 0
        self.run_once_result: dict = {"sessions": 0}
        self._run_lock: asyncio.Lock | None = None
        self._eval_tasks: set = set()
        self._eval_jobs: set = set()
        self.app = object()  # create_http_app sentinel, unique per engine
        FakeEngine.instances.append(self)

    @property
    def tenant_id(self) -> str:
        return str(getattr(self.config, "pg_tenant_id", "") or DEFAULT_TENANT_ID)

    def _get_run_lock(self) -> asyncio.Lock:
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()
        return self._run_lock

    def stop(self) -> None:
        self.stopped = True

    async def run_once(self) -> dict:
        self.run_once_calls += 1
        return dict(self.run_once_result)

    def create_http_app(self):
        return self.app


@pytest.fixture(autouse=True)
def _fake_evolve_server(monkeypatch):
    """Swap the lazily-resolved EvolveServer for FakeEngine in every test."""
    monkeypatch.setattr(evolve_pkg, "EvolveServer", FakeEngine)


@pytest.fixture(autouse=True)
def _isolate_quota_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ProxyServer, "_evolve_daily_file", lambda self: tmp_path / "quota.json")


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    FakeEngine.instances = []
    yield
    FakeEngine.instances = []


def _builder(config, **_kwargs) -> EvolveServerConfig:
    """Mimics ProxyServer._build_embedded_evolve_config (tenant stays default)."""
    return EvolveServerConfig(
        pg_tenant_id="default",
        max_parallel_groups=int(
            getattr(config, "evolve_max_parallel_groups", 8) or 8
        ),
    )


# --------------------------------------------------------------------------- #
# EnginePool unit behavior                                                     #
# --------------------------------------------------------------------------- #


def test_pool_builds_engine_per_tenant_with_scoped_config():
    pool = EnginePool(object(), _builder, max_engines=4)

    engine = pool.get("t_abc12345")

    assert engine is not None
    assert engine.config.pg_tenant_id == "t_abc12345"
    assert pool.get("t_abc12345") is engine  # cached, same instance


def test_pool_default_tenant_uses_default_scope():
    pool = EnginePool(object(), _builder)

    engine = pool.get(DEFAULT_TENANT_ID)

    assert engine is not None


def test_pool_drop_rebuilds_with_fresh_config_and_clears_build_failure():
    """drop() pops the engine so the next get() rebuilds it (config updates)."""
    pool = EnginePool(object(), _builder)
    engine = pool.get("t_abc12345")
    assert engine is not None

    pool.drop("t_abc12345", reason="config updated")
    assert engine.stopped is True  # idle engine is stopped on drop

    rebuilt = pool.get("t_abc12345")
    assert rebuilt is not None and rebuilt is not engine

    # A build failure cached before the drop no longer blocks the tenant.
    def failing_builder(_config, **_kwargs):
        raise ValueError("boom")

    broken_pool = EnginePool(object(), failing_builder)
    assert broken_pool.get("t_abc12345") is None
    assert broken_pool.build_failed("t_abc12345") is True
    broken_pool.drop("t_abc12345", reason="config updated")
    assert broken_pool.build_failed("t_abc12345") is False
    assert broken_pool.get("t_abc12345") is None  # builder still broken
    assert broken_pool.build_failed("t_abc12345") is True


def test_pool_drop_skips_stop_for_busy_engine():
    """A cycle-running engine is dropped from the pool but not stopped mid-run."""
    pool = EnginePool(object(), _builder)
    engine = pool.get("t_abc12345")
    engine._run_lock = _HeldLock()  # busy: cycle in flight

    pool.drop("t_abc12345")

    assert engine.stopped is False
    assert pool.get("t_abc12345") is not engine


def test_pool_rejects_invalid_tenant_id():
    pool = EnginePool(object(), _builder)

    assert pool.get("../evil") is None
    assert pool.get("") is None
    assert FakeEngine.instances == []


def test_pool_build_failure_is_cached():
    calls = {"n": 0}

    def failing_builder(_config):
        calls["n"] += 1
        raise RuntimeError("boom")

    pool = EnginePool(object(), failing_builder)

    assert pool.get("t_abc12345") is None
    assert pool.get("t_abc12345") is None
    assert calls["n"] == 1
    assert pool.build_failed("t_abc12345")


def test_pool_lru_evicts_oldest_idle_engine():
    pool = EnginePool(object(), _builder, max_engines=2)

    first = pool.get("t_aaaa1111")
    pool.get("t_bbbb2222")
    pool.get("t_cccc3333")

    assert [e.config.pg_tenant_id for e in pool.engines()] == [
        "t_bbbb2222",
        "t_cccc3333",
    ]
    assert first is not None and first.stopped


def test_pool_never_evicts_busy_engine():
    pool = EnginePool(object(), _builder, max_engines=2)

    busy = pool.get("t_aaaa1111")
    assert busy is not None
    busy._run_lock = _HeldLock()  # simulate an in-flight evolution cycle

    pool.get("t_bbbb2222")
    pool.get("t_cccc3333")  # would evict the busy engine if it were not protected

    # The busy engine is retained; the next-idle tenant takes the hit instead.
    assert [e.config.pg_tenant_id for e in pool.engines()] == [
        "t_aaaa1111",
        "t_cccc3333",
    ]


def test_pool_tenant_config_overrides_are_applied():
    class FakeRegistry:
        mode = "single"

        def get(self, tenant_id):
            if tenant_id == "t_ovr00001":
                return TenantContext(
                    tenant_id=tenant_id, config_overrides={"evolve_max_parallel_groups": 3}
                )
            return None

    pool = EnginePool(
        TeamEvolverConfig(),
        _builder,
        registry_provider=lambda: FakeRegistry(),
    )

    engine = pool.get("t_ovr00001")

    assert engine is not None
    assert engine.config.max_parallel_groups == 3


def test_pool_tenant_ids_single_mode_lists_default_only():
    class FakeRegistry:
        mode = "single"

    pool = EnginePool(object(), _builder, registry_provider=lambda: FakeRegistry())

    assert pool.tenant_ids() == [DEFAULT_TENANT_ID]


def test_pool_tenant_ids_postgres_mode_filters_active_and_caches():
    class FakeRegistry:
        mode = "postgres"
        list_calls = 0

        def list_tenants(self):
            self.list_calls += 1
            return [
                TenantContext(tenant_id=DEFAULT_TENANT_ID),
                TenantContext(tenant_id="t_actv0001", status="active"),
                TenantContext(tenant_id="t_dis00001", status="disabled"),
            ]

    registry = FakeRegistry()
    pool = EnginePool(object(), _builder, registry_provider=lambda: registry)

    assert pool.tenant_ids() == [DEFAULT_TENANT_ID, "t_actv0001"]
    assert pool.tenant_ids() == [DEFAULT_TENANT_ID, "t_actv0001"]
    assert registry.list_calls == 1  # second call served from the TTL cache


def test_pool_peek_config_prefers_resident_engine():
    pool = EnginePool(object(), _builder)

    peeked_before = pool.peek_config("t_peek0001")
    assert peeked_before is not None

    engine = pool.get("t_peek0001")
    assert engine is not None
    assert pool.peek_config("t_peek0001") is engine.config


def test_pool_reset_drops_engines_and_failures():
    pool = EnginePool(object(), _builder)

    assert pool.get("t_rstt0001") is not None
    pool.reset()
    assert pool.engines() == []
    assert pool.get("t_rstt0001") is not None


def test_thread_safety_of_pool_get():
    pool = EnginePool(object(), _builder, max_engines=8)
    results: dict[str, list] = {}
    barrier = threading.Barrier(4)

    def worker(tid: str):
        barrier.wait()
        results.setdefault(tid, []).append(pool.get(tid))

    threads = [
        threading.Thread(target=worker, args=(f"t_thr000{i}",)) for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for tid, engines in results.items():
        assert all(engine is engines[0] for engine in engines)
        assert engines[0].config.pg_tenant_id == tid


# --------------------------------------------------------------------------- #
# ProxyServer integration (dispatch, scheduler, advisory cycle lock)           #
# --------------------------------------------------------------------------- #


def _make_server(monkeypatch, *, registry=None) -> ProxyServer:
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "1")
    server = ProxyServer(TeamEvolverConfig())

    built: dict[str, FakeEngine] = {}

    def _engine_for(tid: str) -> FakeEngine:
        if tid not in built:
            built[tid] = FakeEngine(
                EvolveServerConfig(
                    pg_tenant_id=tid, interval_seconds=1, drain_max_per_cycle=0
                )
            )
        return built[tid]

    fake_pool = SimpleNamespace(
        registry=lambda: registry,
        tenant_ids=lambda: [DEFAULT_TENANT_ID, "t_tool0001"],
        get=_engine_for,
    )
    monkeypatch.setattr(server, "_get_engine_pool", lambda: fake_pool)
    return server


def test_default_engine_slot_syncs_with_pool(monkeypatch):
    server = _make_server(monkeypatch)

    engine = server._get_embedded_evolve_server()

    assert engine is not None
    assert engine.config.pg_tenant_id == DEFAULT_TENANT_ID
    assert server._embedded_evolve_server is engine


def test_embedded_app_cache_is_per_tenant(monkeypatch):
    server = _make_server(monkeypatch)

    app_default = server._get_embedded_evolve_app(DEFAULT_TENANT_ID)
    app_default_again = server._get_embedded_evolve_app(DEFAULT_TENANT_ID)
    app_tenant = server._get_embedded_evolve_app("t_tool0001")

    engine_default = server._embedded_evolve_server
    assert engine_default is not None
    assert app_default is engine_default.app
    assert app_default_again is app_default
    assert app_tenant is not app_default
    assert set(server._embedded_evolve_apps) == {DEFAULT_TENANT_ID, "t_tool0001"}


class FakeRuntime:
    """PgRuntime stand-in exposing the advisory-cycle-lock surface."""

    def __init__(self, *, held_elsewhere: bool = False):
        self.held_elsewhere = held_elsewhere
        self.acquired: list[str] = []
        self.released: list[str] = []

    def try_advisory_lock(self, tenant_id: str, *, timeout: float = 10.0) -> bool:
        self.acquired.append(tenant_id)
        return not self.held_elsewhere

    def release_advisory_lock(self, tenant_id: str, *, timeout: float = 10.0) -> None:
        self.released.append(tenant_id)


class FakeRegistryWithRuntime:
    mode = "postgres"

    def __init__(self, runtime):
        self._runtime = runtime

    @property
    def runtime(self):
        return self._runtime


@pytest.mark.anyio
async def test_tenant_cycle_runs_and_releases_advisory_lock(monkeypatch):
    runtime = FakeRuntime()
    server = _make_server(monkeypatch, registry=FakeRegistryWithRuntime(runtime))
    engine = server._get_embedded_evolve_server("t_tool0001")
    assert engine is not None
    engine.run_once_result = {"sessions": 5}

    pool = server._get_engine_pool()
    drained = await server._run_tenant_cycle(pool, "t_tool0001", engine)

    assert drained == 5
    assert runtime.acquired == ["t_tool0001"]
    assert runtime.released == ["t_tool0001"]


@pytest.mark.anyio
async def test_tenant_cycle_skipped_when_lock_held_elsewhere(monkeypatch):
    runtime = FakeRuntime(held_elsewhere=True)
    server = _make_server(monkeypatch, registry=FakeRegistryWithRuntime(runtime))
    engine = server._get_embedded_evolve_server("t_tool0001")
    assert engine is not None

    pool = server._get_engine_pool()
    drained = await server._run_tenant_cycle(pool, "t_tool0001", engine)

    assert drained == 0
    assert engine.run_once_calls == 0  # cycle must not run
    assert runtime.acquired == ["t_tool0001"]
    assert runtime.released == []  # never held, nothing to release


@pytest.mark.anyio
async def test_tenant_cycle_without_pg_skips_advisory_lock(monkeypatch):
    server = _make_server(monkeypatch, registry=None)
    engine = server._get_embedded_evolve_server("t_tool0001")
    assert engine is not None
    engine.run_once_result = {"sessions": 2}

    pool = server._get_engine_pool()
    drained = await server._run_tenant_cycle(pool, "t_tool0001", engine)

    assert drained == 2


@pytest.mark.anyio
async def test_multi_tenant_scheduler_runs_cycle_per_tenant(monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_EVOLVE_TICK_S", "0.5")
    server = _make_server(monkeypatch)
    engine_default = server._get_embedded_evolve_server(DEFAULT_TENANT_ID)
    engine_tool = server._get_embedded_evolve_server("t_tool0001")
    assert engine_default is not None and engine_tool is not None

    task = asyncio.create_task(server._run_multi_tenant_evolve())
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if engine_default.run_once_calls >= 1 and engine_tool.run_once_calls >= 1:
                break
        assert engine_default.run_once_calls >= 1
        assert engine_tool.run_once_calls >= 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_hot_reload_snapshot_reads_injected_legacy_server(monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "1")
    server = ProxyServer(TeamEvolverConfig())
    injected = EvolveServerConfig(pg_tenant_id=DEFAULT_TENANT_ID)
    server._embedded_evolve_server = SimpleNamespace(config=injected)

    assert server._embedded_evolve_config_snapshot() is injected


@pytest.mark.anyio
async def test_hot_reload_detects_config_change_and_restarts_pool(monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "1")
    config = TeamEvolverConfig(sharing_viking_endpoint="https://ov.example")
    server = ProxyServer(config)
    server._embedded_evolve_server = SimpleNamespace(
        config=server._build_embedded_evolve_config(config)
    )
    calls: list[str] = []

    async def fake_stop(*, graceful: bool = False) -> None:
        calls.append("stop")

    monkeypatch.setattr(server, "_stop_embedded_evolve", fake_stop)
    monkeypatch.setattr(
        server, "_start_embedded_evolve", lambda: calls.append("start")
    )

    await server._reload_openviking_integrations(
        replace(config, sharing_viking_team_api_key="sk-next")
    )

    assert calls == ["stop", "start"]
    assert server._embedded_evolve_server is None
    assert server._engine_pool is None


# --------------------------------------------------------------------------- #
# Per-tenant quotas (plan Phase 3)                                             #
# --------------------------------------------------------------------------- #


def test_pool_quota_clamps_max_parallel_groups():
    class FakeRegistry:
        mode = "postgres"

        def get(self, tenant_id):
            return TenantContext(
                tenant_id=tenant_id,
                config_overrides={"max_concurrent_sessions": 2},
            )

    pool = EnginePool(
        TeamEvolverConfig(), _builder, registry_provider=lambda: FakeRegistry()
    )

    engine = pool.get("t_quota001")

    assert engine is not None
    assert engine.config.max_parallel_groups == 2


def test_pool_without_quota_keeps_default_parallelism():
    pool = EnginePool(TeamEvolverConfig(), _builder)

    engine = pool.get("t_quota002")

    assert engine is not None
    assert engine.config.max_parallel_groups == int(
        TeamEvolverConfig().evolve_max_parallel_groups
    )


def _make_quota_registry(quotas_by_tenant: dict):
    class _Reg:
        mode = "postgres"

        def get(self, tid):
            overrides = quotas_by_tenant.get(tid)
            if overrides is None:
                return None
            return TenantContext(tenant_id=tid, config_overrides=overrides)

        @property
        def runtime(self):
            return None

    return _Reg()


def _quota_pool(registry, tenant_ids):
    return SimpleNamespace(
        registry=lambda: registry,
        tenant_ids=lambda: list(tenant_ids),
    )


def test_quota_ordering_weighted_round_robin(monkeypatch):
    registry = _make_quota_registry(
        {
            "t_a": {"max_evolve_per_day": 4},
            "t_b": {"max_evolve_per_day": 2},
        }
    )
    server = _make_server(monkeypatch, registry=registry)
    pool = _quota_pool(registry, ["t_a", "t_b"])
    server._evolve_note_cycle("t_a")
    server._evolve_note_cycle("t_b")

    # Deficit ratios: t_a 1/4=0.25 < t_b 1/2=0.5 — the larger-quota tenant that
    # is further below its share is served first.
    assert server._quota_ordered_tenants(pool) == ["t_a", "t_b"]


def test_quota_daily_cap_defers_tenant_to_midnight(monkeypatch):
    import time as _time

    registry = _make_quota_registry({"t_b": {"max_evolve_per_day": 1}})
    server = _make_server(monkeypatch, registry=registry)
    pool = _quota_pool(registry, ["t_a", "t_b"])
    server._evolve_note_cycle("t_b")  # reaches the daily cap

    ordered = server._quota_ordered_tenants(pool)

    assert ordered == ["t_a"]
    assert server._evolve_next_due["t_b"] > _time.monotonic()  # deferred, not this tick


def test_quota_daily_counter_rolls_over_with_day(monkeypatch):
    server = _make_server(monkeypatch)
    server._evolve_daily["t_a"] = ("2000-01-01", 99)  # stale day

    assert server._evolve_cycle_count_today("t_a") == 0


def test_quota_registry_failure_falls_back_to_unlimited(monkeypatch):
    class ExplodingRegistry:
        mode = "postgres"

        def get(self, tid):
            raise RuntimeError("pg down")

    registry = ExplodingRegistry()
    server = _make_server(monkeypatch, registry=registry)
    pool = _quota_pool(registry, ["t_a"])

    assert server._quota_ordered_tenants(pool) == ["t_a"]
