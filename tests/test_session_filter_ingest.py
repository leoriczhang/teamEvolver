from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.session_filter import heuristic_classify_session


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
