"""Compact Session metadata used by bounded console list queries."""
from __future__ import annotations

import copy
from collections.abc import Mapping


def session_list_meta(session: Mapping, meta: Mapping | None = None) -> dict:
    result = copy.deepcopy(dict(meta or session.get("meta") or {}))
    turns = [turn for turn in session.get("turns") or [] if isinstance(turn, Mapping)]
    used_skills: list[str] = []
    for raw in [
        *(session.get("used_skills") or []),
        *(name for turn in turns for name in turn.get("used_skills") or []),
    ]:
        name = str(raw or "").strip()
        if name and name not in used_skills:
            used_skills.append(name)
    runtime_context = session.get("runtime_context") if isinstance(session.get("runtime_context"), Mapping) else {}
    trace_id = next((
        str(value).strip() for value in (
            result.get("trace_id"),
            session.get("trace_id"),
            runtime_context.get("trace_id"),
            result.get("session_id"),
            runtime_context.get("source_session_id"),
            session.get("session_id"),
        ) if str(value or "").strip()
    ), "")
    result.update(num_turns=len(turns), used_skills=used_skills)
    if trace_id:
        result["trace_id"] = trace_id
    return result
