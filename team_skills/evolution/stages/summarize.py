"""
Session Summarization: for each session, build a lossless structured
trajectory (programmatic) and a trajectory-aware analytical summary (LLM).

Attaches to each session dict:
- ``_trajectory``: structured text preserving the exact step-by-step path
- ``_summary``: LLM-generated analysis focusing on causal chains and insights
- ``_skills_referenced``, ``_avg_prm``, ``_has_tool_errors``: metadata
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from team_skills.evolution.kernel.helpers import compact_tool_calls, compact_tool_observations
from team_skills.evolution.kernel.llm import AsyncLLMClient

logger = logging.getLogger(__name__)

from contextvars import ContextVar

_SUMMARIZER_DEBUG_DIR: ContextVar[str] = ContextVar("summarizer_debug_dir", default="")

# ------------------------------------------------------------------ #
#  Programmatic trajectory builder (zero information loss)             #
# ------------------------------------------------------------------ #

_PROMPT_MAX = 4000
_RESPONSE_MAX = 4000
_TOOL_ARG_MAX = 4000
_TOOL_RESULT_MAX = 4000
_TOOL_ERR_MAX = 2000
_MAX_TOOLS_PER_STEP = 20


def _clip(text: Any, limit: int) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _format_tool_calls(turn: dict) -> list[str]:
    """Render tool calls with their results/errors into compact lines."""
    raw_calls = turn.get("tool_calls") or []
    raw_results = turn.get("tool_results") or []
    raw_observations = turn.get("tool_observations") or []
    raw_errors = turn.get("tool_errors") or []

    result_by_id: dict[str, dict] = {}
    for r in raw_results:
        if isinstance(r, dict) and r.get("tool_call_id"):
            result_by_id[r["tool_call_id"]] = r
    for o in raw_observations:
        if isinstance(o, dict) and o.get("tool_call_id"):
            result_by_id.setdefault(o["tool_call_id"], o)

    error_by_tool: dict[str, list[str]] = {}
    for e in raw_errors:
        if isinstance(e, dict):
            tname = str(e.get("tool_name") or "")
            content = _clip(e.get("content", ""), _TOOL_ERR_MAX)
            error_by_tool.setdefault(tname, []).append(content)

    lines: list[str] = []
    for tc in raw_calls[:_MAX_TOOLS_PER_STEP]:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        # Canonical shape is {id, type, function:{name, arguments}}; accept the
        # legacy mapper shape {name, input} so older sessions still render.
        name = str(func.get("name") or tc.get("name") or "unknown")
        args = _clip(str(func.get("arguments") or tc.get("input") or ""), _TOOL_ARG_MAX)
        call_id = str(tc.get("id") or "")

        outcome = ""
        r = result_by_id.get(call_id)
        if r:
            if r.get("has_error"):
                err_type = r.get("error_type", "")
                err_content = _clip(r.get("content", ""), _TOOL_RESULT_MAX)
                outcome = f" → ✗ [{err_type}] {err_content}" if err_type else f" → ✗ {err_content}"
            else:
                content = _clip(r.get("content", ""), _TOOL_RESULT_MAX)
                cmd = _clip(r.get("command", ""), 80)
                if cmd:
                    outcome = f" → ✓ cmd={cmd}"
                    if content:
                        outcome += f" | {content}"
                elif content:
                    outcome = f" → ✓ {content}"
                else:
                    outcome = " → ✓"

        if not outcome and name in error_by_tool:
            errs = error_by_tool[name]
            outcome = f" → ✗ {errs[0]}"

        lines.append(f"    {name}({args}){outcome}")

    leftover_errors = []
    called_names = {
        (tc.get("function") or {}).get("name", "") for tc in raw_calls[:_MAX_TOOLS_PER_STEP] if isinstance(tc, dict)
    }
    for tname, errs in error_by_tool.items():
        if tname not in called_names:
            for e in errs:
                leftover_errors.append(f"    ⚠ {tname}: {e}")
    lines.extend(leftover_errors[:3])

    if len(raw_calls) > _MAX_TOOLS_PER_STEP:
        lines.append(f"    ... +{len(raw_calls) - _MAX_TOOLS_PER_STEP} more tool calls")

    return lines


def build_session_trajectory(session: dict) -> str:
    """Build a structured trajectory preserving the step-by-step path.

    If the session contains aggregated rollouts (turns carry ``_rollout_idx``),
    the trajectory is organised per-rollout with a header showing ORM score
    and success flag.  The user prompt is shown once at the top and omitted
    from subsequent steps to avoid redundancy.

    Each step shows: skills used, tool calls with outcomes, agent response
    snippet, and PRM / ORM score where available.
    """
    turns = session.get("turns", [])
    if not turns:
        return "(empty session)"

    # ---- detect rollout structure ------------------------------------ #
    has_rollouts = any(t.get("_rollout_idx") is not None for t in turns)

    # Deduplicate user prompt: show once, then omit repeats
    first_prompt = _clip((turns[0].get("prompt_text") or ""), _PROMPT_MAX)

    if has_rollouts:
        return _build_rollout_trajectory(turns, first_prompt)
    return _build_flat_trajectory(turns, first_prompt)


def _build_flat_trajectory(turns: list[dict], first_prompt: str) -> str:
    """Single-rollout (or non-aggregated) trajectory."""
    blocks: list[str] = []
    for i, t in enumerate(turns, 1):
        blocks.append(_format_step(t, i, first_prompt, show_prompt=(i == 1)))
    return "\n".join(blocks)


def _build_rollout_trajectory(turns: list[dict], first_prompt: str) -> str:
    """Multi-rollout aggregated trajectory with per-rollout headers."""
    # Group turns by _rollout_idx
    rollouts: dict[int, list[dict]] = {}
    for t in turns:
        idx = t.get("_rollout_idx", 0)
        rollouts.setdefault(idx, []).append(t)

    blocks: list[str] = []
    if first_prompt:
        blocks.append(f"Task: {first_prompt}")
        blocks.append("")

    for rollout_idx in sorted(rollouts.keys()):
        rollout_turns = rollouts[rollout_idx]
        # Extract ORM score from the rollout metadata on turns
        orm = rollout_turns[0].get("_rollout_score")
        success = rollout_turns[0].get("_rollout_success")
        orm_str = f"ORM={orm}" if orm is not None else "ORM=n/a"
        suc_str = f"success={success}" if success is not None else ""
        header_parts = [f"Rollout {rollout_idx}", orm_str]
        if suc_str:
            header_parts.append(suc_str)
        blocks.append(f"═══ {' | '.join(header_parts)} ═══")

        for step_i, t in enumerate(rollout_turns, 1):
            blocks.append(_format_step(t, step_i, first_prompt, show_prompt=False))
        blocks.append("")  # blank line between rollouts

    return "\n".join(blocks)


def _format_step(
    turn: dict,
    step_num: int,
    first_prompt: str,
    *,
    show_prompt: bool,
) -> str:
    """Format a single step line for the trajectory."""
    prompt = _clip(turn.get("prompt_text", ""), _PROMPT_MAX)
    response = _clip(turn.get("response_text", ""), _RESPONSE_MAX)

    skills = []
    for s in turn.get("read_skills") or []:
        name = s.get("skill_name", "").strip() if isinstance(s, dict) else str(s or "").strip()
        if name:
            skills.append(name)
    modified = []
    for s in turn.get("modified_skills") or []:
        name = s.get("skill_name", "").strip() if isinstance(s, dict) else str(s or "").strip()
        if name:
            modified.append(name)
    injected = [str(s or "").strip() for s in (turn.get("injected_skills") or []) if str(s or "").strip()]

    prm = turn.get("prm_score")
    prm_str = f"PRM={prm}" if prm is not None else ""

    header_parts = [f"[Step {step_num}]"]
    if prm_str:
        header_parts.append(prm_str)
    if skills:
        header_parts.append(f"read_skills={skills}")
    if modified:
        header_parts.append(f"modified_skills={modified}")
    if injected:
        header_parts.append(f"injected={injected}")
    header = " | ".join(header_parts)

    lines = [header]
    if show_prompt and prompt:
        lines.append(f"  User: {prompt}")

    tool_lines = _format_tool_calls(turn)
    if tool_lines:
        lines.append("  Tools:")
        lines.extend(tool_lines)

    if response:
        lines.append(f"  Agent: {response}")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  LLM-based analytical summary (trajectory-aware)                     #
# ------------------------------------------------------------------ #

# The standalone summarize LLM stage was merged into the single
# ``analyze_session`` call (see ``stages/analyze.py``). Only the programmatic
# helpers below survive — they build the trajectory / payload / metadata that
# the merged stage, the orchestrator and Prompt Studio still consume.

_SUMMARY_PROMPT_MAX_CHARS = 8000
_SUMMARY_RESPONSE_MAX_CHARS = 8000


def _build_session_payload(session: dict) -> dict[str, Any]:
    """Build a compact representation of the session for the LLM.

    Deduplicates repeating user prompts — only the first occurrence is
    included; subsequent turns with the same prompt get ``prompt: "(same)"``.
    """
    turns = session.get("turns", [])
    first_prompt = (turns[0].get("prompt_text") or "")[:_SUMMARY_PROMPT_MAX_CHARS] if turns else ""
    interactions: list[dict[str, Any]] = []

    for idx, t in enumerate(turns):
        raw_prompt = (t.get("prompt_text") or "")[:_SUMMARY_PROMPT_MAX_CHARS]
        prompt = raw_prompt if idx == 0 else ("(same)" if raw_prompt == first_prompt else raw_prompt)

        interaction: dict[str, Any] = {
            "prompt": prompt,
            # Keep enough tail content for command-heavy sessions where the
            # decisive environment knowledge often appears near the end.
            "response": (t.get("response_text") or "")[:_SUMMARY_RESPONSE_MAX_CHARS],
            "prm_score": t.get("prm_score"),
        }
        # Carry rollout metadata so the summarizer can reason per-rollout
        ri = t.get("_rollout_idx")
        if ri is not None:
            interaction["rollout_idx"] = ri
            rs = t.get("_rollout_score")
            if rs is not None:
                interaction["rollout_score"] = rs
            rsu = t.get("_rollout_success")
            if rsu is not None:
                interaction["rollout_success"] = rsu

        read_skills = t.get("read_skills") or []
        if read_skills:
            interaction["read_skills"] = [
                s.get("skill_name", "") if isinstance(s, dict) else str(s or "") for s in read_skills
            ]
        modified_skills = t.get("modified_skills") or []
        if modified_skills:
            interaction["modified_skills"] = [
                s.get("skill_name", "") if isinstance(s, dict) else str(s or "") for s in modified_skills
            ]
        injected = t.get("injected_skills") or []
        if injected:
            interaction["injected_skills"] = injected
        tc = compact_tool_calls(t.get("tool_calls"), max_items=6)
        if tc:
            interaction["tool_calls"] = tc
        tr = compact_tool_observations(t.get("tool_results"), max_items=6)
        if tr:
            interaction["tool_results"] = tr
        to = compact_tool_observations(t.get("tool_observations"), max_items=4)
        if to:
            interaction["tool_observations"] = to
        te = t.get("tool_errors") or []
        if te:
            interaction["tool_errors"] = te
        usage = (
            t.get("context_usage")
            if isinstance(t.get("context_usage"), dict)
            and t.get("context_usage", {}).get("verified") is True
            else {}
        )
        if usage:
            interaction["context_usage"] = {
                "context_snapshot_id": str(
                    usage.get("context_snapshot_id") or ""
                ),
                "skill_refs": [
                    {
                        "context_ref": str(item.get("context_ref") or ""),
                        "scope": str(item.get("scope") or ""),
                        "qualified_skill_id": str(
                            item.get("qualified_skill_id") or ""
                        ),
                        "version": str(item.get("version") or ""),
                        "operation": str(item.get("operation") or ""),
                    }
                    for item in usage.get("skill_refs") or []
                    if isinstance(item, dict)
                ],
                "feedback": (
                    dict(usage.get("feedback"))
                    if isinstance(usage.get("feedback"), dict)
                    else {}
                ),
            }
        interactions.append(interaction)

    payload: dict[str, Any] = {
        "session_id": session.get("session_id", ""),
        "total_interactions": len(turns),
        "interactions": interactions,
    }
    agg = session.get("aggregate")
    if agg:
        payload["aggregate"] = {
            "rollout_count": agg.get("rollout_count"),
            "scores": agg.get("scores"),
            "mean_score": agg.get("mean_score"),
            "success_count": agg.get("success_count"),
            "fail_count": agg.get("fail_count"),
            "stability": agg.get("stability"),
        }
    return payload


# ------------------------------------------------------------------ #
#  Metadata extraction                                                 #
# ------------------------------------------------------------------ #


def _extract_session_metadata(session: dict) -> None:
    """Extract skill references and compute aggregate metrics for a session.

    Attaches the following keys directly to the session dict:
    - ``_skills_referenced``: set of skill names explicitly read, used, or
      modified by any interaction. Prompt-time injected skill catalog entries
      are only exposure metadata and do not count as actual skill references.
    - ``_skills_injected``: set of skill names exposed in the prompt catalog.
    - ``_prm_scores``: list of all non-None PRM scores
    - ``_avg_prm``: mean PRM (or None if no scores)
    - ``_has_tool_errors``: True if any interaction had tool errors
    """
    skills: set[str] = set()
    injected_skills: set[str] = set()
    prm_scores: list[float] = []
    has_tool_errors = False
    verified_feedback: list[dict[str, Any]] = []

    for turn in session.get("turns", []):
        for item in turn.get("read_skills") or []:
            name = item.get("skill_name", "").strip() if isinstance(item, dict) else str(item or "").strip()
            if name:
                skills.add(name)
        for item in turn.get("modified_skills") or []:
            name = item.get("skill_name", "").strip() if isinstance(item, dict) else str(item or "").strip()
            if name:
                skills.add(name)
        # Langfuse mapper output: skill names whose files the agent actually
        # used via tool calls (e.g. exec touching skills/<name>/). This is an
        # explicit use, equivalent to read/modify for aggregation purposes.
        for item in turn.get("used_skills") or []:
            name = item.get("skill_name", "").strip() if isinstance(item, dict) else str(item or "").strip()
            if name:
                skills.add(name)
        for item in turn.get("injected_skills") or []:
            name = item.get("skill_name", "").strip() if isinstance(item, dict) else str(item or "").strip()
            if name:
                injected_skills.add(name)
        usage = (
            turn.get("context_usage")
            if isinstance(turn.get("context_usage"), dict)
            and turn.get("context_usage", {}).get("verified") is True
            else {}
        )
        for item in usage.get("skill_refs") or []:
            if not isinstance(item, dict) or item.get("scope") != "team_skills":
                continue
            qualified = str(
                item.get("qualified_skill_id")
                or item.get("skill_name")
                or ""
            ).strip()
            name = qualified.removeprefix("team:")
            if name:
                skills.add(name)
        if usage.get("skill_refs") and isinstance(usage.get("feedback"), dict):
            verified_feedback.append(dict(usage["feedback"]))
        prm = turn.get("prm_score")
        if prm is not None:
            prm_scores.append(prm)
        if turn.get("tool_errors"):
            has_tool_errors = True

    if skills:
        session["_skills_referenced"] = skills
        session["_skills_reference_source"] = "explicit"
    else:
        session["_skills_referenced"] = set()
        session["_skills_reference_source"] = "catalog_only" if injected_skills else "none"
    session["_skills_injected"] = injected_skills
    session["_prm_scores"] = prm_scores
    session["_avg_prm"] = round(sum(prm_scores) / len(prm_scores), 3) if prm_scores else None
    session["_has_tool_errors"] = has_tool_errors
    session["_verified_skill_feedback"] = verified_feedback


# ------------------------------------------------------------------ #
#  Public API                                                          #
# ------------------------------------------------------------------ #


def set_summarizer_debug_dir(path: str) -> None:
    """Retained no-op-friendly setter for the summarizer debug directory.

    The standalone summarize LLM stage (and its debug artifact dump) was merged
    into ``stages/analyze.py``. The orchestrator still calls this to record a
    debug directory; the value is kept on the ContextVar for any future opt-in
    tooling but no artifacts are written from this module anymore.
    """
    _SUMMARIZER_DEBUG_DIR.set(str(path or "").strip())
