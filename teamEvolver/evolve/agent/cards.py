"""Round-1 index cards and per-round observation rendering.

Progressive disclosure: the first user message carries only dense
one-line-per-session indexes and a skill outline; full session bodies and
skill content are loaded on demand through the loop's tools.
"""

from __future__ import annotations

import json
from typing import Optional


def _compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _gist(session: dict, limit: int = 160) -> str:
    return _compact(str(session.get("_summary") or ""), limit)


def _aggregate_stats(session: dict) -> str:
    aggregate = session.get("aggregate") or {}
    if not isinstance(aggregate, dict) or not aggregate:
        return ""
    parts: list[str] = []
    if aggregate.get("rollout_count"):
        parts.append(f"{aggregate['rollout_count']} rollouts")
    mean_score = aggregate.get("mean_score")
    if mean_score is not None:
        parts.append(f"mean ORM={mean_score:.3f}")
    if aggregate.get("success_count") or aggregate.get("fail_count"):
        parts.append(f"success={aggregate.get('success_count', 0)} fail={aggregate.get('fail_count', 0)}")
    if aggregate.get("stability"):
        parts.append(f"stability={aggregate['stability']}")
    return ", ".join(parts)


def _evidence_flags(session: dict) -> str:
    flags: list[str] = []
    avg_prm = session.get("_avg_prm")
    if avg_prm is not None:
        flags.append(f"avg PRM: {avg_prm}")
    stats = _aggregate_stats(session)
    if stats:
        flags.append(stats)
    if session.get("_has_tool_errors"):
        flags.append("has tool errors")
    skills = session.get("_skills_referenced") or set()
    if skills:
        flags.append(f"skills: {sorted(skills)}")
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), dict)
        else {}
    )
    profile = str(
        runtime_context.get("evaluation_profile")
        or session.get("_evaluation_profile")
        or ""
    ).strip()
    if profile:
        flags.append(f"profile: {profile}")
    judge_scores = (
        session.get("_judge_scores")
        if isinstance(session.get("_judge_scores"), dict)
        else {}
    )
    evidence_kind = str(judge_scores.get("evolution_evidence") or "").strip().lower()
    if evidence_kind in {"defect", "exemplary"}:
        reason = str(judge_scores.get("evidence_reason") or "").strip()
        flags.append(f"evidence: {evidence_kind}" + (f" — {_compact(reason, 120)}" if reason else ""))
    return "; ".join(flags)


def build_session_index(sessions: list[dict], max_sessions: int = 200) -> str:
    """One dense line per session; full bodies load via ``read_session``."""
    lines = [
        f"## Session evidence index ({len(sessions)} sessions)",
        "",
        "每行一个会话的紧凑索引。需要完整内容时用 read_session 工具按需加载"
        "（part=summary / trajectory / tail / full），用 search_sessions 检索关键词。",
        "",
    ]
    for session in sessions[:max_sessions]:
        session_id = str(session.get("session_id") or "?")
        entry = f"- `{session_id}`"
        flags = _evidence_flags(session)
        if flags:
            entry += f" | {flags}"
        gist = _gist(session)
        if gist:
            entry += f" | gist: {gist}"
        elif not flags:
            entry += " | (no data)"
        lines.append(entry)
    if len(sessions) > max_sessions:
        lines.append(f"\n... and {len(sessions) - max_sessions} more sessions")
    return "\n".join(lines)


def build_skill_outline_card(skill: Optional[dict]) -> str:
    """Name/description/headings only — full content loads via ``read_skill``."""
    if not skill:
        return "## Current skill (outline)\n\n(no current skill)"
    content = str(skill.get("content") or "")
    headings = [
        line.strip()
        for line in content.splitlines()
        if line.lstrip().startswith("#")
    ]
    parts = [
        "## Current skill (outline)",
        "",
        f"Name: {skill.get('name', '')}",
        f"Description: {skill.get('description', '')}",
        f"Category: {skill.get('category', 'general')}",
        f"Content length: {len(content)} chars",
        "",
        "Headings:",
    ]
    if headings:
        parts.extend(headings[:60])
        if len(headings) > 60:
            parts.append(f"... and {len(headings) - 60} more headings")
    else:
        parts.append("(no headings)")
    editable = (
        skill.get("_editable_bundle_files")
        if isinstance(skill.get("_editable_bundle_files"), dict)
        else {}
    )
    if editable:
        parts.append("")
        parts.append("Editable bundle files:")
        parts.extend(
            f"- `{path}` ({len(str(text))} chars)"
            for path, text in sorted(editable.items())
        )
    parts.append("")
    parts.append(
        "正文全文需要用 read_skill 工具获取（propose_edits 的锚点必须从那里逐字节复制）。"
    )
    return "\n".join(parts)


def build_merge_card(existing: dict, incoming: dict) -> str:
    """Both merge versions inlined (bounded inputs, no tools needed)."""
    return (
        f"## Version A (currently in shared storage, v{existing.get('_version', '?')})\n\n"
        f"Name: {existing.get('name', '')}\n"
        f"Description: {existing.get('description', '')}\n"
        f"Category: {existing.get('category', 'general')}\n\n"
        f"Content:\n```\n{existing.get('content', '')}\n```\n\n"
        f"---\n\n"
        f"## Version B (newly evolved)\n\n"
        f"Name: {incoming.get('name', '')}\n"
        f"Description: {incoming.get('description', '')}\n"
        f"Category: {incoming.get('category', 'general')}\n\n"
        f"Content:\n```\n{incoming.get('content', '')}\n```"
    )


def build_scratchpad_header(state: dict) -> str:
    return "## Loop state\n" + json.dumps(state, ensure_ascii=False)


def render_observation_message(
    state: dict,
    observations: list[dict],
    *,
    must_submit: bool = False,
    notice: str = "",
) -> str:
    """The user message fed back after each assistant turn."""
    parts = [
        build_scratchpad_header(state),
        "",
        "## Observations",
        json.dumps(observations, ensure_ascii=False, indent=2),
    ]
    if notice:
        parts.extend(["", notice])
    if must_submit:
        parts.extend(
            [
                "",
                "注意：轮数即将耗尽。下一轮必须输出 final（type=final 的最终决策），"
                "不要再调用工具。",
            ]
        )
    return "\n".join(parts)


def build_round1_user_msg(
    *,
    stage: str,
    skill_name: str,
    sessions: list[dict],
    current_skill: Optional[dict],
    existing_skill_names: list[str],
    evolution_context: Optional[dict],
) -> str:
    """Round-1 user message for evolve/create (progressive disclosure card)."""
    # Lazy import avoids a circular module dependency (execute imports the
    # runner lazily inside its stage wrappers).
    from ..stages.execute import (
        _build_cross_cycle_context,
        _build_evaluation_cohort_context,
    )

    parts: list[str] = []
    if stage == "evolve_skill":
        parts.append(build_skill_outline_card(current_skill))
    cross_cycle = _build_cross_cycle_context(evolution_context)
    if cross_cycle:
        parts.append(cross_cycle)
    cohort = _build_evaluation_cohort_context(sessions)
    if cohort:
        parts.append(cohort)
    parts.append(build_session_index(sessions))
    parts.append(
        "## Existing skill names in the library\n\n"
        + (", ".join(existing_skill_names) or "(none)")
    )
    return "\n\n".join(parts)
