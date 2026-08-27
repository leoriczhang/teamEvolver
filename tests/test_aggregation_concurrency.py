from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.aggregation.service import MemoryAggregationService


def test_compile_concurrency_is_shared_across_runs() -> None:
    service = MemoryAggregationService(
        SimpleNamespace(aggregation_phase1_concurrency=1)
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0
    active = 0
    max_active = 0

    class Client:
        async def run_batch(self, **_kwargs):
            nonlocal calls, active, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            try:
                if calls == 1:
                    first_started.set()
                    await release_first.wait()
                return {"ok": True}
            finally:
                active -= 1

    async def scenario() -> None:
        client = Client()
        first = asyncio.create_task(service._run_compile(client))
        await first_started.wait()
        second = asyncio.create_task(service._run_compile(client))
        await asyncio.sleep(0.05)
        assert calls == 1
        release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())

    assert calls == 2
    assert max_active == 1
