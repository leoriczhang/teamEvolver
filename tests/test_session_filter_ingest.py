from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.session_filter import (
    SessionValueClassifier,
    heuristic_classify_session,
)
from teamEvolver.session_store import SessionStore
from teamEvolver.storage import is_not_found_error


def _config(tmp_path: Path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_session_backend="viking",
        sharing_viking_endpoint="memory://" + str(tmp_path),
        sharing_skill_reload_mode="off",
        llm_api_key="",
    )


def _server(tmp_path: Path) -> ProxyServer:
    return ProxyServer(config=_config(tmp_path))


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore.from_config(_config(tmp_path))


def _queued_exists(store: SessionStore, session_id: str) -> bool:
    try:
        store._bucket.get_object(store.queue_key(session_id))
        return True
    except Exception as exc:
        if is_not_found_error(exc):
            return False
        raise


def test_ingest_skips_chitchat_sessions(tmp_path: Path) -> None:
    app = _server(tmp_path).app

    with TestClient(app) as client:
        resp = client.post(
            "/ingest_session",
            json={
                "session_id": "hello-1",
                "messages": [{"role": "user", "content": "谢谢"}],
                "turns": [{"prompt_text": "谢谢"}],
                "metrics": {"tool_call_count": 0},
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "skipped"
    assert body["queued"] is False
    assert not _queued_exists(_store(tmp_path), "hello-1")


def test_injected_skills_alone_do_not_make_chitchat_valuable() -> None:
    result = heuristic_classify_session(
        {
            "turns": [{"prompt_text": "谢谢"}],
            "injected_skills": ["text-translation", "glossary"],
            "metrics": {"tool_call_count": 0},
        }
    )

    assert result["decision"] == "chitchat"


def test_incomplete_task_discussion_is_task_only() -> None:
    result = heuristic_classify_session(
        {
            "turns": [
                {"prompt_text": "生成红楼梦沉浸式介绍 PPT"},
                {"prompt_text": "继续"},
            ],
            "metrics": {"tool_call_count": 0},
        }
    )

    assert result["decision"] == "task_only"
    assert result["memory_candidates"] == []


def test_controlled_managed_eval_feedback_is_valuable() -> None:
    result = heuristic_classify_session(
        {
            "defer_evolution_trigger": True,
            "turns": [
                {"prompt_text": "制作一个演示稿"},
                {"prompt_text": "请沉淀生成后校验产物的共性流程"},
            ],
            "metrics": {"tool_call_count": 0},
        }
    )

    assert result["decision"] == "valuable"


@pytest.mark.anyio
async def test_verified_candidate_audit_bypasses_subjective_classifier() -> None:
    class FailingClient:
        async def chat(self, *_args, **_kwargs):
            raise AssertionError("deterministic audit must not call the LLM")

    result = await SessionValueClassifier(client=FailingClient()).classify(
        {
            "runtime_context": {
                "candidate_job_id": "job-1",
                "candidate_sha256": "a" * 64,
            },
            "turns": [
                {
                    "prompt_text": "Audit the candidate.",
                    "tool_results": [
                        {
                            "tool_name": "candidate_skill_gap_report",
                            "has_error": False,
                            "content": '{"passed": false}',
                        }
                    ],
                }
            ],
        }
    )

    assert result["decision"] == "valuable"
    assert result["confidence"] == 1.0
    assert result["mode"] == "deterministic"


def test_ingest_queues_valuable_sessions(tmp_path: Path) -> None:
    app = _server(tmp_path).app

    with TestClient(app) as client:
        resp = client.post(
            "/ingest_session",
            json={
                "session_id": "valuable/1",
                "user_alias": "tester",
                "turns": [
                    {
                        "prompt_text": "帮我整理这个接口调用流程并生成可复用步骤",
                        "tool_calls": [{"function": {"name": "terminal", "arguments": "{}"}}],
                    }
                ],
                "metrics": {"tool_call_count": 1},
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["queued"] is True
    store = _store(tmp_path)
    stored = json.loads(
        store._bucket.get_object(store.queue_key("valuable-1")).read().decode("utf-8")
    )
    assert stored["session_id"] == "valuable-1"
    assert stored["value_judge"]["decision"] == "valuable"


def test_managed_eval_train_session_queues_without_auto_trigger(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    app = server.app
    scheduled = 0

    def fake_schedule() -> bool:
        nonlocal scheduled
        scheduled += 1
        return True

    server._schedule_evolve_trigger = fake_schedule  # type: ignore[method-assign]
    with TestClient(app) as client:
        resp = client.post(
            "/ingest_session",
            json={
                "session_id": "managed-eval-train-1",
                "defer_evolution_trigger": True,
                "turns": [
                    {
                        "prompt_text": "制作一份演示稿",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "general_skill.pptx-deck-builder",
                                    "arguments": "{}",
                                }
                            }
                        ],
                    }
                ],
                "metrics": {"tool_call_count": 1},
            },
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["trigger_scheduled"] is False
    assert scheduled == 0


def test_reingesting_unchanged_processed_session_is_skipped(tmp_path: Path) -> None:
    """A consumed session re-submitted with identical content must not re-queue."""
    server = _server(tmp_path)
    app = server.app
    payload = {
        "session_id": "dup-1",
        "user_alias": "tester",
        "turns": [
            {
                "prompt_text": "帮我整理这个接口调用流程并生成可复用步骤",
                "tool_calls": [{"function": {"name": "terminal", "arguments": "{}"}}],
            }
        ],
        "metrics": {"tool_call_count": 1},
    }

    with TestClient(app) as client:
        first = client.post("/ingest_session", json=payload)
        assert first.json()["status"] == "queued"

        # Simulate the external evolve engine consuming the session: the queue
        # entry is removed while the archive copy remains.
        store = _store(tmp_path)
        store._bucket.delete_object(store.queue_key("dup-1"))
        assert store._bucket.get_object(store.archive_key("dup-1"))

        again = client.post("/ingest_session", json=payload)

    body = again.json()
    assert body["status"] == "duplicate"
    assert body["queued"] is False
    # Not re-queued.
    assert not _queued_exists(_store(tmp_path), "dup-1")


def test_reingesting_continued_session_with_new_turn_requeues(tmp_path: Path) -> None:
    """A genuinely continued conversation (new turn) is ingested again."""
    server = _server(tmp_path)
    app = server.app
    base_turn = {
        "prompt_text": "帮我整理这个接口调用流程并生成可复用步骤",
        "tool_calls": [{"function": {"name": "terminal", "arguments": "{}"}}],
    }

    with TestClient(app) as client:
        first = client.post(
            "/ingest_session",
            json={
                "session_id": "cont-1",
                "user_alias": "tester",
                "turns": [base_turn],
                "metrics": {"tool_call_count": 1},
            },
        )
        assert first.json()["status"] == "queued"
        store = _store(tmp_path)
        store._bucket.delete_object(store.queue_key("cont-1"))

        again = client.post(
            "/ingest_session",
            json={
                "session_id": "cont-1",
                "user_alias": "tester",
                "turns": [
                    base_turn,
                    {
                        "prompt_text": "再补一个步骤：把结果落地成脚本",
                        "tool_calls": [{"function": {"name": "terminal", "arguments": "{}"}}],
                    },
                ],
                "metrics": {"tool_call_count": 2},
            },
        )

    assert again.json()["status"] == "queued"
    assert _queued_exists(_store(tmp_path), "cont-1")
