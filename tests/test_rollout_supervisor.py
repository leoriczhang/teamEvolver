from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.integrations import skillopt_rollout


class LeaseBucket:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.acquire_calls = []
        self.release_calls = []

    def try_background_lock(self, namespace):
        self.acquire_calls.append(namespace)
        return self.acquired

    def release_background_lock(self, namespace):
        self.release_calls.append(namespace)


def _service(bucket):
    return SimpleNamespace(hub=SimpleNamespace(_bucket=bucket))


def _config(*, enabled=True, endpoint="http://upclaw.test"):
    return SimpleNamespace(
        skillopt_rollout_enabled=enabled,
        skillopt_rollout_endpoint=endpoint,
    )


def test_disabled_tenant_does_not_acquire_lease() -> None:
    bucket = LeaseBucket(True)

    result = asyncio.run(
        skillopt_rollout.rollout_tick_with_lease(
            _config(enabled=False),
            _service(bucket),
        )
    )

    assert result["state"] == "disabled"
    assert result["lease_supported"] is True
    assert result["lease_held"] is False
    assert bucket.acquire_calls == []


def test_standby_skips_tick_when_lease_is_held_elsewhere(monkeypatch) -> None:
    bucket = LeaseBucket(False)
    calls = []

    async def fake_tick(*_args, **_kwargs):
        calls.append(True)
        return {"synced": 0, "failed": 0, "attempted": 0}

    monkeypatch.setattr(skillopt_rollout, "rollout_tick", fake_tick)
    result = asyncio.run(
        skillopt_rollout.rollout_tick_with_lease(
            _config(),
            _service(bucket),
        )
    )

    assert result["state"] == "standby"
    assert calls == []
    assert bucket.release_calls == []


def test_leader_runs_tick_and_releases_lease(monkeypatch) -> None:
    bucket = LeaseBucket(True)

    async def fake_tick(*_args, **_kwargs):
        return {"synced": 1, "failed": 0, "attempted": 1}

    monkeypatch.setattr(skillopt_rollout, "rollout_tick", fake_tick)
    result = asyncio.run(
        skillopt_rollout.rollout_tick_with_lease(
            _config(),
            _service(bucket),
        )
    )

    assert result == {
        "synced": 1,
        "failed": 0,
        "attempted": 1,
        "state": "leader",
        "lease_supported": True,
        "lease_acquired": True,
        "lease_held": False,
    }
    assert bucket.acquire_calls == ["skillopt-rollout-v1"]
    assert bucket.release_calls == ["skillopt-rollout-v1"]
