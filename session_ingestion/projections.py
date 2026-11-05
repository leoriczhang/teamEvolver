"""Compact Session metadata used by bounded console list queries."""
from __future__ import annotations

import copy
from collections.abc import Mapping


def _clean_title(value) -> str:
    return " ".join(str(value or "").split())[:120]


def _session_title(session: Mapping, meta: Mapping) -> str:
    for value in (meta.get("title"), session.get("title")):
        title = _clean_title(value)
        if title:
            return title
    for turn in session.get("turns") or []:
        if isinstance(turn, Mapping):
            title = _clean_title(turn.get("prompt_text"))
            if title:
                return title
    for message in session.get("messages") or []:
        if isinstance(message, Mapping) and message.get("role") == "user":
            title = _clean_title(message.get("content"))
            if title:
                return title
    return ""


def session_list_meta(session: Mapping, meta: Mapping | None = None) -> dict:
    result = copy.deepcopy(dict(meta or session.get("meta") or {}))
    turns = [turn for turn in session.get("turns") or [] if isinstance(turn, Mapping)]
    evidence_turns = [
        item for turn in turns for item in [turn, *(turn.get("agent_threads") or [])]
        if isinstance(item, Mapping)
    ]
    used_skills: list[str] = []
    for raw in [
        *(session.get("used_skills") or []),
        *(name for turn in evidence_turns for key in ("used_skills", "read_skills", "modified_skills")
          for name in turn.get(key) or []),
    ]:
        name = str(raw.get("skill_name") or raw.get("name") or "") if isinstance(raw, Mapping) else str(raw or "")
        name = name.strip()
        if name and name not in used_skills:
            used_skills.append(name)
    available_skills: list[str] = []
    for turn in evidence_turns:
        for ref in turn.get("skill_refs") or []:
            if not isinstance(ref, Mapping) or ref.get("source") != "configured":
                continue
            name = str(ref.get("name") or ref.get("skill_name") or "").strip()
            if name and name not in available_skills:
                available_skills.append(name)
    runtime_context = session.get("runtime_context") if isinstance(session.get("runtime_context"), Mapping) else {}
    runtime = session.get("runtime") or {}
    if runtime.get("type") == "managed-agents":
        result.update({
            "ma_connection_id": runtime.get("connection_id"),
            "ma_parent_session_id": runtime_context.get("source_session_id"),
            "runtime_type": "managed-agents",
        })
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
    if available_skills:
        result["available_skills"] = available_skills
    title = _session_title(session, result)
    if title:
        result["title"] = title
    if trace_id:
        result["trace_id"] = trace_id
    return result
