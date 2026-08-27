from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from teamEvolver.aggregation.service import (
    AggregationRun,
    GroupResult,
    MemoryAggregationService,
)
from teamEvolver.aggregation.staging import DeterministicStagingClient
from teamEvolver.aggregation.state import AggregationState


def test_compile_concurrency_is_shared_across_runs() -> None:
    service = MemoryAggregationService(
        SimpleNamespace(aggregation_merge_concurrency=1)
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


def test_phase_one_uses_bounded_workers_for_ten_thousand_users(
    tmp_path,
    monkeypatch,
) -> None:
    concurrency = 4
    service = MemoryAggregationService(
        SimpleNamespace(
            aggregation_phase1_concurrency=concurrency,
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            sharing_viking_endpoint="http://openviking.example",
        )
    )
    run = service.new_run("default")
    state = AggregationState(tmp_path / "state.json", "default")
    started = 0
    workers_started = asyncio.Event()
    release_workers = asyncio.Event()

    class StagingClient:
        def __init__(self, **_kwargs):
            pass

        async def inspect(self, _kinds):
            nonlocal started
            started += 1
            if started == concurrency:
                workers_started.set()
            await release_workers.wait()
            return SimpleNamespace(files=(), fingerprint="sha256:" + "0" * 64)

    monkeypatch.setattr(
        "teamEvolver.aggregation.service.DeterministicStagingClient",
        StagingClient,
    )

    async def scenario() -> int:
        pipeline = asyncio.create_task(
            service._run_pipeline(
                run=run,
                users=[f"user-{index:05d}" for index in range(10_000)],
                user_api_keys={
                    f"user-{index:05d}": "secret"
                    for index in range(10_000)
                },
                target_user_id="team",
                target_api_key="team-secret",
                kinds=["events"],
                endpoint="http://openviking.example",
                agent_id="team-skill-evolver",
                state=state,
                force_all=False,
            )
        )
        await workers_started.wait()
        pending = sum(not task.done() for task in asyncio.all_tasks())
        pipeline.cancel()
        try:
            await pipeline
        except asyncio.CancelledError:
            pass
        return pending

    pending_tasks = asyncio.run(scenario())

    assert started == concurrency
    assert pending_tasks < 100


def test_staging_inventory_walks_deep_memory_without_recursive_listing(
    monkeypatch,
) -> None:
    """A large/deep memory tree is enumerated with cheap non-recursive listings.

    A single recursive whole-tree ``ls`` is what makes OpenViking time out
    (504) on users with real memory, because that endpoint has no pagination
    and a high ``node_limit`` forces a deep tree walk. Staging must instead
    descend directory-by-directory so arbitrarily large memory is collected in
    full, without ever issuing a recursive listing and without truncation.
    """
    calls = []

    # Immediate children per directory URI (non-recursive listing shape).
    tree = {
        "viking://user/alice/memories": [
            {"uri": "viking://user/alice/memories/profile.md", "isDir": False,
             "size": 10, "modTime": "2026-08-01T00:00:00Z"},
            {"uri": "viking://user/alice/memories/events", "isDir": True},
            {"uri": "viking://user/alice/memories/secrets", "isDir": True},
        ],
        "viking://user/alice/memories/events": [
            {"uri": "viking://user/alice/memories/events/launch.md", "isDir": False,
             "size": 20, "modTime": "2026-08-02T00:00:00Z"},
            {"uri": "viking://user/alice/memories/events/2026", "isDir": True},
        ],
        "viking://user/alice/memories/events/2026": [
            {"uri": "viking://user/alice/memories/events/2026/q1.md", "isDir": False,
             "size": 30, "modTime": "2026-08-03T00:00:00Z"},
        ],
        # Not a requested kind; must never be listed or collected.
        "viking://user/alice/memories/secrets": [
            {"uri": "viking://user/alice/memories/secrets/key.md", "isDir": False,
             "size": 40, "modTime": "2026-08-04T00:00:00Z"},
        ],
    }

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self.is_success = True
            self._payload = payload

        def json(self):
            return {"status": "ok", "result": self._payload}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params, headers):
            calls.append((url, dict(params), headers))
            return Response(tree.get(params["uri"], []))

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = DeterministicStagingClient(
        endpoint="http://openviking.example",
        account_id="default",
        source_user_id="alice",
        source_api_key="secret",
        target_user_id="team",
        target_api_key="team-secret",
    )

    inventory = asyncio.run(client.inspect(["profile", "events"]))

    # Deep files are fully collected, not just the first directory level.
    assert [source.relative_path for source in inventory.files] == [
        "events/2026/q1.md",
        "events/launch.md",
        "profile.md",
    ]
    assert inventory.fingerprint.startswith("sha256:")
    # Every listing is non-recursive (the fix), regardless of tree depth.
    assert calls, "inspect issued no listing calls"
    assert all(call[1]["recursive"] == "false" for call in calls)
    listed_uris = {call[1]["uri"] for call in calls}
    assert "viking://user/alice/memories/events/2026" in listed_uris
    # Unrequested top-level kinds are never descended into.
    assert "viking://user/alice/memories/secrets" not in listed_uris


def test_run_status_details_are_bounded_for_large_accounts() -> None:
    service = MemoryAggregationService(
        SimpleNamespace(
            aggregation_phase1_concurrency=1,
            aggregation_run_detail_limit=100,
        )
    )
    run = AggregationRun(
        task_id="agg-large",
        account_id="default",
        endpoint="http://openviking.example",
        auth_mode="trusted",
        target_uri="viking://resources/team-memory",
    )

    for index in range(10_000):
        service._append_group(
            run,
            GroupResult(
                group_key=f"stage:user-{index}",
                kind="(all)",
                target_uri=f"viking://resources/staging/user-{index}",
                source_count=1,
                status="skipped",
            ),
        )
    service._append_group(
        run,
        GroupResult(
            group_key="stage:failed-user",
            kind="(all)",
            target_uri="viking://resources/staging/failed-user",
            source_count=1,
            status="failed",
        ),
    )

    public = run.to_public()
    assert public["group_total"] == 10_001
    assert public["group_counts"] == {
        "ok": 0,
        "skipped": 10_000,
        "failed": 1,
    }
    assert public["groups_truncated"] is True
    assert len(public["groups"]) == 100
    assert any(group["status"] == "failed" for group in public["groups"])
