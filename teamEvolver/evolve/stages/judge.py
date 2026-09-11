"""
Session-level LLM judge for classic evolve-server sessions.

This stage runs after summarization so it can reuse the generated
``_trajectory`` and ``_summary`` fields. It only backfills sessions that
do not already have a reliable session-level score from benchmark /
aggregate pipelines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from ..kernel.llm import AsyncLLMClient

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = """\
你是 teamEvolver 轨迹的会话级评估者。

你会收到一个会话，其中包含：
- 无损轨迹（lossless trajectory）
- 一份 LLM 生成的分析摘要
- agent 读取过的源工件（source artifacts）
- 轻量元数据，例如历史 PRM 分数和工具错误标记
- 当 agent 写过文件时，其最终输出工件的提取内容

请按 0.0-1.0 为以下维度打分：
- task_completion：用户目标是否完成
- response_quality：最终结果的正确性、完整性与清晰度
- efficiency：执行路径是否避免了不必要的重试/绕路
- tool_usage：工具使用是否恰当且有效

总评使用以下权重：
- task_completion: 0.55
- response_quality: 0.30
- efficiency: 0.05
- tool_usage: 0.10

评分准则：
- 1.0 表示该维度明显优秀。
- 0.5 表示好坏参半 / 不确定 / 部分成功。
- 0.0 表示该维度明显失败。
- 以轨迹为事实依据（ground truth）；摘要仅作为辅助分析。
- 区分"缺少证据"与"明确失败"。证据不足时宁可保守，不要走极端。
- 不要假设存在基准（benchmark）标签。
- 把事实正确性和目标完成度置于表面润色之上。

通用评判原则（适用于任何具体场景）：
- 按用户目标与可交付结果判断交付形式。除非用户明确要求特定载体（文件格式/可编辑对象）
  或事后拒绝了该交付，否则能够等效达成目标的替代形式不扣分。
- 区分与任务求解无关的环境/框架/启动噪音（无害的开场读取、初始化、短暂非阻塞绕路）
  与实质性无效劳动（反复失败的重试、长时间打转、大量无关工作）。只对后者扣效率分。
- 基于实际观测到的路径与结果评判，不因存在其他同样可行的路径或 Skill 而扣分。
- 不得基于"某技能/工具可能适用"的推测扣 tool_usage 分；只有轨迹中存在明确证据
  （读取过该技能/工具的文档，或其说明明确覆盖该请求）时，才能作为未用对工具的依据。
- 当请求超出 agent 能力或知识范围，且 agent 诚实说明、做出了合理的路由或澄清尝试时，
  按过程质量（沟通与路由正确性）评分，不要因客观不可答而给 task_completion 极低分；
  这类会话应标记为 defect 演化证据（知识缺口），而非劣质执行。
- 如果会话包含具体的输出工件（例如 agent 写入的文件内容），将这些工件作为 task_completion
  和 response_quality 的强证据。
- 如果会话包含 agent 从任务工作区读取的具体源工件，将这些源工件作为判断最终输出是否准确的
  主要事实依据。
- 当写入的产出符合要求的 schema/格式，且与现有证据一致时，即使前期探索比较混乱，也应主要
  基于产出的正确性来打完成度/质量分。
- 只有当最终产出缺失、格式错误、与证据明显矛盾或缺乏事实支撑时，才大幅降低完成度/质量分。

演化证据标记：
除打分外，请判断该会话对团队技能演化的沉淀价值，输出 evolution_evidence：
- "defect"：会话暴露了可复用的技能/流程缺陷——错误的技能或工具路由、违反技能规范、
  被用户纠正、反复重试同一错误，或诚实暴露的知识/能力缺口（含无产出但源于知识缺失的失败）。
- "exemplary"：会话是特别优秀的执行范例——技能/工具路由正确且理由清晰，产出高质量且
  经得起源工件核对，值得作为范例沉淀供团队复用。
- "none"：常规成功或失败，无上述沉淀价值。
当 evolution_evidence 不是 "none" 时，用 evidence_reason 用中文简述依据（1-3 句）。
宁可标记 "none"，不要为了标记而标记。

只返回一个 JSON 对象，格式如下：
{
  "task_completion": <浮点数 0..1>,
  "response_quality": <浮点数 0..1>,
  "efficiency": <浮点数 0..1>,
  "tool_usage": <浮点数 0..1>,
  "overall_score": <浮点数 0..1>,
  "evolution_evidence": "defect|exemplary|none",
  "evidence_reason": "<中文说明，evolution_evidence 为 none 时可省略>",
  "reasons": {
    "task_completion": ["<要点>", "<要点>"],
    "response_quality": ["<要点>"],
    "efficiency": ["<要点>"],
    "tool_usage": ["<要点>"]
  },
  "rationale": "<简要说明>"
}

`reasons` 中必须为四个维度各写 1-4 个评分要点，要求：
- 每个要点单独成条（字符串数组的一项），一句话说明一个事实或判断。
- 要点必须引用轨迹中的具体依据（工具调用及其结果、最终产出内容、错误信息、轮次数等），
  不要泛泛而谈。
- 要点必须与该维度的分数一致：高分要点应说明做得好的具体表现，低分要点应指出具体的
  问题或缺失的证据。
- 每个维度的要点合在一起应能独立解释该维度的分数，不依赖其他维度的上下文。

`rationale` 必须用中文书写。`reasons` 中每个要点也必须用中文书写。工具名、命令、路径、\
报错信息等专有名词可保留英文原文，但叙述语言必须是中文——不要在同一段落中英文混杂。

不要 markdown 围栏。不要多余文本。
"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_DIMENSION_KEYS = (
    "task_completion",
    "response_quality",
    "efficiency",
    "tool_usage",
)
_WEIGHTS = {
    "task_completion": 0.55,
    "response_quality": 0.30,
    "efficiency": 0.05,
    "tool_usage": 0.10,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _effective_judge_system() -> str:
    """Return the judge system prompt, honoring any Prompt Studio override.

    Lazy import avoids a circular import; falls back to the module default so
    the pipeline is byte-identical when no override is stored.
    """
    try:
        from ..prompt_studio import effective_prompt

        return effective_prompt("judge", _JUDGE_SYSTEM)
    except Exception:  # noqa: BLE001 - never let studio wiring break the pipeline
        return _JUDGE_SYSTEM


def _judge_call_options() -> dict[str, Any]:
    try:
        from ..prompt_studio import stage_call_options

        return stage_call_options("judge")
    except Exception:  # noqa: BLE001 - retain stable stage defaults
        return {"max_tokens": 32768, "temperature": 0.1}


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
    if not _is_number(value):
        return None
    score = max(0.0, min(1.0, float(value)))
    return round(score, 3)


def _normalize_reason_list(value: Any) -> list[str]:
    """Coerce one dimension's reason payload into a list of bullet strings.

    Tolerates models that emit a single string, a multi-line string, or a
    list with empty/None entries instead of the required string array.
    """
    if isinstance(value, str):
        parts = [part.strip() for part in value.splitlines()]
        return [part for part in parts if part]
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            items.append(text)
    return items


def _parse_reasons(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Extract per-dimension scoring reasons from the judge payload.

    Accepts the canonical nested ``reasons`` object plus flat fallbacks
    (``<dim>_reasons`` / ``<dim>_reason``) so slightly-off-schema outputs
    still surface their reasons.
    """
    nested = payload.get("reasons") if isinstance(payload.get("reasons"), dict) else {}
    reasons: dict[str, list[str]] = {}
    for key in _DIMENSION_KEYS:
        items = _normalize_reason_list(nested.get(key))
        if not items:
            items = _normalize_reason_list(payload.get(f"{key}_reasons"))
        if not items:
            items = _normalize_reason_list(payload.get(f"{key}_reason"))
        if items:
            reasons[key] = items
    return reasons


def _clip_text(value: Any, max_chars: int = 8000) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _compute_weighted_overall(scores: dict[str, float]) -> float:
    total = 0.0
    for key in _DIMENSION_KEYS:
        total += scores[key] * _WEIGHTS[key]
    return round(total, 3)


def _has_benchmark_overall_score(session: dict[str, Any]) -> bool:
    benchmark = session.get("benchmark")
    if isinstance(benchmark, dict) and _is_number(benchmark.get("overall_score")):
        return True
    return False


def _has_aggregate_mean_score(session: dict[str, Any]) -> bool:
    aggregate = session.get("aggregate")
    if isinstance(aggregate, dict) and _is_number(aggregate.get("mean_score")):
        return True
    return False


def _looks_like_existing_session_level_turn_score(session: dict[str, Any]) -> bool:
    turns = session.get("turns")
    if not isinstance(turns, list) or not turns:
        return False

    last_turn = turns[-1] if isinstance(turns[-1], dict) else {}
    last_score = last_turn.get("prm_score")
    if not _is_number(last_score):
        return False
    if not (0.0 <= float(last_score) <= 1.0):
        return False

    earlier_scores = []
    for turn in turns[:-1]:
        if not isinstance(turn, dict):
            continue
        prm = turn.get("prm_score")
        if prm is not None:
            earlier_scores.append(prm)

    # Be conservative: only treat the last-turn score as a benchmark-like
    # session score when the session also carries task/aggregate metadata
    # and there are no earlier PRM scores to suggest per-turn PRM usage.
    has_benchmarkish_context = bool(session.get("task_id") or session.get("aggregate") or session.get("phase"))
    return has_benchmarkish_context and not earlier_scores


def _should_skip_judging(session: dict[str, Any]) -> bool:
    turns = session.get("turns")
    if not isinstance(turns, list) or not turns:
        return True

    existing_judge = session.get("_judge_scores")
    if isinstance(existing_judge, dict) and _is_number(existing_judge.get("overall_score")):
        return True

    if _has_benchmark_overall_score(session):
        return True
    if _has_aggregate_mean_score(session):
        return True
    if _looks_like_existing_session_level_turn_score(session):
        return True
    return False


def _build_judge_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "num_turns": session.get("num_turns"),
        "skills_referenced": sorted(session.get("_skills_referenced") or []),
        "has_tool_errors": bool(session.get("_has_tool_errors")),
        "prior_prm_scores": list(session.get("_prm_scores") or []),
        "avg_prm_before_judge": session.get("_avg_prm"),
        "source_artifacts": _extract_source_artifacts(session),
        "output_artifacts": _extract_output_artifacts(session),
        "trajectory": session.get("_trajectory") or "",
        "summary": session.get("_summary") or "",
    }


def _extract_output_artifacts(
    session: dict[str, Any],
    *,
    max_artifacts: int = 12,
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for tool_call in turn.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != "write":
                continue
            raw_args = function.get("arguments")
            if not isinstance(raw_args, str):
                continue
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                continue
            path = str(args.get("path") or "").strip()
            content = args.get("content")
            if not path or content is None:
                continue
            artifacts.append(
                {
                    "path": path,
                    "content": _clip_text(content),
                }
            )
            seen_paths.add(path)
            if len(artifacts) >= max_artifacts:
                return artifacts
        for tool_result in turn.get("tool_results") or []:
            if not isinstance(tool_result, dict):
                continue
            result = (
                tool_result.get("result")
                if isinstance(tool_result.get("result"), dict)
                else {}
            )
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            path = str(
                data.get("output_path")
                or data.get("artifact_path")
                or data.get("file_path")
                or ""
            ).strip()
            if not path or path in seen_paths:
                continue
            preview = json.dumps(data, ensure_ascii=False, default=str)
            artifact_path = Path(path).expanduser()
            try:
                if artifact_path.is_file():
                    preview = artifact_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
            except OSError:
                pass
            artifacts.append({"path": path, "content": _clip_text(preview)})
            seen_paths.add(path)
            if len(artifacts) >= max_artifacts:
                return artifacts
    return artifacts


def _extract_source_artifacts(
    session: dict[str, Any],
    *,
    max_artifacts: int = 12,
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue

        call_args_by_id: dict[str, dict[str, Any]] = {}
        for tool_call in turn.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != "read":
                continue
            raw_args = function.get("arguments")
            if not isinstance(raw_args, str):
                continue
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                continue
            call_id = str(tool_call.get("id") or "").replace("_", "")
            if call_id:
                call_args_by_id[call_id] = parsed_args

        for tool_result in turn.get("tool_results") or []:
            if not isinstance(tool_result, dict):
                continue
            if str(tool_result.get("tool_name") or "").strip() != "read":
                continue
            if bool(tool_result.get("has_error")):
                continue
            result_call_id = str(tool_result.get("tool_call_id") or "").replace("_", "")
            args = call_args_by_id.get(result_call_id)
            if not isinstance(args, dict):
                continue
            path = str(args.get("path") or "").strip()
            if not path or path in seen_paths:
                continue
            if path.startswith("/root/"):
                continue
            content = str(tool_result.get("content") or "").strip()
            if not content or content == "(see attached image)":
                continue
            artifacts.append(
                {
                    "path": path,
                    "content": _clip_text(content),
                }
            )
            seen_paths.add(path)
            if len(artifacts) >= max_artifacts:
                return artifacts
    return artifacts


def _apply_judge_scores(session: dict[str, Any], scores: dict[str, Any]) -> None:
    turns = session.get("turns") or []
    previous_prm_scores = list(session.get("_prm_scores") or [])
    previous_last_prm = None
    if turns and isinstance(turns[-1], dict):
        previous_last_prm = turns[-1].get("prm_score")
        turns[-1]["prm_score"] = scores["overall_score"]

    judge_scores = dict(scores)
    if previous_prm_scores:
        judge_scores["original_prm_scores"] = previous_prm_scores
    if previous_last_prm is not None:
        judge_scores["previous_last_prm_score"] = previous_last_prm

    session["_judge_scores"] = judge_scores
    session["_prm_scores"] = [scores["overall_score"]]
    session["_avg_prm"] = scores["overall_score"]


def _parse_scores(raw: str) -> Optional[dict[str, Any]]:
    payload = _extract_json_object(raw)
    if not payload:
        return None

    scores: dict[str, float] = {}
    for key in _DIMENSION_KEYS:
        normalized = _normalize_score(payload.get(key))
        if normalized is None:
            return None
        scores[key] = normalized

    overall = _compute_weighted_overall(scores)
    result = {
        **scores,
        "overall_score": overall,
        "rationale": str(payload.get("rationale") or "").strip(),
    }
    reasons = _parse_reasons(payload)
    if reasons:
        result["reasons"] = reasons
    raw_overall = _normalize_score(payload.get("overall_score"))
    if raw_overall is not None:
        result["model_overall_score"] = raw_overall
    evidence = str(payload.get("evolution_evidence") or "").strip().lower()
    result["evolution_evidence"] = evidence if evidence in {"defect", "exemplary", "none"} else "none"
    if result["evolution_evidence"] != "none":
        evidence_reason = str(payload.get("evidence_reason") or "").strip()
        if evidence_reason:
            result["evidence_reason"] = evidence_reason
    return result


_DEFECT_SCORE_THRESHOLD_DEFAULT = 0.5


def _defect_threshold() -> float:
    try:
        return max(
            0.0,
            min(
                1.0,
                float(
                    os.environ.get(
                        "EVOLVE_REQUEUE_DEFECT_THRESHOLD",
                        str(_DEFECT_SCORE_THRESHOLD_DEFAULT),
                    )
                ),
            ),
        )
    except ValueError:
        return _DEFECT_SCORE_THRESHOLD_DEFAULT


def judge_has_defect_evidence(scores: Any, threshold: Optional[float] = None) -> bool:
    """True when a judge result carries reusable defect evidence.

    Either the overall score falls below the requeue threshold (a failing
    session likely exposing a skill/process gap) or the judge explicitly
    flagged ``evolution_evidence == "defect"``.
    """
    if not isinstance(scores, dict):
        return False
    if threshold is None:
        threshold = _defect_threshold()
    overall = scores.get("overall_score")
    if (
        isinstance(overall, (int, float))
        and not isinstance(overall, bool)
        and float(overall) < threshold
    ):
        return True
    return str(scores.get("evolution_evidence") or "").strip().lower() == "defect"


def judge_is_exemplary(scores: Any) -> bool:
    """True when the judge flagged the session as an exemplary goodcase."""
    if not isinstance(scores, dict):
        return False
    return str(scores.get("evolution_evidence") or "").strip().lower() == "exemplary"


async def judge_session(
    llm: AsyncLLMClient,
    session: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Judge one session and backfill session-level score metadata."""
    if _should_skip_judging(session):
        return None

    payload = _build_judge_payload(session)
    messages = [
        {"role": "system", "content": _effective_judge_system()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        # Reasoning models spend the budget on hidden reasoning tokens before
        # emitting any content; on large sessions the reasoning alone can exceed
        # 8k and return empty content (finish_reason=length). Keep this high, and
        # the client also auto-doubles the budget on an empty length-capped reply.
        raw = await llm.chat(
            messages,
            **_judge_call_options(),
            trace_name="teamEvolver.evolve.judge",
            trace_tags=["evolve", "judge"],
            trace_metadata={
                "component": "teamEvolver.evolve",
                "operation": "judge",
                "source_session_id": str(
                    session.get("session_id") or ""
                ),
            },
        )
    except Exception as exc:
        logger.warning(
            "[SessionJudge] LLM call failed for session %s: %s",
            session.get("session_id"),
            exc,
        )
        return None

    scores = _parse_scores(raw)
    if not scores:
        logger.warning(
            "[SessionJudge] could not parse judge output for session %s; raw[:500]=%s",
            session.get("session_id"),
            raw[:500],
        )
        return None

    _apply_judge_scores(session, scores)
    return scores


async def judge_sessions_parallel(
    llm: AsyncLLMClient,
    sessions: list[dict[str, Any]],
) -> int:
    """Judge all sessions that lack a reliable session-level score."""
    if not sessions:
        return 0

    candidates = [session for session in sessions if not _should_skip_judging(session)]
    if not candidates:
        return 0

    results = await asyncio.gather(
        *[judge_session(llm, session) for session in candidates],
        return_exceptions=True,
    )

    judged = 0
    for session, result in zip(candidates, results):
        if isinstance(result, BaseException):
            logger.warning(
                "[SessionJudge] exception while judging session %s: %s",
                session.get("session_id"),
                result,
            )
            continue
        if result is not None:
            judged += 1

    logger.info(
        "[SessionJudge] judged %d/%d candidate sessions (%d total)",
        judged,
        len(candidates),
        len(sessions),
    )
    return judged
