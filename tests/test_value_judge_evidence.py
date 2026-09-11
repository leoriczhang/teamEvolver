"""Value-judgment chain improvements: judge evolution-evidence field,
defect/exemplary helpers, evidence stratification, and the ingest-time
requeue of defective skipped sessions.

Covers the fixes for run evolution_local_20260908_v3 findings: invalid
classifier JSON, heuristic/model calibration drift, and failure evidence
that the value filter skipped but the judge flagged.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.evolve.runtime.evidence import (
    SkillEvidenceStore,
    _stratified_history,
)
from teamEvolver.evolve.stages.judge import (
    _parse_scores,
    judge_has_defect_evidence,
    judge_is_exemplary,
)
from teamEvolver.session_store import SessionStore

# --------------------------------------------------------------------------- #
# judge schema: evolution_evidence                                            #
# --------------------------------------------------------------------------- #


def _full_judge_payload(**overrides) -> dict:
    payload = {
        "task_completion": 0.8,
        "response_quality": 0.7,
        "efficiency": 0.9,
        "tool_usage": 0.8,
        "overall_score": 0.78,
        "evolution_evidence": "defect",
        "evidence_reason": "agent 未按 SKILL.md 附带知识来源",
        "rationale": "整体良好但存在规范缺陷",
        "reasons": {"task_completion": ["产出完整"]},
    }
    payload.update(overrides)
    return payload


def test_parse_scores_reads_evolution_evidence() -> None:
    result = _parse_scores(json.dumps(_full_judge_payload(), ensure_ascii=False))

    assert result is not None
    assert result["evolution_evidence"] == "defect"
    assert result["evidence_reason"] == "agent 未按 SKILL.md 附带知识来源"
    assert result["overall_score"] == pytest.approx(0.78, abs=0.01)


def test_parse_scores_defaults_evolution_evidence_to_none() -> None:
    payload = _full_judge_payload()
    payload.pop("evolution_evidence")
    payload.pop("evidence_reason")

    result = _parse_scores(json.dumps(payload, ensure_ascii=False))

    assert result is not None
    assert result["evolution_evidence"] == "none"
    assert "evidence_reason" not in result


def test_parse_scores_rejects_invalid_evidence_value() -> None:
    payload = _full_judge_payload(evolution_evidence="excellent")

    result = _parse_scores(json.dumps(payload, ensure_ascii=False))

    assert result is not None
    assert result["evolution_evidence"] == "none"


def test_judge_has_defect_evidence_by_threshold() -> None:
    assert judge_has_defect_evidence({"overall_score": 0.3}, threshold=0.5)
    assert not judge_has_defect_evidence({"overall_score": 0.85}, threshold=0.5)
    # boundary: exactly at threshold is not a defect
    assert not judge_has_defect_evidence({"overall_score": 0.5}, threshold=0.5)


def test_judge_has_defect_evidence_by_flag() -> None:
    assert judge_has_defect_evidence(
        {"overall_score": 0.9, "evolution_evidence": "defect"}, threshold=0.5
    )
    assert not judge_has_defect_evidence(
        {"overall_score": 0.9, "evolution_evidence": "exemplary"}, threshold=0.5
    )
    assert not judge_has_defect_evidence(None, threshold=0.5)
    assert not judge_has_defect_evidence("not-a-dict", threshold=0.5)


def test_judge_is_exemplary_flag() -> None:
    assert judge_is_exemplary({"overall_score": 0.95, "evolution_evidence": "exemplary"})
    assert not judge_is_exemplary({"overall_score": 0.95, "evolution_evidence": "defect"})
    assert not judge_is_exemplary(None)


def test_defect_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamEvolver.evolve.stages.judge import _defect_threshold

    monkeypatch.setenv("EVOLVE_REQUEUE_DEFECT_THRESHOLD", "0.7")
    assert _defect_threshold() == 0.7
    assert judge_has_defect_evidence({"overall_score": 0.6})

    monkeypatch.setenv("EVOLVE_REQUEUE_DEFECT_THRESHOLD", "not-a-number")
    assert _defect_threshold() == 0.5


# --------------------------------------------------------------------------- #
# evidence ledger: exemplary goodcases                                        #
# --------------------------------------------------------------------------- #


def _entry(session_id: str, *, score=None, evidence: str = "") -> dict:
    return {
        "session_id": session_id,
        "observed_at": f"2026-09-08T00:00:{int(session_id) % 60:02d}Z",
        "summary": f"summary {session_id}",
        "trajectory": "trajectory",
        "judge_overall_score": score,
        "evolution_evidence": evidence,
        "evidence_reason": "because" if evidence else "",
        "has_tool_errors": False,
        "verified_skill_feedback": [],
        "source": "test",
        "runtime": {},
        "runtime_context": {},
        "replay_cases": [],
    }


def test_stratified_history_reserves_exemplary_quota() -> None:
    entries = [
        # older failures first so the "last limit//2" slice takes the recent ones
        _entry("1", score=0.2),
        _entry("2", score=0.3),
        # exemplary goodcase
        _entry("3", score=0.97, evidence="exemplary"),
        # ordinary fillers
        _entry("4", score=0.9),
        _entry("5", score=0.9),
        _entry("6", score=0.9),
    ]

    selected = _stratified_history(entries, limit=4)
    ids = [item["session_id"] for item in selected]

    # recent failure kept, exemplary goodcase kept despite decent score
    assert "2" in ids
    assert "3" in ids
    assert len(ids) == 4


def test_stratified_history_without_exemplaries_unchanged() -> None:
    entries = [_entry(str(i), score=0.9) for i in range(1, 9)]

    selected = _stratified_history(entries, limit=4)

    assert len(selected) == 4
    assert all(item["evolution_evidence"] == "" for item in selected)


def test_synthetic_session_restores_evolution_evidence() -> None:
    entry = _entry("7", score=0.95, evidence="exemplary")

    synthetic = SkillEvidenceStore._synthetic_session(entry, "historical")

    assert synthetic["_judge_scores"]["overall_score"] == 0.95
    assert synthetic["_judge_scores"]["evolution_evidence"] == "exemplary"
    assert synthetic["_judge_scores"]["evidence_reason"] == "because"


def test_synthetic_session_without_evidence_keeps_overall_only() -> None:
    synthetic = SkillEvidenceStore._synthetic_session(_entry("8", score=0.6), "recent")

    assert synthetic["_judge_scores"] == {"overall_score": 0.6}


# --------------------------------------------------------------------------- #
# backfill requeue (scripts/run_local_evolution.py)                           #
# --------------------------------------------------------------------------- #


def _load_script_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_local_evolution.py"
    spec = importlib.util.spec_from_file_location("run_local_evolution", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(tmp_path: Path) -> TeamEvolverConfig:
    return TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_session_backend="viking",
        sharing_viking_endpoint="memory://" + str(tmp_path),
        sharing_skill_reload_mode="off",
        llm_api_key="",
    )


def _skipped_session(session_id: str, judge: dict) -> dict:
    return {
        "session_id": session_id,
        "title": f"session {session_id}",
        "turns": [{"prompt_text": "查询时效", "response_text": "已回答"}],
        "value_judge": {"decision": "task_only", "mode": "model", "confidence": 0.8},
        "judge": judge,
    }


def test_requeue_defective_backfill_is_idempotent(tmp_path: Path) -> None:
    module = _load_script_module()
    cfg = _config(tmp_path)
    store = SessionStore.from_config(cfg)
    store.save_skipped(
        _skipped_session("defect-1", {"overall_score": 0.3, "task_completion": 0.2})
    )
    store.save_skipped(
        _skipped_session("healthy-1", {"overall_score": 0.85, "task_completion": 0.9})
    )

    counts = module.requeue_defective_sessions(cfg, threshold=0.5)

    assert counts["requeued"] == 1
    assert counts["defect_candidates"] == 1
    queued = json.loads(
        store._bucket.get_object(store.queue_key("defect-1")).read().decode("utf-8")
    )
    assert queued["requeue_reason"] == "judge_defect_evidence"
    # index row flipped to queued and the healthy session stays skipped
    rows = {row["session_id"]: row for row in store.load_index_rows()}
    assert rows["defect-1"]["status"] == "queued"
    assert rows["healthy-1"]["status"] == "skipped"

    # second run: the re-queued session is no longer "skipped" → no-op
    counts_again = module.requeue_defective_sessions(cfg, threshold=0.5)
    assert counts_again["requeued"] == 0


def test_requeue_defective_respects_explicit_defect_flag(tmp_path: Path) -> None:
    module = _load_script_module()
    cfg = _config(tmp_path)
    store = SessionStore.from_config(cfg)
    store.save_skipped(
        _skipped_session(
            "defect-flag-1",
            {"overall_score": 0.9, "evolution_evidence": "defect"},
        )
    )

    counts = module.requeue_defective_sessions(cfg, threshold=0.5)

    assert counts["requeued"] == 1
    assert store._bucket.get_object(store.queue_key("defect-flag-1"))


# --------------------------------------------------------------------------- #
# ingest-time requeue branch                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_ingest_requeues_defective_task_only_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task_only session whose judge score shows defect evidence must be
    queued (with requeue_reason) instead of archived as skipped."""
    module = _load_script_module()
    from teamEvolver.evolve.stages import judge as judge_stage
    from teamEvolver.evolve.stages import summarize as summarize_stage

    async def fake_summarize(_llm, session):
        return f"summary for {session.get('session_id')}"

    async def fake_judge(_llm, session):
        scores = (
            {"overall_score": 0.3, "task_completion": 0.2, "evolution_evidence": "defect"}
            if session.get("session_id") == "defect-1"
            else {"overall_score": 0.9, "task_completion": 0.9, "evolution_evidence": "none"}
        )
        session["_judge_scores"] = dict(scores)
        return scores

    monkeypatch.setattr(summarize_stage, "summarize_session", fake_summarize)
    monkeypatch.setattr(judge_stage, "judge_session", fake_judge)

    cfg = _config(tmp_path)
    sessions_dir = tmp_path / "converted"
    sessions_dir.mkdir()
    for sid in ("defect-1", "healthy-1"):
        (sessions_dir / f"session_{sid}.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "turns": [
                        {
                            "prompt_text": "武汉到多地的顺丰时效是多少",
                            "tool_calls": [{"function": {"name": "read", "arguments": "{}"}}],
                        }
                    ],
                    "metrics": {"tool_call_count": 1},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report = await module.run_ingest(cfg, sessions_dir, run_dir, 2)

    assert report["counts"]["requeued"] == 1
    assert report["counts"]["skipped"] == 1
    store = SessionStore.from_config(cfg)
    queued = json.loads(
        store._bucket.get_object(store.queue_key("defect-1")).read().decode("utf-8")
    )
    assert queued["requeue_reason"] == "judge_defect_evidence"
    assert queued["value_judge"]["decision"] == "task_only"
    assert queued["judge"]["overall_score"] == 0.3
    with pytest.raises(Exception):  # healthy session must NOT be queued
        store._bucket.get_object(store.queue_key("healthy-1"))
