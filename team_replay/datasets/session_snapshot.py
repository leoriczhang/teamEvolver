"""Immutable Dataset Case projection of an archived Session."""
import copy
import uuid


def snapshot_case(session: dict, row: dict) -> dict:
    """Consume canonical adapter output, never re-parse upstream traces."""
    turns = [turn for turn in session.get("turns") or [] if isinstance(turn, dict)]
    first = next((turn for turn in turns if str(turn.get("prompt_text") or "").strip()), {})
    query = str(first.get("prompt_text") or "").strip()
    if not query:
        messages = session.get("messages") or [m for turn in turns for m in turn.get("messages") or []]
        query = next((
            m["content"].strip() for m in messages
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
            and m["content"].strip()
        ), "")
    meta = row.get("meta") or session.get("meta") or {}
    used_skills = list(dict.fromkeys(
        str(item or "").strip()
        for item in row.get("used_skills") or session.get("used_skills") or []
        if str(item or "").strip()
    ))
    session_id = str(row.get("session_id") or session.get("session_id") or "")
    if not session_id or len(session_id) > 200 or "/" in session_id:
        raise ValueError("invalid Session ID")
    return {
        "case_id": "case_" + uuid.uuid4().hex,
        "query": query,
        "skill_ids": used_skills,
        "checks": [{"id": "R01", "kind": "output", "text": query}],
        "materials": [],
        "provenance": {
            "session_id": session_id,
            "session_ids": [session_id],
            "trace_id": str(meta.get("trace_id") or ""),
            "turn_num": first.get("turn_num") or 1,
            "timestamp": row.get("timestamp") or session.get("timestamp") or "",
            "ingested_at": row.get("ingested_at") or session.get("ingested_at") or "",
            "session_snapshot": copy.deepcopy(session),
        },
        "replay": {
            "progressive_disclosure": {
                "enabled": True,
                "initial_visibility": "query_only",
                "batch_size": 4,
                "stop_when": "all_checklist_items_satisfied",
            },
        },
        "metadata": {
            "title": str(row.get("title") or session.get("title") or query),
            "used_skills": used_skills,
            "user_alias": row.get("user_alias") or session.get("user_alias") or "",
            "judge": copy.deepcopy(row.get("judge") or session.get("judge") or {}),
        },
    }
