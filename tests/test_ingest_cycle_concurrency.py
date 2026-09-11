"""Concurrency hardening: ingest fan-out + evolution cycle parallelism.

Covers the three changes that make throughput LLM-bound rather than
serialization-bound:

- ``SessionStore`` index merges are serialized per store identity, so
  concurrent ingests no longer lose each other's ``session_index.json`` rows
  (read-modify-write race).
- ``pull_sessions`` processes sessions with bounded concurrency while
  preserving result order and per-session error isolation.
- ``EvolveServer._prepare_sessions`` runs summarize→judge as a per-session
  pipeline capped by ``llm_max_concurrency``.
- ``run_periodic`` drains backlogs back-to-back instead of idling a full
  interval between cycles.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from teamEvolver.session_store import SessionStore
from teamEvolver.storage import LocalObjectStore


# --------------------------------------------------------------------- #
# session_index concurrent merges                                        #
# --------------------------------------------------------------------- #


def _session(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "turns": [{"turn_num": 1, "prompt_text": f"i {session_id}", "response_text": "r"}],
    }


def test_concurrent_ingests_never_lose_index_rows(tmp_path) -> None:
    # Distinct SessionStore instances over the same local root — the shape of
    # concurrent HTTP ingests (SessionStore.from_config per request).
    def make_store() -> SessionStore:
        return SessionStore(LocalObjectStore(tmp_path / "store"))

    errors: list[Exception] = []

    def ingest(tag: int) -> None:
        store = make_store()
        try:
            for i in range(25):
                store.save_queued(_session(f"t{tag}-s{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ingest, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = make_store().list_queue(limit=10000)
    assert len(rows) == 200
    assert len({row["session_id"] for row in rows}) == 200


# --------------------------------------------------------------------- #
# pull_sessions bounded concurrency                                      #
# --------------------------------------------------------------------- #


class _FakeLangfuseClient:
    def __init__(self, session_ids, *, delay: float = 0.02, fail_ids=()):
        self._ids = list(session_ids)
        self._delay = delay
        self._fail = set(fail_ids)
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def list_session_ids(self, filters, max_sessions=0):
        return self._ids[: max_sessions or None]

    def fetch_session_with_traces(self, session_id, *, trace_name=""):
        from teamEvolver.integrations.langfuse_client import LangfuseError

        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self._delay)
            if session_id in self._fail:
                raise LangfuseError(f"boom {session_id}")
            return (
                {"id": session_id, "traces": []},
                [{"id": f"trace-{session_id}", "input": "x", "output": "y"}],
            )
        finally:
            with self._lock:
                self.active -= 1


def _pull_config(fake_client, monkeypatch):
    from teamEvolver.integrations import langfuse_pull

    monkeypatch.setattr(langfuse_pull, "_ensure_enabled", lambda config: None)
    monkeypatch.setattr(
        langfuse_pull.LangfuseClient, "from_config", staticmethod(lambda config: fake_client)
    )
    monkeypatch.setattr(
        langfuse_pull, "build_filters_from_config",
        lambda config, overrides: type("F", (), {"trace_name": "", "as_dict": lambda self: {}})(),
    )
    monkeypatch.setattr(langfuse_pull, "build_mapper_registry", lambda config: None)
    monkeypatch.setattr(
        langfuse_pull,
        "_convert_session",
        lambda session, traces, mapper: {
            "session_id": session["id"],
            "turns": [{"prompt_text": "p", "response_text": "r"}],
        },
    )
    monkeypatch.setattr(langfuse_pull, "_has_meaningful_content", lambda converted: True)
    return object()


@pytest.mark.anyio
async def test_pull_sessions_bounded_concurrency_preserves_order_and_isolates_errors(
    monkeypatch,
) -> None:
    from teamEvolver.integrations import langfuse_pull

    ids = [f"s{i}" for i in range(20)]
    fake = _FakeLangfuseClient(ids, fail_ids={"s3", "s7"})
    config = _pull_config(fake, monkeypatch)

    async def fake_ingest(converted):
        await asyncio.sleep(0.001)
        return {"status": "queued", "queued": True}

    result = await langfuse_pull.pull_sessions(
        config, fake_ingest, max_sessions=20, concurrency=4
    )

    # Order preserved despite concurrent completion.
    assert [r["session_id"] for r in result["results"]] == ids
    # Errors isolated per session.
    by_id = {r["session_id"]: r for r in result["results"]}
    assert by_id["s3"]["status"] == "error"
    assert by_id["s7"]["status"] == "error"
    assert by_id["s0"]["status"] == "queued"
    assert result["counts"]["error"] == 2
    assert result["counts"]["queued"] == 18
    # Concurrency actually bounded at 4, and actually concurrent (>1).
    assert 1 < fake.peak <= 4


@pytest.mark.anyio
async def test_pull_sessions_serial_when_concurrency_one(monkeypatch) -> None:
    from teamEvolver.integrations import langfuse_pull

    ids = [f"s{i}" for i in range(5)]
    fake = _FakeLangfuseClient(ids)
    config = _pull_config(fake, monkeypatch)

    async def fake_ingest(converted):
        return {"status": "queued", "queued": True}

    result = await langfuse_pull.pull_sessions(
        config, fake_ingest, max_sessions=5, concurrency=1
    )
    assert result["counts"]["queued"] == 5
    assert fake.peak == 1


# --------------------------------------------------------------------- #
# _prepare_sessions pipeline                                             #
# --------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_prepare_sessions_pipelines_summarize_and_judge(monkeypatch, tmp_path) -> None:
    import teamEvolver.evolve.runtime.orchestrator as om
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    active = {"n": 0, "peak": 0}

    async def fake_summarize(llm, session):
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        try:
            await asyncio.sleep(0.01)
        finally:
            active["n"] -= 1
        return f"summary {session['session_id']}"

    judged: list[str] = []

    async def fake_judge(llm, session):
        judged.append(session["session_id"])
        session["_judge_scores"] = {"overall_score": 0.8}
        return {"overall_score": 0.8}

    monkeypatch.setattr(om, "summarize_session", fake_summarize)
    monkeypatch.setattr(om, "judge_session", fake_judge)
    monkeypatch.setattr(
        om, "_extract_session_metadata", lambda session: session.setdefault("_skills_referenced", set())
    )
    monkeypatch.setattr(om, "build_session_trajectory", lambda session: "traj")

    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            storage_local_root=str(tmp_path / "store"),
            llm_api_key="k",
            llm_max_concurrency=3,
            publish_mode="direct",
            history_path=str(tmp_path / "h.jsonl"),
            processed_log_path=str(tmp_path / "p.json"),
        ),
        mock=False,
    )

    sessions = [_session(f"ps-{i}") for i in range(10)]
    # One session already has a reliable score — judge must skip it.
    sessions[0]["_judge_scores"] = {"overall_score": 0.9}

    judged_count = await server._prepare_sessions(sessions)

    assert judged_count == 9
    assert len(judged) == 9
    assert "ps-0" not in judged
    assert all(s.get("_summary", "").startswith("summary") for s in sessions)
    assert all(s.get("_trajectory") == "traj" for s in sessions)
    # LLM fan-out capped by llm_max_concurrency=3, and actually parallel.
    assert 1 < active["peak"] <= 3

    summary = server._judge_summary(sessions, judged_count)
    assert summary["judged_sessions"] == 9
    assert summary["scored_sessions"] == 10  # 9 judged + 1 pre-scored
    assert summary["mean_score"] == round((0.8 * 9 + 0.9) / 10, 3)


@pytest.mark.anyio
async def test_prepare_sessions_respects_judge_disabled(monkeypatch, tmp_path) -> None:
    import teamEvolver.evolve.runtime.orchestrator as om
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    async def fake_summarize(llm, session):
        return "s"

    judge_calls: list[str] = []

    async def fake_judge(llm, session):
        judge_calls.append(session["session_id"])
        return {"overall_score": 0.5}

    monkeypatch.setattr(om, "summarize_session", fake_summarize)
    monkeypatch.setattr(om, "judge_session", fake_judge)
    monkeypatch.setattr(om, "_extract_session_metadata", lambda session: None)
    monkeypatch.setattr(om, "build_session_trajectory", lambda session: "t")

    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            storage_local_root=str(tmp_path / "store"),
            llm_api_key="k",
            use_session_judge=False,
            publish_mode="direct",
            history_path=str(tmp_path / "h.jsonl"),
            processed_log_path=str(tmp_path / "p.json"),
        ),
        mock=False,
    )
    judged = await server._prepare_sessions([_session("a"), _session("b")])
    assert judged == 0
    assert judge_calls == []


# --------------------------------------------------------------------- #
# run_periodic backlog drain                                             #
# --------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_run_periodic_drains_backlog_without_interval_wait(monkeypatch, tmp_path) -> None:
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            storage_local_root=str(tmp_path / "store"),
            llm_api_key="k",
            interval_seconds=3600,
            drain_max_per_cycle=5,
            publish_mode="direct",
            history_path=str(tmp_path / "h.jsonl"),
            processed_log_path=str(tmp_path / "p.json"),
        ),
        mock=False,
    )

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        server.stop()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def fake_run_once():
        return {"sessions": 5}  # hit the cap ⇒ backlog presumed

    monkeypatch.setattr(server, "run_once", fake_run_once)

    await server.run_periodic()
    assert sleeps == [1.0]


@pytest.mark.anyio
async def test_run_periodic_sleeps_interval_when_queue_empty(monkeypatch, tmp_path) -> None:
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            storage_local_root=str(tmp_path / "store"),
            llm_api_key="k",
            interval_seconds=3600,
            drain_max_per_cycle=0,  # unlimited: backlog checked via the queue
            publish_mode="direct",
            history_path=str(tmp_path / "h.jsonl"),
            processed_log_path=str(tmp_path / "p.json"),
        ),
        mock=False,
    )

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        server.stop()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def fake_run_once():
        return {"sessions": 0}

    monkeypatch.setattr(server, "run_once", fake_run_once)

    await server.run_periodic()
    assert sleeps == [3600]
