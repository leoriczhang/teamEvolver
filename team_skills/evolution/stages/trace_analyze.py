"""Trace-level analysis: inspect every trace and localize any problems.

The session-level :mod:`team_skills.evolution.stages.analyze` produces one
judge verdict for a whole session (good / mixed / bad). That is enough to
decide whether a session enters the evolution queue, but it never says *where*
inside the session a problem sits. In this codebase one Trace maps to exactly
one turn (each ``turn`` carries its own ``trace_id``; see ``langfuse_convert`` /
``sf_agent_adapter``), so a trace-level analysis is a per-turn analysis.

This stage runs for every non-empty session and walks the trajectory in bounded
LLM batches. For each trace it decides whether that trace is the locus of a
problem and, when it is, tags it with a controlled ``problem_type`` and a
concrete Chinese ``problem_description``. The controlled vocabulary is aligned
with the five-way :class:`FailureType` taxonomy plus a few trace-level
categories that only make sense per turn (spec violation, user correction,
incomplete / wrong output).

For every produced trace result the module fires a hook exactly once, so a
downstream consumer (e.g. a Doris uploader) can persist each result as it is
generated. The hook itself is intentionally NOT implemented here — callers
register one via :func:`register_trace_analysis_hook`, or point
``TEAMEVOLVER_TRACE_ANALYSIS_HOOK`` at a ``module.path:function`` to plug an
uploader without touching this code. Hook dispatch is strictly fail-open: a
hook that raises is logged and never breaks analysis or ingest.
"""

from __future__ import annotations

import importlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from team_skills.evolution.stages.judge import _extract_json_object
from team_skills.evolution.stages.summarize import (
    _extract_session_metadata,
    build_session_trajectory,
)
from teamEvolver.llm import AsyncLLMClient

logger = logging.getLogger(__name__)


class TraceAnalysisError(RuntimeError):
    """A model call or its required per-trace output failed."""


# ------------------------------------------------------------------ #
# Controlled problem-type vocabulary                                  #
# ------------------------------------------------------------------ #
# Aligned with FailureType (skill stale / misselect / gap, tool error, model
# baseline) and extended with trace-level categories a per-turn view exposes.
# The LLM is constrained to this set; anything off-vocabulary normalizes to
# "other" so downstream storage always sees a known value.
PROBLEM_TYPE_LABELS: dict[str, str] = {
    "skill_content_stale": "技能内容过时",
    "skill_misselect": "技能选择错误",
    "skill_gap": "技能缺失/知识缺口",
    "tool_error": "工具使用错误",
    "model_baseline": "模型基础能力不足",
    "spec_violation": "违反规范或要求",
    "user_correction": "被用户纠正",
    "incomplete_output": "产出不完整或缺失",
    "wrong_output": "产出错误或与证据矛盾",
    "other": "其他问题",
}
_VALID_PROBLEM_TYPES = frozenset(PROBLEM_TYPE_LABELS)
_FALLBACK_PROBLEM_TYPE = "other"


def normalize_problem_type(value: Any) -> str:
    """Coerce a model-supplied problem type into the controlled vocabulary."""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in _VALID_PROBLEM_TYPES else _FALLBACK_PROBLEM_TYPE


_TRACE_ANALYZE_SYSTEM = """\
你是 teamEvolver 的 Trace 级分析器。输入是一个 agent 会话。
在本系统中一个 Trace 恰好对应一个交互轮次（turn），每个 Trace 带有唯一的 trace_id。
无论会话整体评分高低，都必须逐个分析输入中的全部 Trace。

你的任务：逐个 Trace 判断它是否是问题所在，并对存在问题的 Trace 给出**问题类型**与**问题描述**。
请基于完整轨迹（用户输入、工具调用及其结果/报错、agent 回复、评分、用户后续纠正）综合判断，
不要只看关键词。

## 判定原则
- 只把真正暴露问题的 Trace 标为 is_badcase=true：例如工具调用报错、技能路由错误、
  违反明确要求、被用户纠正、产出缺失或错误、暴露知识/能力缺口。
- 正常推进、成功的中间步骤标为 is_badcase=false，problem_type 用 "other" 且 problem_description 留空。
- 一个会话里通常只有少数几个 Trace 是真正的问题源头；正常会话也可能没有任何 BadCase，
  不要为了标注而把每个 Trace 都标成问题。
- 问题描述要具体：指出这一步做了什么、错在哪里、有什么证据
  （工具名/报错内容/被纠正的内容/缺失的产出），用中文书写。
  专有名词（工具名、命令、路径、报错）可保留英文，1-3 句。

## 问题类型（problem_type，必须取以下之一）
- skill_content_stale：使用的技能内容过时、与当前环境/接口不符导致失败。
- skill_misselect：路由到了错误的技能，或该用某技能却没用。
- skill_gap：缺少可用技能或知识，agent 无法完成（含诚实暴露的知识缺口）。
- tool_error：工具调用失败、用法错误、参数错误或反复重试同一错误。
- model_baseline：与技能/工具无关的模型自身能力问题（推理错误、遗漏要求、幻觉）。
- spec_violation：违反了用户明确的规范/格式/约束要求。
- user_correction：该步骤的产出被用户在后续轮次明确纠正或否定。
- incomplete_output：产出不完整、半途而废或缺失关键部分。
- wrong_output：产出错误、与已知事实或源工件明显矛盾。
- other：其它或该 Trace 无问题。

## 输出格式（严格输出一个 JSON 对象，不要 markdown 围栏，不要任何额外文本）
{
  "traces": [{
    "trace_id": "<对应输入的 trace_id>",
    "turn_num": <整数>,
    "is_badcase": <true|false>,
    "problem_type": "<上述取值之一>",
    "problem_description": "<中文，is_badcase=false 时可为空字符串>"
  }]
}

必须为输入中的**每一个** Trace 输出一条记录，trace_id 必须与输入完全一致，顺序与输入一致。
"""


def _effective_trace_analyze_system() -> str:
    """Resolve the (possibly operator-edited) system prompt for this stage."""
    try:
        from team_skills.evolution.prompt_studio import effective_prompt

        return effective_prompt("analyze_trace_badcase", _TRACE_ANALYZE_SYSTEM)
    except Exception:  # noqa: BLE001 - prompt wiring must never break the pipeline
        return _TRACE_ANALYZE_SYSTEM


def _trace_analyze_call_options() -> dict[str, Any]:
    try:
        from team_skills.evolution.prompt_studio import stage_call_options

        return stage_call_options("analyze_trace_badcase")
    except Exception:  # noqa: BLE001 - retain stable defaults if settings corrupt
        return {"max_tokens": 8192, "temperature": 0.1}


# ------------------------------------------------------------------ #
# Payload / metadata helpers                                          #
# ------------------------------------------------------------------ #

_MAX_TEXT = 2000
_TRACE_BATCH_SIZE = 60


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _turn_business_time(turn: dict[str, Any]) -> str:
    """Best-effort upstream (business) timestamp of one trace/turn."""
    langfuse = turn.get("_langfuse") if isinstance(turn.get("_langfuse"), dict) else {}
    for source in (turn.get("timestamp"), langfuse.get("timestamp")):
        text = str(source or "").strip()
        if text:
            return text
    return ""


def _turn_trace_id(turn: dict[str, Any], turn_num: int) -> str:
    trace_id = str(turn.get("trace_id") or "").strip()
    if trace_id:
        return trace_id
    # Fall back to a synthetic id so a turn without an upstream trace id still
    # gets a stable, addressable result row instead of collapsing onto "".
    return f"{turn_num}"


def _tool_error_signals(turn: dict[str, Any]) -> list[str]:
    """Concise tool-error strings for a turn (both explicit errors and error
    results), supplied as evidence to the model."""
    signals: list[str] = []
    for err in turn.get("tool_errors") or []:
        if isinstance(err, dict):
            name = str(err.get("tool_name") or "").strip()
            content = _clip(err.get("content", ""), 400)
            signals.append(f"{name}: {content}" if name else content)
    for res in turn.get("tool_results") or []:
        if isinstance(res, dict) and res.get("has_error"):
            name = str(res.get("tool_name") or "").strip()
            etype = str(res.get("error_type") or "").strip()
            content = _clip(res.get("content", ""), 400)
            head = " ".join(part for part in (name, etype) if part)
            signals.append(f"{head}: {content}" if head else content)
    return [s for s in signals if s]


def build_trace_payload(
    session: dict[str, Any],
    *,
    turns: Optional[list[dict[str, Any]]] = None,
    start_turn_num: int = 1,
) -> dict[str, Any]:
    """Compact per-trace payload for the localization call.

    One entry per turn (== per trace), carrying the identity and the minimal
    signals the model needs to localize a problem: prompt, response, tool
    calls/errors and any per-turn score.
    """
    selected_turns = (
        [t for t in (session.get("turns") or []) if isinstance(t, dict)]
        if turns is None
        else turns
    )
    traces: list[dict[str, Any]] = []
    for idx, turn in enumerate(selected_turns, start_turn_num):
        entry: dict[str, Any] = {
            "trace_id": _turn_trace_id(turn, idx),
            "turn_num": idx,
            "prompt": _clip(turn.get("prompt_text", "")),
            "response": _clip(turn.get("response_text", "")),
        }
        prm = turn.get("prm_score")
        if prm is not None:
            entry["score"] = prm
        errors = _tool_error_signals(turn)
        if errors:
            entry["tool_errors"] = errors[:6]
        traces.append(entry)
    return {
        "session_id": str(session.get("session_id") or ""),
        "summary": _clip(session.get("_summary", ""), 4000),
        "total_traces": len(traces),
        "traces": traces,
    }


def _identity(session: dict[str, Any]) -> dict[str, str]:
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    return {
        "user_id": str(meta.get("user_id") or session.get("user_alias") or "").strip(),
        "session_trace_id": str(meta.get("trace_id") or "").strip(),
    }


def _session_overall_score(session: dict[str, Any]) -> Optional[float]:
    scores = session.get("_judge_scores")
    if isinstance(scores, dict):
        value = scores.get("overall_score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


# ------------------------------------------------------------------ #
# Result assembly                                                     #
# ------------------------------------------------------------------ #

def _build_results(
    session: dict[str, Any],
    model_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach metadata to complete, validated model results in turn order."""
    turns = session["turns"]
    identity = _identity(session)
    ingested_at = str(session.get("ingested_at") or "").strip()
    overall = _session_overall_score(session)
    analyzed_at = datetime.now(timezone.utc).isoformat()

    results: list[dict[str, Any]] = []
    for idx, turn in enumerate(turns, 1):
        finding = model_results[idx - 1]
        problem_type = finding["problem_type"]
        results.append(
            {
                **finding,
                "session_id": str(session.get("session_id") or ""),
                "problem_type_label": PROBLEM_TYPE_LABELS.get(problem_type, ""),
                # 业务发生时间（上游 trace 时间）与系统入库时间分开呈现，便于核对。
                "business_time": _turn_business_time(turn),
                "ingested_at": ingested_at,
                "user_id": identity["user_id"],
                "session_trace_id": identity["session_trace_id"],
                "session_overall_score": overall,
                "source": "llm",
                "analyzed_at": analyzed_at,
            }
        )
    return results


def _parse_llm_analysis(
    raw: str,
    expected_traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Require exactly one explicit model verdict for each supplied turn."""
    payload = _extract_json_object(raw)
    entries = payload.get("traces") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or len(entries) != len(expected_traces):
        raise TraceAnalysisError("Trace Analyze result incomplete: one verdict per trace is required")
    expected = {trace["turn_num"]: trace["trace_id"] for trace in expected_traces}
    findings: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TraceAnalysisError("Trace Analyze returned a non-object verdict")
        turn_num = entry.get("turn_num")
        trace_id = entry.get("trace_id")
        if (
            type(turn_num) is not int
            or turn_num not in expected
            or trace_id != expected[turn_num]
            or turn_num in findings
        ):
            raise TraceAnalysisError("Trace Analyze returned an unknown or duplicate trace/turn")
        is_badcase = entry.get("is_badcase")
        description = entry.get("problem_description")
        raw_type = entry.get("problem_type")
        if (
            not isinstance(is_badcase, bool)
            or not isinstance(description, str)
            or not isinstance(raw_type, str)
            or not raw_type.strip()
            or (is_badcase and not description.strip())
        ):
            raise TraceAnalysisError("Trace Analyze returned an invalid or incomplete verdict")
        problem_type = normalize_problem_type(entry.get("problem_type"))
        if not is_badcase and (problem_type != "other" or description.strip()):
            raise TraceAnalysisError("Trace Analyze returned a contradictory normal verdict")
        findings[turn_num] = {
            "trace_id": trace_id,
            "turn_num": turn_num,
            "is_badcase": is_badcase,
            "problem_type": problem_type,
            "problem_description": description.strip(),
        }
    return [findings[trace["turn_num"]] for trace in expected_traces]


# ------------------------------------------------------------------ #
# Hook registry (dispatch one result per trace)                       #
# ------------------------------------------------------------------ #

TraceHook = Callable[[dict[str, Any]], None]

_HOOKS: list[TraceHook] = []
_ENV_HOOK_ENV = "TEAMEVOLVER_TRACE_ANALYSIS_HOOK"
_env_hook_loaded = False
_env_hook: Optional[TraceHook] = None


def register_trace_analysis_hook(hook: TraceHook) -> None:
    """Register a callable invoked once per generated trace result.

    Each hook receives a single result dict (see :func:`_build_results`).
    Duplicate registrations are ignored. Hooks should be cheap and must not
    raise for control flow — dispatch swallows exceptions.
    """
    if not callable(hook):
        raise TypeError("trace analysis hook must be callable")
    if hook not in _HOOKS:
        _HOOKS.append(hook)


def unregister_trace_analysis_hook(hook: TraceHook) -> None:
    """Remove a previously registered hook (no-op if absent)."""
    try:
        _HOOKS.remove(hook)
    except ValueError:
        pass


def clear_trace_analysis_hooks() -> None:
    """Drop all in-process hooks (test isolation / reconfiguration)."""
    _HOOKS.clear()


def _load_env_hook() -> Optional[TraceHook]:
    """Resolve ``TEAMEVOLVER_TRACE_ANALYSIS_HOOK`` = ``module.path:function``.

    Lets an operator plug an uploader (e.g. Doris) without code changes. The
    spec is resolved once and cached; a bad spec logs a warning and disables the
    env hook for the process.
    """
    global _env_hook_loaded, _env_hook
    if _env_hook_loaded:
        return _env_hook
    _env_hook_loaded = True
    spec = str(os.environ.get(_ENV_HOOK_ENV, "") or "").strip()
    if not spec:
        _env_hook = None
        return None
    module_path, _, attr = spec.partition(":")
    if not module_path or not attr:
        logger.warning(
            "[TraceAnalyze] %s must be 'module.path:function', got %r; ignoring",
            _ENV_HOOK_ENV,
            spec,
        )
        _env_hook = None
        return None
    try:
        module = importlib.import_module(module_path)
        candidate = getattr(module, attr)
    except Exception as exc:  # noqa: BLE001 - never break analysis on a bad spec
        logger.warning("[TraceAnalyze] failed to load hook %r: %s", spec, exc)
        _env_hook = None
        return None
    if not callable(candidate):
        logger.warning("[TraceAnalyze] hook target %r is not callable", spec)
        _env_hook = None
        return None
    _env_hook = candidate
    return _env_hook


def _reset_env_hook_cache() -> None:
    """Force the env hook spec to be re-resolved (test hook)."""
    global _env_hook_loaded, _env_hook
    _env_hook_loaded = False
    _env_hook = None


def dispatch_trace_result(result: dict[str, Any]) -> None:
    """Fire every configured hook for one trace result (fail-open).

    Invoked once per generated trace result. A hook raising is logged and
    swallowed so one bad consumer never blocks analysis or the ingest path.
    """
    hooks: list[TraceHook] = list(_HOOKS)
    env_hook = _load_env_hook()
    if env_hook is not None and env_hook not in hooks:
        hooks.append(env_hook)
    for hook in hooks:
        try:
            hook(result)
        except Exception:  # noqa: BLE001 - hooks are best-effort side channels
            logger.warning(
                "[TraceAnalyze] trace analysis hook failed for session=%s trace=%s",
                result.get("session_id"),
                result.get("trace_id"),
                exc_info=True,
            )


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

async def analyze_session_traces(
    llm: AsyncLLMClient,
    session: dict[str, Any],
) -> list[dict[str, Any]]:
    """Analyze every trace in one session and localize any problems.

    Returns one model result per trace, or raises when the model is unavailable
    or any verdict is invalid/missing. Hooks run only after all batches succeed.
    """
    turns = session.get("turns") or []
    if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
        raise TraceAnalysisError("Trace Analyze requires a list of turn objects")
    if not turns:
        return []
    if llm is None:
        raise TraceAnalysisError("Trace Analyze requires a configured LLM client")

    # Ensure the summary/metadata the payload references exist even when this is
    # called standalone (e.g. offline batch) rather than after analyze_session.
    _extract_session_metadata(session)
    if not str(session.get("_summary") or "").strip() and not session.get("_trajectory"):
        session["_trajectory"] = build_session_trajectory(session)

    model_results: list[dict[str, Any]] = []
    for offset in range(0, len(turns), _TRACE_BATCH_SIZE):
        batch = turns[offset : offset + _TRACE_BATCH_SIZE]
        payload = build_trace_payload(session, turns=batch, start_turn_num=offset + 1)
        messages = [
            {"role": "system", "content": _effective_trace_analyze_system()},
            {"role": "user", "content": _json_dumps(payload)},
        ]
        try:
            raw = await llm.chat(
                messages,
                **_trace_analyze_call_options(),
                trace_name="team_skills.evolution.analyze_trace_badcase",
                trace_tags=["evolve", "trace_badcase"],
                trace_metadata={
                    "component": "team_skills.evolution",
                    "operation": "analyze_trace_badcase",
                    "source_session_id": str(session.get("session_id") or ""),
                    "trace_batch_start": offset + 1,
                    "trace_batch_size": len(batch),
                },
            )
            model_results.extend(_parse_llm_analysis(raw, payload["traces"]))
        except Exception as exc:  # noqa: BLE001 - fail the whole Session, including overload
            raise TraceAnalysisError(
                f"Trace Analyze failed for {session.get('session_id') or '<unknown>'} "
                f"turns {offset + 1}-{offset + len(batch)}: {exc}"
            ) from exc

    return _build_results(session, model_results)


async def analyze_and_dispatch_traces(
    llm: AsyncLLMClient,
    session: dict[str, Any],
    *,
    fire_hook: bool = True,
) -> list[dict[str, Any]]:
    """Analyze a session's traces and fire the hook once per trace result.

    This is the single entry the ingest pipeline and the offline batch call.

    - Every non-empty session is analyzed. There is no quality gate or feature
      switch that can skip this stage.
    - For every produced trace result, :func:`dispatch_trace_result` is invoked
      exactly once (unless ``fire_hook`` is False), satisfying the contract that
      each generated trace analysis result triggers one hook call.
    """
    results = await analyze_session_traces(llm, session)
    if fire_hook:
        for result in results:
            dispatch_trace_result(result)
    return results


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------ #
# Test support                                                        #
# ------------------------------------------------------------------ #
# A real importable hook target so the env-hook loader
# (``TEAMEVOLVER_TRACE_ANALYSIS_HOOK=...:_test_env_sink``) can be exercised end
# to end. Not used by production code.
_TEST_ENV_SINK: list[dict[str, Any]] = []


def _test_env_sink(result: dict[str, Any]) -> None:
    _TEST_ENV_SINK.append(result)
