from __future__ import annotations

import asyncio
import threading
import time

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.llm import (
    LLMOverloadedError,
    _dispatcher_for,
    _reset_dispatchers_for_testing,
)
from teamEvolver.tenants.registry import TenantContext, effective_config
from team_skills.evolution import EvolveServerConfig


@pytest.fixture(autouse=True)
def reset_dispatchers():
    _reset_dispatchers_for_testing()
    yield
    _reset_dispatchers_for_testing()


def test_dispatcher_is_shared_within_tenant_and_isolated_across_tenants() -> None:
    tenant_a = _dispatcher_for("tenant-a", 2, 8)
    same_tenant = _dispatcher_for("tenant-a", 2, 8)
    tenant_b = _dispatcher_for("tenant-b", 2, 8)

    assert tenant_a is same_tenant
    assert tenant_a is not tenant_b


def test_tenants_use_their_model_concurrency_in_parallel() -> None:
    async def scenario() -> int:
        tenant_a = _dispatcher_for("tenant-a", 1, 4)
        tenant_b = _dispatcher_for("tenant-b", 1, 4)
        lock = threading.Lock()
        active = 0
        peak = 0

        def work() -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

        await asyncio.gather(
            tenant_a.call(work),
            tenant_b.call(work),
        )
        return peak

    assert asyncio.run(scenario()) == 2


def test_one_tenant_respects_its_own_concurrency_limit() -> None:
    async def scenario() -> int:
        dispatcher = _dispatcher_for("tenant-a", 2, 8)
        lock = threading.Lock()
        active = 0
        peak = 0

        def work() -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

        await asyncio.gather(*(dispatcher.call(work) for _ in range(6)))
        return peak

    assert asyncio.run(scenario()) == 2


def test_queue_overload_is_scoped_to_one_tenant() -> None:
    async def scenario() -> str:
        tenant_a = _dispatcher_for("tenant-a", 1, 1)
        tenant_b = _dispatcher_for("tenant-b", 1, 1)
        started = threading.Event()
        release = threading.Event()

        def blocking() -> str:
            started.set()
            release.wait(timeout=2)
            return "a"

        first = asyncio.create_task(tenant_a.call(blocking))
        await asyncio.to_thread(started.wait, 1)
        with pytest.raises(LLMOverloadedError, match="tenant-a"):
            await tenant_a.call(lambda: "overflow")

        result_b = await tenant_b.call(lambda: "b")
        release.set()
        result_a = await first
        return result_a + result_b

    assert asyncio.run(scenario()) == "ab"


def test_tenant_llm_limits_flow_into_evolution_config(monkeypatch) -> None:
    monkeypatch.delenv("EVOLVE_LLM_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("EVOLVE_LLM_QUEUE_CAPACITY", raising=False)
    base = TeamEvolverConfig()
    tenant = TenantContext(
        tenant_id="tenant-a",
        config_overrides={
            "llm_max_concurrency": 12,
            "llm_queue_capacity": 96,
        },
    )

    config = effective_config(None, tenant, base)
    evolve = EvolveServerConfig.from_teamEvolver_config(config)

    assert evolve.llm_max_concurrency == 12
    assert evolve.llm_queue_capacity == 96


def test_tenant_cycle_scheduler_is_unbounded_by_default(monkeypatch) -> None:
    from teamEvolver.proxy.server import ProxyServer

    monkeypatch.delenv("TEAMEVOLVER_TENANT_CONCURRENCY", raising=False)
    assert ProxyServer._tenant_cycle_limit() == 0

    monkeypatch.setenv("TEAMEVOLVER_TENANT_CONCURRENCY", "12")
    assert ProxyServer._tenant_cycle_limit() == 12


def test_model_settings_expose_per_tenant_queue_limits() -> None:
    from teamEvolver.proxy.routes import _model_settings_payload

    payload = _model_settings_payload(
        TeamEvolverConfig(
            llm_max_concurrency=10,
            llm_queue_capacity=80,
        ),
        {"llm": {}},
    )

    assert payload["max_concurrency"] == 10
    assert payload["queue_capacity"] == 80
