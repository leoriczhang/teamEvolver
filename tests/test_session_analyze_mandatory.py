"""Regression tests for mandatory ingest-time Session analysis."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from session_ingestion.service import _classify_session
from team_skills.evolution import prompt_studio
from team_skills.evolution.runtime.orchestrator import EvolveServer
from team_skills.evolution.session_filter import SessionValueClassifier
from team_skills.evolution.session_judge_queue import AsyncSessionJudgeQueue
from team_skills.evolution.stages.analyze import SessionAnalysisError, session_has_merged_outputs
from team_skills.evolution.stages.trace_analyze import TraceAnalysisError
from teamEvolver.session_store import SessionStore
from teamEvolver.storage.local import LocalObjectStore


def _analysis_reply() -> str:
    return "\n".join(
        [
            "<classification>",
            json.dumps(
                {
                    "decision": "valuable",
                    "confidence": 0.9,
                    "reason": "模型已完成完整分析",
                },
                ensure_ascii=False,
            ),
            "</classification>",
            "<summary>会话包含明确任务、执行过程和用户反馈，已完成完整分析。</summary>",
            "<judge>",
            json.dumps(
                {
                    "task_completion": 0.8,
                    "response_quality": 0.8,
                    "efficiency": 0.7,
                    "tool_usage": 0.9,
                    "overall_score": 0.8,
                    "evolution_evidence": "exemplary",
                    "evidence_reason": "用户提供了明确反馈。",
                    "reasons": {
                        "task_completion": ["任务已完成。"],
                        "response_quality": ["结果清晰。"],
                        "efficiency": ["执行路径直接。"],
                        "tool_usage": ["工具调用正确。"],
                    },
                    "rationale": "整体完成良好。",
                },
                ensure_ascii=False,
            ),
            "</judge>",
        ]
    )


class _FakeClient:
    model = "test-model"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.reply


class _BrokenClient:
    model = "test-model"

    async def chat(self, messages, **kwargs) -> str:
        raise RuntimeError("session analyze unavailable")


def _session(*, candidate_audit: bool = False) -> dict:
    session = {
        "session_id": "sess-mandatory",
        "defer_evolution_trigger": True,
        "turns": [
            {
                "prompt_text": "请执行候选技能",
                "response_text": "已执行",
                "tool_calls": [],
                "tool_results": [],
            },
            {
                "prompt_text": "结果符合预期",
                "response_text": "收到",
                "tool_calls": [],
                "tool_results": [],
            },
        ],
    }
    if candidate_audit:
        session["runtime_context"] = {
            "candidate_job_id": "job-1",
            "candidate_sha256": "sha-1",
        }
        session["turns"][-1]["tool_results"] = [
            {
                "tool_name": "candidate_skill_gap_report",
                "has_error": False,
                "content": "success",
            }
        ]
    return session


def test_explicit_user_feedback_cannot_bypass_session_analysis(monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_MERGED_ANALYSIS", "0")
    client = _FakeClient(_analysis_reply())
    classifier = SessionValueClassifier(client=client)
    session = _session()

    result = asyncio.run(_classify_session(session, classifier))

    assert len(client.calls) == 1
    assert result["mode"] == "merged"
    assert result["reason"] != (
        "controlled managed-agent training session contains explicit user feedback"
    )
    assert session["_summary"]
    assert session["_judge_scores"]["overall_score"] == pytest.approx(0.805)


def test_verified_candidate_audit_cannot_bypass_session_analysis():
    client = _FakeClient(_analysis_reply())
    classifier = SessionValueClassifier(client=client)

    result = asyncio.run(_classify_session(_session(candidate_audit=True), classifier))

    assert len(client.calls) == 1
    assert result["mode"] == "merged"


def test_missing_session_analysis_client_fails_closed():
    classifier = SessionValueClassifier(client=None)

    with pytest.raises(RuntimeError, match="Session Analyze"):
        asyncio.run(_classify_session(_session(), classifier))


def test_session_analysis_llm_failure_fails_closed():
    classifier = SessionValueClassifier(client=_BrokenClient())

    with pytest.raises(RuntimeError, match="session analyze unavailable"):
        asyncio.run(_classify_session(_session(), classifier))


def test_incomplete_session_analysis_fails_closed():
    reply = (
        '<classification>{"decision":"valuable","confidence":0.9,'
        '"reason":"只有分类"}</classification>'
    )
    classifier = SessionValueClassifier(client=_FakeClient(reply))

    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(_classify_session(_session(), classifier))


def test_prompt_studio_connection_can_supply_mandatory_analysis_client(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "TEAMEVOLVER_STAGE_SETTINGS_PATH",
        str(tmp_path / "stage-settings.json"),
    )
    config = SimpleNamespace(
        llm_api_key="",
        llm_api_base="",
        llm_model_id="",
        model_name="",
    )
    prompt_studio.set_stage_settings(
        "analyze_session",
        {
            "provider": "custom",
            "base_url": "https://stage.example/v1",
            "model": "stage-model",
            "api_key": "stage-secret",
            "temperature": 0.1,
            "max_tokens": 32768,
        },
        config=config,
    )

    classifier = SessionValueClassifier.from_config(config)

    assert classifier.client is not None
    assert classifier.client.model == "stage-model"


def test_evolution_backfills_missing_session_analysis():
    client = _FakeClient(_analysis_reply())
    owner = SimpleNamespace(
        config=SimpleNamespace(use_session_judge=True),
        _llm=client,
    )
    session = _session()

    judged = asyncio.run(EvolveServer._prepare_sessions(owner, [session]))

    assert judged == 1
    assert len(client.calls) == 1
    assert session["_summary"]
    assert session["_judge_scores"]["overall_score"] == pytest.approx(0.805)


@pytest.mark.parametrize("bad_classification", ["not json", "null", '{"decision":"skip"}'])
def test_invalid_classification_is_an_analysis_error(bad_classification):
    reply = _analysis_reply()
    start = reply.index("<classification>")
    end = reply.index("</classification>") + len("</classification>")
    reply = reply[:start] + f"<classification>{bad_classification}</classification>" + reply[end:]
    session = _session()
    with pytest.raises(SessionAnalysisError, match="classification"):
        asyncio.run(_classify_session(session, SessionValueClassifier(client=_FakeClient(reply))))
    assert "_judge_scores" not in session
    assert "_summary" not in session


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_model_scores_are_rejected(value):
    reply = _analysis_reply().replace('"task_completion": 0.8', f'"task_completion": {json.dumps(value)}')
    session = _session()
    with pytest.raises(SessionAnalysisError, match="judge"):
        asyncio.run(_classify_session(session, SessionValueClassifier(client=_FakeClient(reply))))
    assert "_judge_scores" not in session


def test_legacy_score_cannot_bypass_model_analysis():
    session = _session()
    session["_summary"] = "legacy summary"
    session["_judge_scores"] = {"overall_score": 0.9}
    assert not session_has_merged_outputs(session)
    client = _FakeClient(_analysis_reply())
    owner = SimpleNamespace(config=SimpleNamespace(use_session_judge=True), _llm=client)
    asyncio.run(EvolveServer._prepare_sessions(owner, [session]))
    assert len(client.calls) == 1
    assert session["_judge_scores"]["source"] == "llm"
    assert session_has_merged_outputs(session)
    asyncio.run(EvolveServer._prepare_sessions(owner, [session]))
    assert len(client.calls) == 1


def test_trace_failure_prevents_ingest_storage(monkeypatch):
    import session_ingestion.service as service

    class Client(_FakeClient):
        async def chat(self, messages, **kwargs):
            if kwargs.get("trace_name", "").endswith("analyze_trace_badcase"):
                return '{"traces":[]}'
            return await super().chat(messages, **kwargs)

    class Store:
        saved = []

        @classmethod
        def from_config(cls, *args):
            return cls()

        def duplicate_of_processed(self, session):
            return False

        def save_queued(self, session):
            self.saved.append(session)

        def save_skipped(self, session):
            self.saved.append(session)

    client = Client(_analysis_reply())
    monkeypatch.setattr(service, "SessionStore", Store)
    monkeypatch.setattr(
        service.SessionValueClassifier, "from_config",
        lambda config: SessionValueClassifier(client=client),
    )
    owner = SimpleNamespace(config=SimpleNamespace())
    with pytest.raises(TraceAnalysisError):
        asyncio.run(service._ingest_one(owner, _session()))
    assert Store.saved == []


def test_legacy_heuristic_archive_is_not_treated_as_already_analyzed(tmp_path):
    store = SessionStore(LocalObjectStore(str(tmp_path)))
    session = _session()
    store.save_skipped({**session, "value_judge": {"mode": "heuristic", "decision": "valuable"}})
    assert store.duplicate_of_processed(session) is False
    asyncio.run(_classify_session(session, SessionValueClassifier(client=_FakeClient(_analysis_reply()))))
    store.save_skipped(session)
    assert store.duplicate_of_processed(session) is True
    engine = SimpleNamespace(
        _bucket=store._bucket,
        _archive_key=store.archive_key,
        config=SimpleNamespace(storage_backend="local"),
    )
    EvolveServer._archive_sessions(engine, [session])
    assert store.duplicate_of_processed(session) is True


def test_backfill_does_not_skip_for_legacy_score_or_disabled_judge(monkeypatch):
    session = _session()
    session["judge"] = {"overall_score": 0.9}
    client = _FakeClient(_analysis_reply())
    engine = SimpleNamespace(config=SimpleNamespace(use_session_judge=False), _llm=client)
    owner = SimpleNamespace(
        config=SimpleNamespace(storage_pg_enabled=False),
        _get_embedded_evolve_server=lambda tenant_id: engine,
    )
    saved = []
    store = SimpleNamespace(
        has_judge_score=lambda sid: True,
        load_archived=lambda sid: session,
        save_session_judge=lambda sid, scores: saved.append(scores),
    )
    monkeypatch.setattr(SessionStore, "from_config", lambda *args: store)
    queue = AsyncSessionJudgeQueue(owner)
    monkeypatch.setattr(queue, "_tenant_context", lambda tid: None)
    asyncio.run(queue._review_one("default", "sess-mandatory"))
    assert len(client.calls) == 1
    assert saved[0]["overall_score"] == pytest.approx(0.805)
