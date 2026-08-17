from __future__ import annotations

from pathlib import Path

from teamEvolver.storage import InMemoryObjectStore
from teamEvolver.validation.store import ValidationStore


def _store(tmp_path: Path) -> ValidationStore:
    return ValidationStore.from_bucket(bucket=InMemoryObjectStore(str(tmp_path)))


def test_fresh_evaluation_reused_when_revision_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_job({"job_id": "job-1", "candidate_revision": 1})
    store.save_evaluation("job-1", {"score": 0.9})

    fresh = store.load_fresh_evaluation("job-1")

    assert fresh is not None
    assert fresh["score"] == 0.9
    # save_evaluation stamps the job's revision so later staleness checks work.
    assert fresh["candidate_revision"] == 1


def test_fresh_evaluation_discarded_when_revision_advanced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_job({"job_id": "job-1", "candidate_revision": 1})
    store.save_evaluation("job-1", {"score": 0.9})

    # The candidate content was revised (e.g. re-generated / merged) under the
    # same job_id; the cached evaluation is now stale and must not be reused.
    store.save_job({"job_id": "job-1", "candidate_revision": 2})

    assert store.load_fresh_evaluation("job-1") is None
    # The raw cache still exists — only the freshness-guarded read rejects it.
    assert store.load_evaluation("job-1") is not None


def test_legacy_evaluation_without_revision_is_not_falsely_stale(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A job and evaluation predating the revision field both default to 1.
    store.save_job({"job_id": "job-legacy"})
    store._bucket.put_object(
        store._evaluation_key("job-legacy"),
        b'{"job_id": "job-legacy", "score": 0.8}',
    )

    assert store.load_fresh_evaluation("job-legacy") is not None


def test_decision_index_reconciles_to_latest_decision_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_job({"job_id": "job-1", "candidate_skill_name": "skill-a"})
    store.save_evaluation("job-1", {"score": 0.9})

    # First publish → index row at v2.
    store.save_decision(
        "job-1",
        {"status": "published", "version": 2, "decided_at": "2026-08-04T00:00:00+00:00"},
    )
    # Simulate stale-index drift: the per-job decision file advances to v7 but
    # the index row is left behind at v2.
    store._bucket.put_object(
        store._decision_key("job-1"),
        b'{"status": "published", "version": 7, "decided_at": "2026-08-10T00:00:00+00:00"}',
    )

    records = store.list_decision_records()
    row = next(r for r in records if r.get("job_id") == "job-1")

    assert row["decision"]["version"] == 7
    assert row["decided_at"] == "2026-08-10T00:00:00+00:00"
