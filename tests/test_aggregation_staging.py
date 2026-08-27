from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from teamEvolver.aggregation.staging import (
    DeterministicStagingClient,
    StagingError,
    StagingInventory,
    StagingSource,
)


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def test_snapshot_copies_visible_memory_without_invoking_compile(monkeypatch) -> None:
    calls: list[tuple[str, str, dict, dict]] = []
    source_contents = {
        "viking://user/alice/memories/events/launch.md": "# Launch\n\nShip Friday.",
        "viking://user/alice/memories/profile.md": "# Profile\n\nPrefers concise docs.",
    }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params, headers):
            calls.append(("GET", url, params, headers))
            if url.endswith("/api/v1/fs/ls"):
                return _Response(
                    200,
                    {
                        "status": "ok",
                        "result": [
                            {
                                "uri": "viking://user/alice/memories/profile.md",
                                "rel_path": "profile.md",
                                "isDir": False,
                                "size": 23,
                                "modTime": "2026-08-26T00:00:00Z",
                            },
                            {
                                "uri": (
                                    "viking://user/alice/memories/events/launch.md"
                                ),
                                "rel_path": "events/launch.md",
                                "isDir": False,
                                "size": 22,
                                "modTime": "2026-08-27T00:00:00Z",
                            },
                            {
                                "uri": "viking://user/alice/memories/tools/cli.md",
                                "rel_path": "tools/cli.md",
                                "isDir": False,
                                "size": 10,
                                "modTime": "2026-08-27T00:00:00Z",
                            },
                        ],
                    },
                )
            if url.endswith("/api/v1/fs/stat"):
                return _Response(404, {"detail": "not found"})
            if url.endswith("/api/v1/content/read"):
                return _Response(
                    200,
                    {"status": "ok", "result": source_contents[params["uri"]]},
                )
            raise AssertionError(url)

        async def post(self, url, *, json, headers):
            calls.append(("POST", url, json, headers))
            return _Response(200, {"status": "ok", "result": {}})

        async def delete(self, url, *, params, headers):
            calls.append(("DELETE", url, params, headers))
            return _Response(200, {"status": "ok", "result": {}})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = DeterministicStagingClient(
        endpoint="http://openviking.example",
        account_id="default",
        source_user_id="alice",
        source_api_key="alice-secret",
        target_user_id="admin",
        target_api_key="admin-secret",
    )

    async def scenario():
        inventory = await client.inspect(["profile", "events"])
        snapshot = await client.publish(
            inventory,
            staging_uri=(
                "viking://user/admin/resources/teamEvolver/staging/"
                f"{inventory.fingerprint.removeprefix('sha256:')}"
            ),
            run_id="agg-test",
        )
        return inventory, snapshot

    inventory, snapshot = asyncio.run(scenario())

    assert [item.relative_path for item in inventory.files] == [
        "events/launch.md",
        "profile.md",
    ]
    assert snapshot.source_count == 2
    assert snapshot.chunk_count == 1
    assert "/bot/v1/compile" not in {call[1] for call in calls}

    source_calls = [
        call
        for call in calls
        if call[1].endswith("/api/v1/fs/ls")
        or call[1].endswith("/api/v1/content/read")
    ]
    target_calls = [call for call in calls if call not in source_calls]
    assert all(call[3]["X-API-Key"] == "alice-secret" for call in source_calls)
    assert all(call[3]["X-API-Key"] == "admin-secret" for call in target_calls)

    writes = [
        call[2]
        for call in calls
        if call[1].endswith("/api/v1/content/batch-write")
    ]
    snapshot_operation = next(
        operation
        for request in writes
        for operation in request["operations"]
        if operation["uri"].endswith(".jsonl")
    )
    records = [
        json.loads(line)
        for line in snapshot_operation["content"].splitlines()
    ]
    assert [record["source_uri"] for record in records] == [
        "viking://user/alice/memories/events/launch.md",
        "viking://user/alice/memories/profile.md",
    ]
    assert records[0]["content"] == source_contents[records[0]["source_uri"]]
    assert records[1]["content"] == source_contents[records[1]["source_uri"]]

    move = next(call for call in calls if call[1].endswith("/api/v1/fs/mv"))
    assert move[2]["to_uri"] == snapshot.uri
    assert "-pending-agg-test" in move[2]["from_uri"]


def test_inventory_fingerprint_is_stable_across_listing_order(monkeypatch) -> None:
    entries = [
        {
            "uri": "viking://user/alice/memories/events/b.md",
            "rel_path": "events/b.md",
            "isDir": False,
            "size": 2,
            "modTime": "2026-08-27T00:00:00Z",
        },
        {
            "uri": "viking://user/alice/memories/events/a.md",
            "rel_path": "events/a.md",
            "isDir": False,
            "size": 1,
            "modTime": "2026-08-26T00:00:00Z",
        },
    ]
    invocation = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url, *, params, headers):
            nonlocal invocation
            del params, headers
            invocation += 1
            ordered = entries if invocation == 1 else list(reversed(entries))
            return _Response(200, {"status": "ok", "result": ordered})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = DeterministicStagingClient(
        endpoint="http://openviking.example",
        account_id="default",
        source_user_id="alice",
        source_api_key="alice-secret",
        target_user_id="admin",
        target_api_key="admin-secret",
    )

    first = asyncio.run(client.inspect(["events"]))
    second = asyncio.run(client.inspect(["events"]))

    assert first.fingerprint == second.fingerprint
    assert first.files == second.files


def test_inventory_listing_is_non_recursive_and_time_bounded(monkeypatch) -> None:
    client_options: list[dict] = []
    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            del args
            client_options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url, *, params, headers):
            del headers
            calls.append(params)
            return _Response(200, {"status": "ok", "result": []})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = DeterministicStagingClient(
        endpoint="http://openviking.example",
        account_id="default",
        source_user_id="alice",
        source_api_key="alice-secret",
        target_user_id="admin",
        target_api_key="admin-secret",
        timeout_seconds=3000,
    )

    asyncio.run(client.inspect(["events"]))

    # A large memory tree must never be fetched via one recursive listing:
    # OpenViking has no pagination there and a deep tree walk 504s. Staging
    # descends with cheap non-recursive listings on a short, dedicated timeout.
    assert calls[0]["recursive"] == "false"
    assert client_options[0]["timeout"] == 60.0


def test_snapshot_discards_pending_copy_when_source_changes(monkeypatch) -> None:
    source = StagingSource(
        uri="viking://user/alice/memories/events/a.md",
        relative_path="events/a.md",
        kind="events",
        size=3,
        modified_at="2026-08-27T00:00:00Z",
    )
    inventory = StagingInventory(
        user_id="alice",
        source_root="viking://user/alice/memories",
        kinds=("events",),
        files=(source,),
        fingerprint="sha256:" + "a" * 64,
    )
    changed = StagingInventory(
        user_id=inventory.user_id,
        source_root=inventory.source_root,
        kinds=inventory.kinds,
        files=inventory.files,
        fingerprint="sha256:" + "b" * 64,
    )
    deleted: list[str] = []
    moved = False

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url, *, params, headers):
            del params, headers
            return _Response(200, {"status": "ok", "result": "body"})

    client = DeterministicStagingClient(
        endpoint="http://openviking.example",
        account_id="default",
        source_user_id="alice",
        source_api_key="alice-secret",
        target_user_id="admin",
        target_api_key="admin-secret",
    )

    async def missing(_uri):
        return False

    async def no_op(*_args, **_kwargs):
        return None

    async def inspect(_kinds):
        return changed

    async def delete(uri):
        deleted.append(uri)

    async def move(*_args, **_kwargs):
        nonlocal moved
        moved = True

    monkeypatch.setattr(client, "_client", lambda: FakeAsyncClient())
    monkeypatch.setattr(client, "snapshot_exists", missing)
    monkeypatch.setattr(client, "_mkdir", no_op)
    monkeypatch.setattr(client, "_batch_write", no_op)
    monkeypatch.setattr(client, "inspect", inspect)
    monkeypatch.setattr(client, "_delete_best_effort", delete)
    monkeypatch.setattr(client, "_move", move)

    with pytest.raises(StagingError, match="changed while"):
        asyncio.run(
            client.publish(
                inventory,
                staging_uri="viking://user/admin/resources/staging/alice",
                run_id="agg-test",
            )
        )

    assert moved is False
    assert deleted == [
        "viking://user/admin/resources/staging/alice-pending-agg-test"
    ]
