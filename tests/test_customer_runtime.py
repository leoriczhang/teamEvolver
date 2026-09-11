"""Customer deployment regressions, independent of customer network services."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.llm import AsyncLLMClient
from teamEvolver.proxy.engine_pool import EnginePool
from teamEvolver.storage.pg_pool import PgRuntime
from teamEvolver.tenants.registry import TenantContext, TenantRegistry, effective_config


def test_registry_exposes_real_cycle_runtime():
    runtime = object()
    assert TenantRegistry(runtime=runtime).runtime is runtime


def test_pg_enabled_without_dsn_fails_closed(monkeypatch):
    monkeypatch.delenv("OV_PG_HOST", raising=False)
    with pytest.raises(ValueError, match="DSN"):
        TenantRegistry(TeamEvolverConfig(storage_pg_enabled=True))


def test_tenant_config_cannot_inherit_another_accounts_storage():
    base = TeamEvolverConfig(storage_pg_enabled=True, sharing_viking_account="other-account")
    ctx = TenantContext(tenant_id="account-a")
    config = effective_config(None, ctx, base)
    assert config.sharing_viking_account == "account-a"
    assert config.sharing_session_backend == "postgres"
    assert config.sharing_skill_backend == "postgres"
    assert config.sharing_local_fallback_enabled is False


def test_concurrent_first_requests_build_one_engine(monkeypatch):
    constructed = []

    class Engine:
        def __init__(self, config):
            time.sleep(0.03)
            constructed.append(self)
            self.config = config

    monkeypatch.setattr("teamEvolver.evolve.EvolveServer", Engine)
    pool = EnginePool(object(), lambda _: EvolveServerConfig())
    with ThreadPoolExecutor(max_workers=12) as executor:
        engines = list(executor.map(pool.get, ["account-a"] * 12))
    assert len(constructed) == 1
    assert all(engine is engines[0] for engine in engines)


def test_pg_timeout_cancels_pending_work():
    runtime = PgRuntime(dsn="postgresql://unused@127.0.0.1:1/unused")
    cancelled = threading.Event()

    async def work():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    try:
        with pytest.raises(TimeoutError):
            runtime.run(work(), timeout=0.02)
        assert cancelled.wait(0.5), "timed-out database work continued in background"
    finally:
        runtime.close()


def test_llm_budget_shared_across_tenants(monkeypatch):
    active = peak = 0
    guard = threading.Lock()

    def complete(**kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=None,
        )

    clients = [AsyncLLMClient(api_key="test", max_retries=1) for _ in range(3)]
    for client in clients:
        monkeypatch.setattr(client._client.chat.completions, "create", complete)

    async def run():
        return await asyncio.gather(*(clients[i % 3].chat([{"role": "user", "content": "test"}]) for i in range(40)))

    assert set(asyncio.run(run())) == {"ok"}
    assert peak <= 8, f"observed {peak} simultaneous LLM requests"


def test_llm_accepts_legacy_full_url_and_bearer_token():
    client = AsyncLLMClient(
        api_key="Bearer test-token",
        base_url="https://model.example/gateway/v1/chat/completions",
    )
    assert str(client._client.base_url) == "https://model.example/gateway/v1/"
    assert client._client.api_key == "test-token"


def test_customer_model_cap_and_legacy_token_parameter(monkeypatch):
    import httpx
    from openai import BadRequestError

    monkeypatch.setenv("TEAMEVOLVER_LLM_MAX_OUTPUT_TOKENS", "8192")
    client = AsyncLLMClient(api_key="test", max_retries=1)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        if "max_completion_tokens" in kwargs:
            response = httpx.Response(
                400, text="unsupported max_completion_tokens", request=httpx.Request("POST", "http://model.test")
            )
            raise BadRequestError("unsupported", response=response, body={})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")], usage=None
        )

    monkeypatch.setattr(client._client.chat.completions, "create", complete)
    assert asyncio.run(client.chat([{"role": "user", "content": "test"}], max_tokens=100000)) == "ok"
    assert len(calls) == 2
    assert calls[0]["max_completion_tokens"] == calls[1]["max_tokens"] == 8192


def test_unconfigured_model_never_sends_session(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = AsyncLLMClient()
    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(client.chat([{"role": "user", "content": "private session"}]))
