"""Frozen native validation records for Prompt continuation feedback tests."""
from __future__ import annotations

import copy

from team_replay.execution_plan import execution_plan_hash, normalize_execution_plan
from team_skills.evolution.repository import digest


def analyzed_source(session_id="old-source"):
    ref = {"source_session_key": "original-root", "trace_id": "original-trace",
           "revision": "fact-revision", "context_revision": "original-context"}
    return {
        "session_id": session_id, "source": "langfuse", "user_alias": "user-1",
        "_summary": "Existing ingestion analysis", "_judge_scores": {"source": "llm", **{
            key: 0.8 for key in ("overall_score", "task_completion", "response_quality", "efficiency", "tool_usage")}},
        "value_judge": {"mode": "merged", "decision": "valuable", "confidence": 0.9, "reason": "Shared defect"},
        "provenance": {"schema_version": "session-source.v1", "identity_verified": True,
                       "trace_identity_verified": True, "source_session_key": "original-root",
                       "trace_revisions": [ref], "target_trace_refs": [ref], "context_trace_refs": []},
        "turns": [{"source_trace_id": "original-trace", "prompt_text": "Original task",
                   "response_text": "Original answer", "used_skills": ["demo"],
                   "request_context": {"trace_input": {"query": "Original task"}}}],
    }


def development_validation(candidate):
    """Attach one public development failure and one private locked Case."""
    detail = copy.deepcopy(candidate["detail"])
    # The real native validation freezes canonical plans while Candidate detail
    # can still retain integer revision scalars from the original proposal.
    for key in ("baseline_execution_plan", "candidate_execution_plan"):
        detail[key] = normalize_execution_plan(detail[key])
    checks = [{"id": "R01", "kind": "output", "text": "Preserve the original business intent"}]
    cases, pairs = [], []
    for selection, query in (("current", "Original development question"),
                             ("locked_acceptance", "LOCKED-QUESTION")):
        provenance = {"replay_selection": selection}
        case = {"case_id": selection + "-case", "query": query, "checks": checks,
                "provenance": provenance, "metadata": {}}
        cases.append(case)
        pair = {"case_id": case["case_id"], "query": query, "selection": selection,
                "provenance": copy.deepcopy(provenance)}
        for branch in ("baseline", "candidate"):
            satisfied = branch == "baseline"
            pair[branch] = {
                "ok": True, "completed": satisfied, "execution_verification": "verified",
                "execution_plan_hash": execution_plan_hash(detail[branch + "_execution_plan"]),
                "interactions": [{"prompt": query, "response": branch + " original actual response"},
                                 {"prompt": "Existing progressive feedback", "response": branch + " second response"}],
                "checklist_report": {"all_satisfied": satisfied,
                    "items": [{**checks[0], "satisfied": satisfied, "evidence": branch + " actual machine reason"}],
                    "private": "RAW-PRIVATE"},
                "observed_requests": [{"private": "RAW-PRIVATE"}],
                "disclosures": [{"message": "RAW-PRIVATE"}],
            }
        pairs.append(pair)
    detail["replay_cases"] = cases
    candidate["evaluation"] = {
        **candidate.get("evaluation", {}), "candidate_revision": candidate["revision"],
        "validation_run_id": "development-validation", "status": "evaluated",
        "replay": {**candidate.get("evaluation", {}).get("replay", {}), "cases": pairs,
                   "probe_gate": {"private": "PROBE-PRIVATE"}},
    }
    payload = {"candidate_id": candidate["candidate_id"], "revision": candidate["revision"], "detail": detail}
    return {"command_id": "development-validation", "kind": "skill.validate", "payload": payload,
            "payload_hash": digest(payload), "finished_at": "2026-11-03T00:00:00Z",
            "result": {"status": "evaluated", "evaluation": copy.deepcopy(candidate["evaluation"])}}
