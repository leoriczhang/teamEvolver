from __future__ import annotations

import hashlib
import json

from teamEvolver.dreamcycle.memory_changes import MemoryChangeLedger
from teamEvolver.storage.memory import InMemoryObjectStore
from teamEvolver.storage.snapshot import (
    SnapshotBlob,
    SnapshotConflictError,
)

OID_A = "a" * 40
OID_B = "b" * 40


class _Snapshot:
    def __init__(self) -> None:
        self.commits: list[dict] = []
        self.blobs = {
            (OID_A, "viking://user/memories/pattern/a.md"): b"old",
            (OID_B, "viking://user/memories/pattern/a.md"): b"new",
        }

    def commit(self, **kwargs):
        self.commits.append(kwargs)
        return {
            "result": "created",
            "commit_oid": OID_A if len(self.commits) == 1 else OID_B,
        }

    def show_blob(self, oid, *, path, raw):
        del raw
        content = self.blobs[(oid, path)]
        return SnapshotBlob(oid=oid, content=content)

    def diff(self, **kwargs):
        assert kwargs["raw"] is False
        return {
            "change_type": "modified",
            "diff_text": "-old\n+new\n",
        }


class _BrokenSnapshot:
    def commit(self, **_kwargs):
        raise SnapshotConflictError("CONFLICT", "branch changed")


def _ledger(snapshot, store) -> MemoryChangeLedger:
    ledger = MemoryChangeLedger(
        snapshot_client=snapshot,
        object_store=store,
        maintained_root="viking://user/memories",
        owner_user="team",
        account_hash="account-hash",
    )
    ledger.begin_round("dcr-test")
    ledger.begin_job("consolidate")
    return ledger


def test_memory_change_persists_snapshot_hashes_and_opaque_sources() -> None:
    snapshot = _Snapshot()
    store = InMemoryObjectStore()
    ledger = _ledger(snapshot, store)
    target = "viking://user/memories/pattern/a.md"
    personal = "viking://user/alice/memories/profile.md"

    token = ledger.prepare(
        action="update",
        target_paths=[target],
        source_refs=[target, personal],
        reason="cross-member evidence",
        before_path=target,
        after_path=target,
    )
    summary = ledger.finish(
        token,
        result="applied",
        metadata={"write_status": 200, "ignored": "not persisted"},
    )

    records = ledger.list_changes()
    assert len(records) == 1
    record = records[0]
    assert record["schema_version"] == "teamevolver.memory-change.v1"
    assert record["run_id"] == "dcr-test"
    assert record["job_name"] == "consolidate"
    assert record["before_oid"] == OID_A
    assert record["after_oid"] == OID_B
    assert record["before_hash"] == hashlib.sha256(b"old").hexdigest()
    assert record["after_hash"] == hashlib.sha256(b"new").hexdigest()
    assert record["diff_hash"] == hashlib.sha256(
        b"-old\n+new\n"
    ).hexdigest()
    assert record["snapshot_status"] == "complete"
    assert record["risk_level"] == "unclassified"
    assert record["metadata"] == {"write_status": 200}
    assert record["source_refs"][0]["uri"] == target
    assert record["source_refs"][1]["scope"] == "opaque_source"
    serialized = json.dumps(record, ensure_ascii=False)
    assert personal not in serialized
    assert summary["record_key"].startswith("memory-changes/")
    assert snapshot.commits[0]["paths"] == [target]
    assert snapshot.commits[1]["paths"] == [target]


def test_memory_change_records_snapshot_failure_without_losing_ledger() -> None:
    store = InMemoryObjectStore()
    ledger = _ledger(_BrokenSnapshot(), store)
    target = "viking://user/memories/pattern/a.md"

    token = ledger.prepare(
        action="update",
        target_paths=[target],
        before_path=target,
        after_path=target,
    )
    summary = ledger.finish(
        token,
        result="applied",
    )

    record = ledger.list_changes()[0]
    assert summary["snapshot_status"] == "failed"
    assert record["result"] == "applied"
    assert record["ledger_status"] == "persisted"
    assert [item["stage"] for item in record["snapshot_errors"]] == [
        "before",
        "after",
    ]
    assert all(
        item["code"] == "CONFLICT"
        for item in record["snapshot_errors"]
    )
