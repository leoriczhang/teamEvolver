"""Console stage results without copying raw conversations or runtime credentials."""
from __future__ import annotations

from collections.abc import Mapping

STAGE_ORDER = ("normalize", "split", "analyze", "trace_analyze", "archive")


def fields(value, keys):
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in keys if key in value}


def stage_result(stage, session):
    """Project the actual immutable output of one stage for an expandable view."""
    turns = session.get("turns") or []
    overview = {
        **fields(session, ("session_id", "user_id", "timestamp", "business_time")),
        "turn_count": len(turns),
        "tool_call_count": sum(len(t.get("tool_calls") or []) for t in turns if isinstance(t, Mapping)),
    }
    if stage == "normalize":
        return {"overview": overview}
    if stage == "split":
        segments = session.get("segments") or []
        return {"segment_count": len(segments), "segments": [
            {**fields(item, ("session_id", "title", "timestamp")),
             "turn_count": len(item.get("turns") or [])}
            for item in segments if isinstance(item, Mapping)
        ]}
    if stage == "trace_analyze":
        return {"traces": [
            fields(item, (
                "trace_id", "turn_num", "is_badcase", "problem_type", "problem_type_label",
                "problem_description", "business_time", "analyzed_at", "agent_name",
                "session_thread_id", "parent_trace_id", "parent_turn_num", "used_skills",
            )) for item in session.get("trace_analysis") or []
        ]}
    return {
        "summary": session.get("_summary") or "",
        "value_judge": fields(session.get("value_judge"), ("decision", "confidence", "reason", "mode", "model")),
        "judge": fields(session.get("_judge_scores"), (
            "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage", "rationale", "reasons",
        )),
        **({"overview": overview} if stage == "archive" else {}),
    }
