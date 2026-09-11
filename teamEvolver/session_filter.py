"""Session value classification before entering skill evolution."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from .llm import AsyncLLMClient, LLMOverloadedError

logger = logging.getLogger(__name__)
_DEFAULT_CLASSIFIER_TIMEOUT_SECONDS = 60
_SESSION_CLASSIFIER_SYSTEM = (
    "你负责判断一个已完成的 agent 会话是否应进入 Skill 演化流水线。\n"
    "不要通过关键词匹配、固定短语列表或特定语言的触发词来分类。"
    "请基于完整的交互序列进行判断，包括用户的纠正和 assistant 的最终结果。\n"
    "技能被注入（injected）只说明该技能对 agent 可见，其本身并不构成使用证据。"
    "已使用的技能、工具调用、具体操作过程和任务结果才是更强的证据。\n"
    "仅当会话包含可复用的团队 Skill 证据时才返回 decision='valuable'："
    "例如执行过的工作流、明确的产出、因果性的技能缺陷、领域操作规程，"
    "或针对产出的用户反馈。当有用证据属于用户个人偏好或习惯而非团队 SOP 时，"
    "返回 decision='memory_candidate'。对于真实的任务请求但尚无完成结果或可演化证据时，"
    "返回 decision='task_only'。仅社交闲聊、空会话或非任务交互才返回 decision='chitchat'。\n"
    "不要把某次交付物的明确要求提升为用户记忆。memory candidate 必须在同用户未来的任务中"
    "仍然可能有用。\n"
    'Schema: {"decision":"valuable|memory_candidate|task_only|chitchat",'
    '"confidence":0..1,"reason":"简短理由（用中文书写，专有名词可保留英文原文）","memory_candidates":'
    '[{"preference":"...","scope":"...","evidence":"..."}]}'
)


def _effective_classifier_system() -> str:
    try:
        from .evolve.prompt_studio import effective_prompt

        return effective_prompt("session_filter", _SESSION_CLASSIFIER_SYSTEM)
    except Exception:  # noqa: BLE001 - prompt configuration must not block ingest
        return _SESSION_CLASSIFIER_SYSTEM


def _classifier_call_options() -> dict[str, Any]:
    try:
        from .evolve.prompt_studio import stage_call_options

        return stage_call_options("session_filter")
    except Exception:  # noqa: BLE001 - retain stable defaults if settings are corrupt
        return {"max_tokens": 512, "temperature": 0}


def _clip(text: str, limit: int = 8000) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _extract_json_object(text: Any) -> dict[str, Any]:
    text = str(text or "")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_DECISION_RE = re.compile(r'"decision"\s*:\s*"(valuable|memory_candidate|task_only|chitchat)"')
_CONFIDENCE_RE = re.compile(r'"confidence"\s*:\s*([0-9]+(?:\.[0-9]+)?)')
_REASON_RE = re.compile(r'"reason"\s*:\s*"([^"]*)"')


def _lenient_parse(text: Any) -> Optional[dict[str, Any]]:
    """Recover the decision from a truncated/unparseable classifier reply.

    Reasoning models may exhaust the token budget mid-JSON; strict parsing
    then discards a usable decision. Extract the individual fields instead.
    """
    raw = str(text or "")
    match = _DECISION_RE.search(raw)
    if not match:
        return None
    parsed: dict[str, Any] = {"decision": match.group(1)}
    confidence = _CONFIDENCE_RE.search(raw)
    parsed["confidence"] = 0.5
    if confidence:
        try:
            parsed["confidence"] = max(0.0, min(1.0, float(confidence.group(1))))
        except ValueError:
            pass
    reason = _REASON_RE.search(raw)
    if reason:
        parsed["reason"] = reason.group(1)
    return parsed


def _session_user_texts(session: dict[str, Any], limit: int = 20) -> list[str]:
    texts: list[str] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("prompt_text") or turn.get("instruction") or "").strip()
        if text:
            texts.append(text)
    if texts:
        return texts[:limit]
    for message in session.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = str(message.get("content") or "").strip()
        if text:
            texts.append(text)
    return texts[:limit]


def _session_summary(session: dict[str, Any]) -> dict[str, Any]:
    metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
    tool_names: list[str] = []
    interactions: list[dict[str, Any]] = []
    verified_feedback: list[dict[str, Any]] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        interaction = {
            "user": _clip(
                str(turn.get("prompt_text") or turn.get("instruction") or ""),
                4000,
            ),
            "assistant": _clip(
                str(turn.get("response_text") or turn.get("response") or ""),
                6000,
            ),
            "tool_call_count": len(turn.get("tool_calls") or []),
            "used_skills": turn.get("used_skills") or turn.get("read_skills") or [],
        }
        usage = (
            turn.get("context_usage")
            if isinstance(turn.get("context_usage"), dict)
            else {}
        )
        if usage.get("verified") is True:
            team_skill_refs = [
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
                and item.get("scope") == "team_skills"
            ]
            feedback = (
                usage.get("feedback")
                if isinstance(usage.get("feedback"), dict)
                else {}
            )
            if team_skill_refs:
                interaction["verified_team_skill_refs"] = team_skill_refs
            if feedback and team_skill_refs:
                safe_feedback = {
                    "outcome": str(
                        feedback.get("outcome")
                        or feedback.get("status")
                        or ""
                    ),
                    "error_code": str(feedback.get("error_code") or ""),
                    "correction": _clip(
                        str(feedback.get("correction") or ""),
                        2000,
                    ),
                }
                interaction["verified_skill_feedback"] = safe_feedback
                verified_feedback.append(safe_feedback)
        if interaction["user"] or interaction["assistant"]:
            interactions.append(interaction)
        for call in turn.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("name") or "").strip()
            if name and name not in tool_names:
                tool_names.append(name)
    return {
        "session_id": str(session.get("session_id") or ""),
        "title": str(session.get("title") or ""),
        "user_alias": str(session.get("user_alias") or ""),
        "user_requests": [_clip(text, 4000) for text in _session_user_texts(session)],
        "used_skills": session.get("used_skills") or [],
        "injected_skills": session.get("injected_skills") or [],
        "tool_names": tool_names[:20],
        "interactions": interactions[:20],
        "verified_skill_feedback": verified_feedback[:20],
        "metrics": {
            "interaction_turns": metrics.get("interaction_turns"),
            "tool_call_count": metrics.get("tool_call_count"),
            "total_tokens": metrics.get("total_tokens"),
        },
    }


def heuristic_classify_session(session: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    """Cheap fallback for environments without a configured classifier model."""
    summary = _session_summary(session)
    user_texts = [str(t or "").strip() for t in summary["user_requests"]]
    combined = "\n".join(user_texts).strip()
    metrics = summary["metrics"]
    tool_call_count = int(metrics.get("tool_call_count") or len(summary["tool_names"]) or 0)
    has_used_skill_signal = bool(summary["used_skills"])
    has_verified_skill_feedback = any(
        str(item.get("outcome") or "").lower()
        in {"success", "partial", "failure", "failed"}
        or bool(item.get("correction"))
        for item in summary.get("verified_skill_feedback") or []
        if isinstance(item, dict)
    )
    is_managed_eval_train = bool(session.get("defer_evolution_trigger"))

    if not combined:
        decision = "chitchat"
        confidence = 0.85
        rationale = "session has no user task text"
    elif has_verified_skill_feedback:
        decision = "valuable"
        confidence = 0.75
        rationale = "session contains verified team-Skill feedback"
    elif is_managed_eval_train and len(user_texts) >= 2:
        decision = "valuable"
        confidence = 0.8
        rationale = "controlled managed-agent training session contains explicit user feedback"
    elif tool_call_count > 0 or has_used_skill_signal:
        # Calibrated with the LLM classifier: tool/skill usage alone is not
        # reusable team-Skill evidence (the model marks such sessions
        # task_only). Only verified feedback or corrections stay valuable.
        decision = "task_only"
        confidence = 0.65
        rationale = "session used tools or skills but has no user feedback or defect evidence"
    elif len(combined) >= 80 or len(user_texts) >= 2:
        decision = "task_only"
        confidence = 0.65
        rationale = "session contains a task request but no reusable execution evidence"
    else:
        decision = "chitchat"
        confidence = 0.6
        rationale = "brief exchange without tool, skill, or reusable task signal"

    if reason:
        rationale = f"{rationale}; fallback: {reason}"
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": rationale,
        "memory_candidates": [],
        "mode": "heuristic",
    }


def _classifier_failure_reason(exc: Exception, timeout_seconds: float) -> str:
    name = type(exc).__name__
    if "Timeout" in name:
        return f"classifier_timeout after {timeout_seconds:.1f}s"
    return f"classifier_error: {name}"


def _is_verified_candidate_audit(session: dict[str, Any]) -> bool:
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), dict)
        else {}
    )
    if not str(runtime_context.get("candidate_job_id") or "").strip():
        return False
    if not str(runtime_context.get("candidate_sha256") or "").strip():
        return False
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for result in turn.get("tool_results") or []:
            if not isinstance(result, dict):
                continue
            name = str(result.get("tool_name") or "").strip()
            payload = (
                result.get("result")
                if isinstance(result.get("result"), dict)
                else {}
            )
            success = bool(
                payload.get("success")
                or result.get("success")
                or (
                    result.get("has_error") is False
                    and result.get("content")
                )
            )
            if name == "candidate_skill_gap_report" and success:
                return True
    return False


@dataclass
class SessionValueClassifier:
    """Classify whether a session is worth entering the evolution queue."""

    client: AsyncLLMClient | None = None

    @classmethod
    def from_config(cls, config) -> "SessionValueClassifier":
        api_key = str(getattr(config, "llm_api_key", "") or "").strip()
        base_url = str(getattr(config, "llm_api_base", "") or "").strip()
        stage_options = _classifier_call_options()
        model = str(
            stage_options.get("model")
            or getattr(config, "llm_model_id", "")
            or getattr(config, "model_name", "")
            or ""
        ).strip()
        if not api_key or not base_url or not model:
            return cls(client=None)
        try:
            timeout_seconds = max(
                1.0,
                float(
                    os.environ.get(
                        "TEAMEVOLVER_SESSION_CLASSIFIER_TIMEOUT_S",
                        str(_DEFAULT_CLASSIFIER_TIMEOUT_SECONDS),
                    )
                ),
            )
        except ValueError:
            timeout_seconds = _DEFAULT_CLASSIFIER_TIMEOUT_SECONDS
        try:
            return cls(
                client=AsyncLLMClient(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    max_tokens=int(stage_options["max_tokens"]),
                    temperature=float(stage_options["temperature"]),
                    timeout_seconds=timeout_seconds,
                    connect_timeout_seconds=min(3.0, timeout_seconds),
                    max_retries=1,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionFilter] classifier unavailable: %s", exc)
            return cls(client=None)

    async def classify(self, session: dict[str, Any]) -> dict[str, Any]:
        if _is_verified_candidate_audit(session):
            return {
                "decision": "valuable",
                "confidence": 1.0,
                "reason": (
                    "controlled candidate audit is anchored to a candidate "
                    "job/SHA and a successful candidate_skill_gap_report ToolResult"
                ),
                "memory_candidates": [],
                "mode": "deterministic",
            }
        if self.client is None:
            return heuristic_classify_session(session, reason="classifier model is not configured")

        summary = _session_summary(session)
        messages = [
            {
                "role": "system",
                "content": _effective_classifier_system(),
            },
            {
                "role": "user",
                "content": json.dumps(summary, ensure_ascii=False, indent=2),
            },
        ]
        try:
            raw = await self.client.chat(
                messages,
                **_classifier_call_options(),
                trace_name="teamEvolver.evolve.session_filter",
                trace_tags=["evolve", "session-filter"],
                trace_metadata={
                    "component": "teamEvolver.evolve",
                    "operation": "session_filter",
                    "source_session_id": str(
                        session.get("session_id") or ""
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMOverloadedError):
                raise
            logger.warning("[SessionFilter] classifier failed: %s", exc)
            return heuristic_classify_session(
                session,
                reason=_classifier_failure_reason(exc, self.client.timeout_seconds),
            )

        parsed = _extract_json_object(raw)
        mode = "model"
        decision = str(parsed.get("decision") or "").strip().lower()
        if decision not in {
            "valuable",
            "memory_candidate",
            "task_only",
            "chitchat",
        }:
            parsed = _lenient_parse(raw) or {}
            decision = str(parsed.get("decision") or "").strip().lower()
            mode = "model_lenient"
        if decision not in {
            "valuable",
            "memory_candidate",
            "task_only",
            "chitchat",
        }:
            return heuristic_classify_session(session, reason="classifier returned invalid JSON")
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        memory_candidates = parsed.get("memory_candidates")
        return {
            "decision": decision,
            "confidence": confidence,
            "reason": _clip(str(parsed.get("reason") or ""), 500),
            "memory_candidates": (
                memory_candidates
                if decision == "memory_candidate"
                and isinstance(memory_candidates, list)
                else []
            ),
            "mode": mode,
            "model": self.client.model,
        }
