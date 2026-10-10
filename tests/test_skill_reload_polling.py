import asyncio

import pytest

from teamEvolver.proxy.uploads import UploadsMixin


@pytest.mark.asyncio
async def test_skill_runtime_cache_refresh_starts_immediately_then_uses_fixed_interval(
    monkeypatch,
):
    poller = UploadsMixin()
    poller._skill_reload_task = None
    poller._task_done_cb = lambda _task: None
    pulls = 0
    delays = []

    async def pull_all_tenant_skill_caches():
        nonlocal pulls
        pulls += 1
        if pulls == 2:
            raise asyncio.CancelledError

    async def sleep(delay):
        delays.append(delay)

    poller._pull_all_tenant_skill_caches = pull_all_tenant_skill_caches
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr("teamEvolver.proxy.uploads.random.uniform", lambda _start, _end: 0)

    poller._start_skill_reload_polling()
    with pytest.raises(asyncio.CancelledError):
        await poller._skill_reload_task

    assert pulls == 2
    assert delays == [30]
