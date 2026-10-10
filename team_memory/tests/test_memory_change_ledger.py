from __future__ import annotations

import json

from team_memory.memory_changes import MemoryChangeLedger
from teamEvolver.storage.memory import InMemoryObjectStore
from teamEvolver.storage.snapshot import SnapshotBlob


class _Snapshot:
    def __init__(self) -> None:
        self.closed = False

    def show_blob(self, oid, *, path, raw):
        del raw
        return SnapshotBlob(oid=oid, content=f"{path}:{oid}".encode())

    def close(self) -> None:
        self.closed = True


def _ledger() -> tuple[MemoryChangeLedger, InMemoryObjectStore, _Snapshot]:
    store = InMemoryObjectStore()
    snapshot = _Snapshot()
    return (
        MemoryChangeLedger(snapshot_client=snapshot, object_store=store),
        store,
        snapshot,
    )


def test_historical_memory_changes_remain_readable() -> None:
    ledger, store, _snapshot = _ledger()
    older = {
        "schema_version": "teamevolver.memory-change.v1",
        "change_id": "mch-old",
        "completed_at": "2026-01-01T00:00:00+00:00",
    }
    newer = {
        "schema_version": "teamevolver.memory-change.v1",
        "change_id": "mch-new",
        "completed_at": "2026-01-02T00:00:00+00:00",
    }
    store.put_object("memory-changes/2026/01/mch-old.json", json.dumps(older).encode())
    store.put_object("memory-changes/2026/01/mch-new.json", json.dumps(newer).encode())

    assert [item["change_id"] for item in ledger.list_changes()] == [
        "mch-new",
        "mch-old",
    ]
    assert ledger.load_change("mch-old") == older


def test_snapshot_reads_and_replay_results_remain_supported() -> None:
    ledger, _store, snapshot = _ledger()

    assert ledger.read_snapshot_text(
        oid="a" * 40,
        path="viking://resources/team-memory/demo.md",
    ).endswith(":" + "a" * 40)

    saved = ledger.save_replay(
        {
            "change_id": "mch-old",
            "replay_id": "rpl-1",
            "completed_at": "2026-01-03T00:00:00+00:00",
        }
    )
    assert saved["record_key"] == "memory-replays/mch-old/rpl-1.json"
    assert ledger.list_replays(change_id="mch-old") == [saved]

    ledger.close()
    assert snapshot.closed is True
