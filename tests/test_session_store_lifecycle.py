from __future__ import annotations

import json
from pathlib import Path

from teamEvolver.session_store import SessionStore
from teamEvolver.storage import InMemoryObjectStore


def test_session_store_archives_queue_and_filter_audit(tmp_path: Path) -> None:
    store = SessionStore(InMemoryObjectStore(str(tmp_path)))
    session = {
        "session_id": "session-1",
        "user_alias": "tester",
        "title": "useful flow",
        "turns": [{"prompt_text": "整理流程"}],
        "metrics": {"tool_call_count": 1, "total_tokens": 100},
        "value_judge": {"decision": "valuable", "confidence": 0.8, "mode": "heuristic"},
    }

    key = store.save_queued(session)

    assert key == "sessions/session-1.json"
    assert store.list_queue()[0]["session_id"] == "session-1"
    assert store.list_conversations()[0]["status"] == "queued"
    assert store.conversation_statuses(["session-1"]) == {"session-1": "queued"}
    assert store.filter_stats()["decisions"]["valuable"] == 1

    store._bucket.delete_object(store.queue_key("session-1"))

    assert store.list_queue() == []
    assert store.list_conversations()[0]["status"] == "consumed"
    assert store.conversation_statuses(["session-1", "missing"]) == {
        "session-1": "consumed",
        "missing": "unknown",
    }


def test_session_store_skipped_sessions_are_archived_for_review(tmp_path: Path) -> None:
    store = SessionStore(InMemoryObjectStore(str(tmp_path)))
    store.save_skipped(
        {
            "session_id": "hello-1",
            "user_alias": "tester",
            "turns": [{"prompt_text": "谢谢"}],
            "value_judge": {"decision": "chitchat", "confidence": 0.9, "mode": "heuristic"},
        }
    )

    assert store.list_queue() == []
    assert store.list_conversations()[0]["status"] == "skipped"
    assert store.load_session("hello-1")["turns"][0]["prompt_text"] == "谢谢"
    assert store.list_filter_audit()[0]["session_id"] == "hello-1"
    assert store.filter_stats()["decisions"]["chitchat"] == 1


class _CountingObjectStore:
    """Wrapper that counts get_object calls to assert bounded fallback."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.reads = 0

    def get_object(self, key):
        self.reads += 1
        return self._inner.get_object(key)

    def put_object(self, key, data):
        return self._inner.put_object(key, data)

    def delete_object(self, key):
        return self._inner.delete_object(key)

    def iter_objects(self, prefix=""):
        return self._inner.iter_objects(prefix=prefix)


def _make_session(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "user_alias": "tester",
        "title": f"session {session_id}",
        "turns": [{"prompt_text": "hello"}],
        "metrics": {"tool_call_count": 0, "total_tokens": 1},
    }


def test_list_queue_missing_index_keys_does_not_trigger_full_scan(tmp_path: Path) -> None:
    """A few index misses must download only the missing objects, never all.

    Regression: the previous fast path required full index coverage and fell
    back to downloading every queued session when even one key was missing,
    stalling the dashboard for minutes against slow remote stores.
    """
    counting = _CountingObjectStore(InMemoryObjectStore(str(tmp_path)))
    store = SessionStore(counting)
    session_ids = [f"session-{i}" for i in range(5)]
    for session_id in session_ids:
        store.save_queued(_make_session(session_id))

    # Simulate index drift: drop two sessions from session_index.json.
    index_key = store.session_index_key()
    rows = [r for r in json.loads(counting._inner.get_object(index_key).read())
            if r.get("session_id") not in {"session-1", "session-3"}]
    counting._inner.put_object(index_key, json.dumps(rows).encode())

    counting.reads = 0
    queued = store.list_queue()

    assert [r["session_id"] for r in queued].sort() == session_ids.sort()
    # 1 index load + 2 missing sessions + 1 index reload inside the merge
    # write. A full scan would have read every queued session object too.
    assert counting.reads == 4

    # The index is repaired in the same pass, so the next call is index-only.
    repaired = {r["session_id"] for r in json.loads(counting._inner.get_object(index_key).read())}
    assert set(session_ids) <= repaired
    counting.reads = 0
    assert len(store.list_queue()) == 5
    assert counting.reads == 1
