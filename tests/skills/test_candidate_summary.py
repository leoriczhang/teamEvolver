"""Candidate queues must not download every stored Replay artifact."""
import copy
import json

from team_skills.evolution.history import compact_evaluation, detail_evaluation, evolution_feedback


def test_candidate_list_preserves_incomplete_verdict_without_large_replay_bodies():
    case = {"baseline": {"messages": [{"content": "x" * 1_000_000}]},
            "candidate": {"error": "Checklist judge unavailable"}}
    report = {"all_satisfied": False, "total": 12, "satisfied_count": 4,
              "unmet_count": 8, "reports": [{"artifacts": "y" * 1_000_000}]}
    evaluation = {
        "status": "evaluated", "candidate_revision": 4, "recommended_publish": False,
        "replay": {"verdict": "inconclusive", "cases": [case], "datasets": [{"cases": [case]}],
                   "window_results": {"recent": {"cases": [case]}},
                   "checklist": {"baseline": report, "candidate": report},
                   "decision_policy": {"decision_basis": "checklist_judge_unavailable", "checklist": report},
                   "efficiency": {"dimensions": {"total_tokens": {"candidate": "unavailable"}}}},
    }
    original = copy.deepcopy(evaluation)
    compact = compact_evaluation(evaluation)
    assert len(json.dumps(compact)) < 2_000
    assert compact["summary_only"]
    assert compact["candidate_revision"] == 4 and not compact["recommended_publish"]
    assert compact["replay"]["verdict"] == "inconclusive"
    assert compact["replay"]["error"] == "Checklist judge unavailable"
    assert compact["replay"]["checklist"]["candidate"]["unmet_count"] == 8
    assert compact["replay"]["efficiency"]["dimensions"]["total_tokens"]["candidate"] == "unavailable"
    assert evaluation == original


def test_absent_evaluation_does_not_look_evaluated():
    assert compact_evaluation(None) is None
    assert compact_evaluation({}) == {}


def test_light_detail_preserves_checklist_evidence_without_replay_artifacts():
    report = {"all_satisfied": False, "total": 1, "satisfied_count": 0,
              "items": [{"id": "video", "text": "交付视频", "satisfied": False, "evidence": "未交付"}],
              "artifacts": "x" * 1_000_000}
    evaluation = {"recommended_publish": False, "replay": {
        "verdict": "inconclusive", "cases": [{"candidate": {"messages": "x" * 1_000_000}}],
        "checklist": {"baseline": {"reports": [report]}, "candidate": report},
        "decision_policy": {"decision_basis": "checklist_judge_unavailable"},
    }}
    original = copy.deepcopy(evaluation)
    detail = detail_evaluation(evaluation)
    assert len(json.dumps(detail)) < 2_000
    assert not detail.get("summary_only")
    assert detail["cached"] and detail["replay_details_omitted"]
    assert detail["replay"]["checklist"]["candidate"]["items"] == report["items"]
    assert detail["replay"]["checklist"]["baseline"]["reports"][0]["items"] == report["items"]
    assert detail["replay"]["case_count"] == 1
    assert detail["replay"]["decision_policy"]["decision_basis"] == "checklist_judge_unavailable"
    assert evaluation == original


def test_planner_feedback_keeps_each_failure_and_unmet_item_without_old_replay_bodies():
    report = {"all_satisfied": False, "items": [
        {"id": "delivery", "text": "交付视频", "satisfied": False, "evidence": "未交付"},
    ], "artifacts": "large-artifact" * 100_000}
    evaluation = {"recommended_publish": False, "replay": {
        "verdict": "inconclusive", "checklist": {"candidate": report}, "cases": [
            {"case_id": "first", "provenance": {"session_snapshot": "old-session" * 100_000},
             "baseline": {"status": "failed", "error": "branch deadline exceeded",
                          "total_tokens": None, "messages": "old-message" * 100_000},
             "candidate": {"completed": False, "error": "Checklist judge unavailable",
                           "checklist_report": report}},
            {"case_id": "second", "candidate": {"status": "failed", "error": "runtime unavailable"}},
        ],
    }}
    original = copy.deepcopy(evaluation)
    feedback = evolution_feedback(evaluation)
    assert len(json.dumps(feedback)) < 3_000
    assert feedback["replay"]["verdict"] == "inconclusive"
    assert feedback["recommended_publish"] is False
    first, second = feedback["replay"]["cases"]
    assert first["baseline"]["error"] == "branch deadline exceeded"
    assert first["baseline"]["total_tokens"] is None
    assert first["candidate"]["completed"] is False
    assert first["candidate"]["checklist_report"]["items"] == report["items"]
    assert second["candidate"]["error"] == "runtime unavailable"
    assert evaluation == original
    assert evolution_feedback(None) == {}
