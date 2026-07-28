from __future__ import annotations

from pathlib import Path

from teamEvolver.session_store import SessionStore
from teamEvolver.storage.local import LocalObjectStore


def test_session_store_archives_queue_and_filter_audit(tmp_path: Path) -> None:
    store = SessionStore(LocalObjectStore(tmp_path))
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
    assert store.filter_stats()["decisions"]["valuable"] == 1

    store._bucket.delete_object(store.queue_key("session-1"))

    assert store.list_queue() == []
    assert store.list_conversations()[0]["status"] == "consumed"


def test_session_store_skipped_sessions_only_write_filter_audit(tmp_path: Path) -> None:
    store = SessionStore(LocalObjectStore(tmp_path))
    store.save_skipped(
        {
            "session_id": "hello-1",
            "user_alias": "tester",
            "turns": [{"prompt_text": "谢谢"}],
            "value_judge": {"decision": "chitchat", "confidence": 0.9, "mode": "heuristic"},
        }
    )

    assert store.list_queue() == []
    assert store.list_conversations() == []
    assert store.list_filter_audit()[0]["session_id"] == "hello-1"
    assert store.filter_stats()["decisions"]["chitchat"] == 1
