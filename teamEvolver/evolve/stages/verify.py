"""
Optional post-generation verifier for workflow-evolved skills.

When enabled, this verifier runs after a candidate skill is generated but
before it is uploaded to shared storage. It is intentionally conservative:
if the verifier cannot confidently approve the candidate, the upload is
blocked and the rejection reason is recorded in the evolve summary.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..kernel.enums import DecisionAction
from ..kernel.llm import AsyncLLMClient

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_SESSIONS = 8
_SUMMARY_MAX_CHARS = 1200
_TRAJECTORY_MAX_CHARS = 3000
_RESPONSE_TAIL_MAX_CHARS = 2000
_TOOL_RESULT_MAX_CHARS = 700
_SKILL_CONTENT_MAX_CHARS = 8000
_CURRENT_SKILL_MAX_CHARS = 4000

_VERIFY_SKILL_SYSTEM = """\
You are the final publication gate for teamEvolver workflow evolution.

You are given:
- the proposed action
- the candidate skill
- optional current skill content
- summarized evidence from the sessions that motivated the change
- structured trajectory evidence from the same sessions, including skills
  actually loaded, tool calls, tool results, final response snippets, and
  outcome metrics

Your job is NOT to improve the skill. Your job is only to decide whether this
candidate is safe and worthwhile to publish to the shared skill store.

Approve the candidate only if ALL of the following are true:
- it is grounded in the provided evidence
- it does not throw away useful existing environment-specific facts without evidence
- it is specific and reusable rather than generic agent advice
- its proposed lessons are reusable team SOPs, not one user's preferences,
  one task's explicit requirements, or agent/runtime failures
- it is coherent enough to be shared with other users immediately

Reject the candidate if ANY of the following are true:
- it is speculative or weakly supported by the evidence
- it removes useful existing instructions, endpoints, ports, filenames, or payload details without justification
- it mostly adds generic best practices instead of environment-specific knowledge
- it promotes a personal preference or task-specific correction into shared behavior
- it attributes an interruption, retry, context loss, tool outage, or failure to
  follow already-correct instructions to the Skill
- it adds or widens a `NOT for` exclusion without causal evidence that the
  current Skill harmed results and alternative routing improved them
- it should stay as a local draft or needs more evidence before publication

Sessions with the same non-empty `evaluation_profile` are a controlled
evaluation cohort designed to learn a shared team method. Concrete narrative
sequences, ratios, schemas, token contracts, notes/audits, and acceptance rules
that independently recur across multiple users in that cohort are reusable team
evidence when they fit the Skill's purpose. Reject a candidate that erases
those demonstrated rules by replacing them with vague advice such as "honor
the requested structure." Continue to reject one-user style, tone, topic,
filename, and other personal or task-instance details.

Assess the entire trajectory. Do not treat early tool errors as proof of
failure if later tool calls/results show the agent recovered, loaded the
correct skill, produced the deliverable, or validated it.

For `optimize_description`, verify only whether the new description is a safer
and more accurate trigger than the old one. An explicitly requested output
format or technology does not by itself prove that a Skill covering that
technology is irrelevant. Capability boundaries may overlap; HTML-based
presentations, for example, may legitimately use frontend visual-design
guidance. Mere availability of another Skill is not evidence for exclusion.

For `create_skill`, verify that the new skill is genuinely distinct and
generalizable from the provided sessions.

Output EXACTLY one JSON object with:
- "decision": "accept" or "reject"
- "score": number in [0, 1]
- "reason": short explanation
- "checks": object with numeric scores in [0, 1] for:
  - "grounded_in_evidence"
  - "preserves_existing_value"
  - "specificity_and_reusability"
  - "evidence_routing"
  - "safe_to_publish"

No markdown fences. No extra text.
"""


def _clip_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = str(text or "")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    if not clean:
        return None
    try:
        obj = json.loads(clean)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    match = _JSON_BLOCK_RE.search(clean)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_score(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    score = max(0.0, min(1.0, float(value)))
    return round(score, 3)


def _normalize_checks(raw_checks: Any) -> dict[str, float]:
    if not isinstance(raw_checks, dict):
        return {}
    out: dict[str, float] = {}
    for key in (
        "grounded_in_evidence",
        "preserves_existing_value",
        "specificity_and_reusability",
        "evidence_routing",
        "safe_to_publish",
    ):
        score = _normalize_score(raw_checks.get(key))
        if score is not None:
            out[key] = score
    return out


def _compute_score(raw_score: Any, checks: dict[str, float]) -> Optional[float]:
    score = _normalize_score(raw_score)
    if score is not None:
        return score
    if not checks:
        return None
    return round(sum(checks.values()) / len(checks), 3)


def _skill_names(value: Any) -> list[str]:
    names: list[str] = []
    for item in value or []:
        if isinstance(item, dict):
            name = str(item.get("skill_name") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _tool_call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    func = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(func.get("name") or call.get("name") or "").strip()


def _compact_tool_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    return {
        "tool_name": str(result.get("tool_name") or "").strip(),
        "tool_call_id": str(result.get("tool_call_id") or "").strip(),
        "has_error": bool(result.get("has_error", False)),
        "content": _clip_text(result.get("content", ""), _TOOL_RESULT_MAX_CHARS),
    }


def _build_trajectory_evidence(session: dict[str, Any]) -> dict[str, Any]:
    turns = [turn for turn in (session.get("turns") or []) if isinstance(turn, dict)]
    used_skills = _skill_names(session.get("used_skills"))
    read_skills: list[str] = []
    tool_call_names: list[str] = []
    all_results: list[dict[str, Any]] = []
    final_response = ""
    for turn in turns:
        for name in _skill_names(turn.get("used_skills")):
            if name not in used_skills:
                used_skills.append(name)
        for name in _skill_names(turn.get("read_skills")):
            if name not in read_skills:
                read_skills.append(name)
        for call in turn.get("tool_calls") or []:
            name = _tool_call_name(call)
            if name:
                tool_call_names.append(name)
        for result in turn.get("tool_results") or []:
            compact = _compact_tool_result(result)
            if compact:
                all_results.append(compact)
        response = str(turn.get("response_text") or "").strip()
        if response:
            final_response = response
    successful_results = [item for item in all_results if not item.get("has_error")]
    failed_results = [item for item in all_results if item.get("has_error")]
    return {
        "turn_count": len(turns),
        "used_skills": used_skills,
        "read_skills": read_skills,
        "tool_call_names": tool_call_names[:20],
        "tool_call_count": len(tool_call_names),
        "successful_tool_results_tail": successful_results[-8:],
        "failed_tool_results_head": failed_results[:8],
        "final_response_tail": _clip_text(final_response[-_RESPONSE_TAIL_MAX_CHARS:], _RESPONSE_TAIL_MAX_CHARS),
        "metrics": session.get("metrics") if isinstance(session.get("metrics"), dict) else {},
    }


def _build_session_evidence(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for session in sessions[:_MAX_SESSIONS]:
        item: dict[str, Any] = {
            "session_id": str(session.get("session_id", "")),
            "summary": _clip_text(session.get("_summary", ""), _SUMMARY_MAX_CHARS),
            "trajectory": _clip_text(session.get("_trajectory", ""), _TRAJECTORY_MAX_CHARS),
            "structured_trajectory": _build_trajectory_evidence(session),
        }
        runtime_context = (
            session.get("runtime_context")
            if isinstance(session.get("runtime_context"), dict)
            else {}
        )
        evaluation_profile = str(
            runtime_context.get("evaluation_profile")
            or session.get("_evaluation_profile")
            or ""
        ).strip()
        if evaluation_profile:
            item["evaluation_profile"] = evaluation_profile
        skills = session.get("_skills_referenced")
        if skills:
            item["skills_referenced"] = sorted(str(s or "") for s in skills if str(s or ""))
        judge_scores = session.get("_judge_scores")
        if isinstance(judge_scores, dict):
            overall = _normalize_score(judge_scores.get("overall_score"))
            if overall is not None:
                item["judge_overall_score"] = overall
        avg_prm = _normalize_score(session.get("_avg_prm"))
        if avg_prm is not None:
            item["avg_prm"] = avg_prm
        evidence.append(item)
    return evidence


async def verify_skill_candidate(
    llm: AsyncLLMClient,
    skill: dict[str, Any],
    sessions: list[dict[str, Any]],
    action_type: str,
    *,
    current_skill: Optional[dict[str, Any]] = None,
    evidence_classification: Optional[dict[str, Any]] = None,
    min_score: float = 0.75,
) -> dict[str, Any]:
    """Verify a candidate skill before publishing it to shared storage."""
    payload = {
        "action": action_type,
        "candidate_skill": {
            "name": str(skill.get("name", "")),
            "description": str(skill.get("description", "")),
            "category": str(skill.get("category", "general")),
            "content": _clip_text(skill.get("content", ""), _SKILL_CONTENT_MAX_CHARS),
        },
        "current_skill": None,
        "session_evidence": _build_session_evidence(sessions),
        "planner_evidence_classification": (
            evidence_classification
            if isinstance(evidence_classification, dict)
            else {}
        ),
        "acceptance_threshold": round(float(min_score), 3),
        "notes": {
            "optimize_description_only": action_type == DecisionAction.OPTIMIZE_DESC,
            "create_skill": action_type == DecisionAction.CREATE,
        },
    }
    if current_skill:
        payload["current_skill"] = {
            "name": str(current_skill.get("name", "")),
            "description": str(current_skill.get("description", "")),
            "category": str(current_skill.get("category", "general")),
            "content": _clip_text(current_skill.get("content", ""), _CURRENT_SKILL_MAX_CHARS),
        }

    try:
        # Reasoning models burn the token budget on hidden reasoning before
        # emitting the JSON verdict; a small cap returns empty content
        # (finish_reason=length). Keep this high; the client also auto-doubles
        # the budget on an empty length-capped reply.
        raw = await llm.chat(
            [
                {"role": "system", "content": _VERIFY_SKILL_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            max_tokens=16384,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("[SkillVerifier] verifier call failed for '%s': %s", skill.get("name", ""), exc)
        return {
            "enabled": True,
            "accepted": False,
            "decision": "reject",
            "score": None,
            "threshold": round(float(min_score), 3),
            "reason": f"Verifier call failed: {exc}",
            "checks": {},
        }

    parsed = _extract_json_object(raw)
    if not parsed:
        logger.warning("[SkillVerifier] invalid verifier output for '%s'", skill.get("name", ""))
        return {
            "enabled": True,
            "accepted": False,
            "decision": "reject",
            "score": None,
            "threshold": round(float(min_score), 3),
            "reason": "Verifier returned invalid JSON.",
            "checks": {},
        }

    checks = _normalize_checks(parsed.get("checks"))
    score = _compute_score(parsed.get("score"), checks)
    decision_raw = str(parsed.get("decision", "") or "").strip().lower()
    reason = str(parsed.get("reason") or parsed.get("rationale") or parsed.get("notes") or "").strip()

    accepted = decision_raw == "accept"
    if score is not None and score < float(min_score):
        accepted = False
    if decision_raw not in {"accept", "reject"}:
        accepted = score is not None and score >= float(min_score)

    return {
        "enabled": True,
        "accepted": bool(accepted),
        "decision": "accept" if accepted else "reject",
        "score": score,
        "threshold": round(float(min_score), 3),
        "reason": reason,
        "checks": checks,
    }
