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


def _server(tmp_path: Path) -> ProxyServer:
    return ProxyServer(
        config=TeamEvolverConfig(
            sharing_enabled=True,
            sharing_backend="local",
            sharing_session_backend="local",
            sharing_local_root=str(tmp_path),
            sharing_skill_reload_mode="off",
            llm_api_key="",
        )
    )


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
    assert not (tmp_path / "sessions" / "hello-1.json").exists()


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
    stored = json.loads((tmp_path / "sessions" / "valuable-1.json").read_text("utf-8"))
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
