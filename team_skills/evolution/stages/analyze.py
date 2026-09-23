"""Merged session analysis: one LLM call for value classification, summary,
and session-level judging.

Historically a session went through three separate LLM stages:

1. ``session_filter`` — decide whether the session is worth evolving.
2. ``summarize`` — produce a trajectory-aware analytical summary.
3. ``judge`` — score the session and tag evolution evidence.

They all consume the same trajectory, so this stage folds them into a single
call. To keep the output stable, the response is split into tagged sections
instead of one large JSON object — the long free-text summary lives in its own
tag, and each of the other two sections carries a small JSON payload:

  <classification>{"decision": ..., "confidence": ..., "reason": ...}</classification>
  <summary>...8-15 句纯文本...</summary>
  <judge>{...four dims, overall_score, evolution_evidence, reasons, rationale...}</judge>

The sections are split back into the exact three consumer shapes the rest of
the pipeline already expects, so no downstream reader changes:

- ``value_judge`` dict: ``decision`` / ``confidence`` / ``reason`` / ``mode``
  (see ``session_filter``; ``memory_candidates`` is kept as an empty list for
  shape parity only — no consumer reads it).
- ``session["_summary"]`` text (see ``summarize``).
- ``session["_judge_scores"]`` / ``_avg_prm`` via ``_apply_judge_scores``
  (see ``judge``), including ``evolution_evidence`` / ``evidence_reason``. The
  ``<judge>`` payload is the exact judge schema, so it is parsed by the judge
  stage's own ``_parse_scores`` — scoring stays byte-identical to standalone.

The focus block is chosen programmatically from the input session (whether it
actually used team Skills) and only the applicable branch is injected — the
prompt never carries both:

- used team Skills: focus on those Skills' own behaviour (routing correctness,
  spec violations, user corrections, defects) as the primary evidence.
- no team Skills: focus on whether the session contains reusable team SOP that
  could evolve into a NEW Skill.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

from team_skills.evolution.stages.judge import (
    _apply_judge_scores,
    _extract_json_object,
    _extract_output_artifacts,
    _extract_source_artifacts,
    _parse_scores,
)
from team_skills.evolution.stages.summarize import (
    _build_session_payload,
    _extract_session_metadata,
    build_session_trajectory,
)
from teamEvolver.llm import AsyncLLMClient

_VALID_DECISIONS = {"valuable", "memory_candidate", "task_only", "chitchat"}


class SessionAnalysisError(RuntimeError):
    """Raised when mandatory Session Analyze cannot produce complete outputs."""


def _section(text: Any, tag: str) -> Optional[str]:
    """Extract the inner content of ``<tag>...</tag>`` from the reply.

    Returns ``None`` when the tag is absent (caller decides the fallback). The
    match is case-insensitive and tolerant of surrounding whitespace; the last
    occurrence wins so a tag echoed inside the prompt preamble cannot shadow the
    real answer.
    """
    raw = str(text or "")
    pattern = re.compile(
        rf"<{re.escape(tag)}\s*>(.*?)</{re.escape(tag)}\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    matches = pattern.findall(raw)
    if not matches:
        return None
    return matches[-1].strip()


_ANALYZE_SYSTEM = """\
你是 teamEvolver 轨迹的会话级分析器。给定一个已完成的 agent 会话，你会收到：
- 无损轨迹（lossless trajectory）
- agent 读取过的源工件（source artifacts）
- 当 agent 写过文件时其最终输出工件的提取内容
- 轻量元数据（历史 PRM 分数、工具错误标记、已用/被注入技能等）

你要在**一次**分析中同时完成三件事，并严格按后文规定的 <classification>/<summary>/<judge> 三个标签分块输出：
(1) 价值分类 decision：判断该会话是否应进入 Skill 演化流水线。
(2) 轨迹摘要 summary：面向轨迹的分析式摘要（8-15 句）。
(3) 会话评分：按四个维度打分、标注演化证据，并提炼 Skill 使用经验。

不要通过关键词匹配或固定短语来判断；请基于完整交互序列（含用户纠正与最终结果）综合判断。技能被注入（injected）只说明它对 agent 可见，本身不构成使用证据；已使用的技能、工具调用、具体操作过程与任务结果才是更强证据。

## 分类准则（decision）
- valuable：包含可复用的团队 Skill 证据——执行过的工作流、明确产出、因果性的技能缺陷、领域操作规程，或针对产出的用户反馈。
- memory_candidate：有用证据属于用户个人偏好/习惯而非团队 SOP，且在该用户未来任务中仍可能有用。不要把一次交付物的明确要求提升为用户记忆。
- task_only：真实任务请求，但尚无完成结果或可演化证据。
- chitchat：仅社交闲聊、空会话或非任务交互。

## 摘要要求（summary）
面向轨迹的分析式摘要（8-15 句），需涵盖：
1. 目标：用户想要完成的总体任务。
2. 关键轨迹：agent 逐步路径——尝试了什么、按什么顺序、为什么（例如“读取技能 X → 尝试方案 Y → 遇到错误 Z → 改用 W”）。
3. 技能有效性：对每个被读取/注入/修改的技能，它是有所帮助还是有所妨碍？是否相关？是否缺少指引或指引有误？
4. 关键转折点：哪些环节顺利、哪些出错，失败由什么导致、成功靠什么促成。
5. 工具使用模式：哪些工具用得当、哪些引发错误、有无反复出现的模式。
6. 结果：最终产出的质量，以及哪些地方本可以做得更好。
重点保留事件的发生顺序和因果关系，并具体说明哪些技能指引起了作用、哪些缺失、哪些产生了误导。

## 评分维度（0.0-1.0）
- task_completion：用户目标是否完成
- response_quality：最终结果的正确性、完整性与清晰度
- efficiency：执行路径是否避免不必要的重试/绕路
- tool_usage：工具使用是否恰当且有效
总评权重：task_completion 0.55、response_quality 0.30、efficiency 0.05、tool_usage 0.10。

评分刻度：
- 1.0 表示该维度明显优秀；0.5 表示好坏参半/不确定/部分成功；0.0 表示明显失败。
- 以轨迹为事实依据（ground truth）；摘要仅作为辅助分析。
- 区分“缺少证据”与“明确失败”，证据不足时宁可保守，不要走极端。
- 不要假设存在基准（benchmark）标签。把事实正确性和目标完成度置于表面润色之上。

通用评判原则（适用于任何具体场景）：
- 按用户目标与可交付结果判断交付形式。除非用户明确要求特定载体（文件格式/可编辑对象）或事后拒绝了该交付，否则能够等效达成目标的替代形式不扣分。
- 区分与任务求解无关的环境/框架/启动噪音（无害的开场读取、初始化、短暂非阻塞绕路）与实质性无效劳动（反复失败的重试、长时间打转、大量无关工作）。只对后者扣效率分。
- 基于实际观测到的路径与结果评判，不因存在其他同样可行的路径或 Skill 而扣分。
- 不得基于“某技能/工具可能适用”的推测扣 tool_usage 分；只有轨迹中存在明确证据（读取过该技能/工具的文档，或其说明明确覆盖该请求）时，才能作为未用对工具的依据。
- 当请求超出 agent 能力或知识范围，且 agent 诚实说明、做出合理的路由或澄清尝试时，按过程质量（沟通与路由正确性）评分，不要因客观不可答而给 task_completion 极低分；这类会话应标记为 defect 演化证据（知识缺口），而非劣质执行。
- 如果会话包含具体的输出工件（例如 agent 写入的文件内容），将其作为 task_completion 与 response_quality 的强证据；如果包含从工作区读取的源工件，将其作为判断最终输出是否准确的主要事实依据。
- 当写入的产出符合要求的 schema/格式且与现有证据一致时，即使前期探索比较混乱，也应主要基于产出的正确性来打完成度/质量分。
- 只有当最终产出缺失、格式错误、与证据明显矛盾或缺乏事实支撑时，才大幅降低完成度/质量分。

## 演化证据（evolution_evidence）
- "defect"：暴露可复用的技能/流程缺陷——错误的技能或工具路由、违反技能规范、被用户纠正、反复重试同一错误，或诚实暴露的知识/能力缺口（含无产出但源于知识缺失的失败）。
- "exemplary"：特别优秀的执行范例——技能/工具路由正确且理由清晰，产出高质量且经得起源工件核对，值得作为范例沉淀供团队复用。
- "none"：常规成功或失败，无上述沉淀价值。
当 evolution_evidence 不是 "none" 时，用 evidence_reason 中文简述依据（1-3 句）。宁可标 "none"，不要为标而标。

## Skill 使用经验（skill_experiences）
仅对本会话**实际使用**且有明确因果证据的 Skill 输出经验；没有可靠经验时输出空数组。
Skill 是否已上传到 teamEvolver 的 Skill 库不影响经验生成；只要名称来自输入 `used_skills`，就按实际使用的 Skill 记录。
每条经验包含：
- `skill_name`：必须与输入 `used_skills` 中的名称完全一致。
- `kind`：`defect` 表示 Agent 使用该 Skill 时反复出错、被纠正或受错误指引影响；`exemplary` 表示值得复用的优秀做法。
- `experience_key`：用简短、稳定的 lower_snake_case 英文短语描述根因或做法。同类问题在不同会话中必须尽量使用同一个 key，以便累计发生次数。
- `description`：一段可独立阅读的中文描述，写清发生了什么、为什么好或不好，以及下次应保持或避免什么。不要写次数，次数由系统累计；不要泛泛评价 Agent。

## 输出格式（严格按下面三个标签分块输出，标签外不要写任何其它文本，不要 markdown 围栏）
先输出分类块，再输出摘要块，最后输出评分块。<classification> 和 <judge> 内各是一个**JSON 对象**；<summary> 内是纯文本（不要 JSON）。

<classification>
{"decision": "valuable|memory_candidate|task_only|chitchat", "confidence": <0..1>, "reason": "<简短中文理由，专有名词可保留英文>"}
</classification>
<summary>
<8-15 句中文轨迹摘要，纯文本>
</summary>
<judge>
{"task_completion": <0..1>, "response_quality": <0..1>, "efficiency": <0..1>, "tool_usage": <0..1>, "overall_score": <0..1>, "evolution_evidence": "defect|exemplary|none", "evidence_reason": "<中文说明，none 时可省略>", "skill_experiences": [{"skill_name": "<实际使用的 Skill>", "kind": "defect|exemplary", "experience_key": "<stable_lower_snake_case_key>", "description": "<一段清晰、可复用的中文经验>"}], "reasons": {"task_completion": ["<要点>"], "response_quality": ["<要点>"], "efficiency": ["<要点>"], "tool_usage": ["<要点>"]}, "rationale": "<简要中文说明>"}
</judge>

reasons 中必须为四个维度各写 1-4 个评分要点，要求：每个要点单独成条、一句话说明一个事实或判断；必须引用轨迹中的具体依据（工具调用及其结果、最终产出内容、错误信息、轮次数等），不要泛泛而谈；要点必须与该维度分数一致（高分说明做得好的具体表现，低分指出具体问题或缺失证据）；每个维度的要点合在一起应能独立解释该维度的分数。summary、reason、reasons 要点、rationale、evidence_reason 一律用中文书写，工具名/命令/路径/报错等专有名词可保留英文原文，但不要中英混杂。
"""

# Focus blocks: exactly one is injected based on whether the input session
# actually used team Skills. The prompt never carries both branches.
_FOCUS_WITH_SKILLS = """\
## 本次分析重点（该会话使用了团队 Skill：{skills}）
重点分析这些被使用 Skill 本身的表现——路由是否正确、是否符合 Skill 规范、是否被用户纠正、是否反复失败或暴露缺陷。此类问题应作为对既有 Skill 的改进证据（evolution_evidence 倾向 "defect"）。summary 与 reasons 要点必须具体指向是哪个 Skill、哪一步、什么问题或什么做得好。"""

_FOCUS_NO_SKILLS = """\
## 本次分析重点（该会话未使用任何团队 Skill）
重点判断会话是否包含可沉淀为**新** Skill 的团队 SOP——是否存在可复用、可泛化的任务方法/操作规程。若存在且证据充分，decision 应为 valuable 并在 summary 中说明可抽象出的 SOP；若只是一次性、无法泛化的操作，则不要拔高为 valuable。"""

_EXPERIENCE_OUTPUT_ADDENDUM = """\
## 兼容要求：Skill 使用经验
<judge> JSON 必须包含 `skill_experiences` 数组。只记录本会话实际使用且有明确因果证据的 Skill；
每项包含 skill_name、kind（defect 或 exemplary）、稳定的 experience_key 和一段中文 description。
skill_name 必须来自 used_skills，但对应 Skill 无需预先上传到 teamEvolver Skill 库。
没有可靠经验时输出空数组。"""


def _effective_analyze_system() -> str:
    try:
        from team_skills.evolution.prompt_studio import effective_prompt

        return effective_prompt("analyze_session", _ANALYZE_SYSTEM)
    except Exception:  # noqa: BLE001 - prompt wiring must not break the pipeline
        return _ANALYZE_SYSTEM


def _compose_system(base: str, used_skills: list[str]) -> str:
    """Append exactly the focus block that matches this session.

    The branch is decided programmatically from the input session's actual
    team-Skill usage; only one block is injected so the model is never asked to
    pick between two branches.
    """
    if used_skills:
        focus = _FOCUS_WITH_SKILLS.format(skills=", ".join(used_skills[:20]))
    else:
        focus = _FOCUS_NO_SKILLS
    addendum = "" if base == _ANALYZE_SYSTEM else f"\n\n{_EXPERIENCE_OUTPUT_ADDENDUM}"
    return f"{base}\n\n{focus}{addendum}"


def _analyze_call_options() -> dict[str, Any]:
    try:
        from team_skills.evolution.prompt_studio import stage_call_options

        return stage_call_options("analyze_session")
    except Exception:  # noqa: BLE001 - retain stable defaults if settings corrupt
        return {"max_tokens": 32768, "temperature": 0.1}


def _session_used_skill_names(session: dict[str, Any]) -> list[str]:
    """Union of team Skills actually used across the session (top level + turns)."""
    names: list[str] = []
    top = session.get("used_skills")
    if isinstance(top, list):
        names.extend(str(s) for s in top if s)
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        used = turn.get("used_skills")
        if isinstance(used, list):
            names.extend(str(s) for s in used if s)
    seen: set[str] = set()
    return [s for s in names if s and not (s in seen or seen.add(s))]


def build_analysis_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Full-trajectory payload for the merged analysis call.

    Combines the summarizer's interaction payload with the judge's source /
    output artifacts and lightweight metadata. ``used_skills`` is included as
    factual context (which Skills to critique); the focus-block choice itself
    is made programmatically in :func:`_compose_system`, not by the model.
    """
    payload = _build_session_payload(session)
    used_skills = _session_used_skill_names(session)
    payload["used_skills"] = used_skills
    payload["skills_referenced"] = sorted(session.get("_skills_referenced") or [])
    payload["has_tool_errors"] = bool(session.get("_has_tool_errors"))
    payload["prior_prm_scores"] = list(session.get("_prm_scores") or [])
    payload["avg_prm_before_judge"] = session.get("_avg_prm")
    payload["trajectory"] = session.get("_trajectory") or ""
    source_artifacts = _extract_source_artifacts(session)
    if source_artifacts:
        payload["source_artifacts"] = source_artifacts
    output_artifacts = _extract_output_artifacts(session)
    if output_artifacts:
        payload["output_artifacts"] = output_artifacts
    return payload


def _value_judge_from_payload(payload: dict[str, Any], *, model: str) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in _VALID_DECISIONS:
        return None
    confidence = payload.get("confidence")
    reason = payload.get("reason")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        return None
    reason = reason.strip()
    if len(reason) > 500:
        reason = reason[:500]
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        # No consumer reads this list (verified across the codebase); kept as an
        # empty list only so the value_judge dict shape matches session_filter's.
        "memory_candidates": [],
        "mode": "merged",
        "model": model,
    }


async def analyze_session(
    llm: AsyncLLMClient,
    session: dict[str, Any],
) -> dict[str, Any]:
    """Run the merged analysis for one session.

    Side effects (to preserve the three legacy consumer contracts):
    - ``session["_trajectory"]`` and metadata are (re)built.
    - ``session["_summary"]`` is set to the merged summary text.
    - ``session["_judge_scores"]`` / ``_prm_scores`` / ``_avg_prm`` are applied
      when the merged JSON carries a full set of dimension scores.

    Returns the ``value_judge`` dict (decision/confidence/reason/mode/model;
    ``memory_candidates`` is always an empty list — no consumer reads it).
    Raises :class:`SessionAnalysisError` when the model call fails or any of the
    required classification, summary, or judge outputs is missing. Callers must
    not continue ingest with a heuristic substitute.
    """
    if llm is None:
        raise SessionAnalysisError("Session Analyze requires a configured LLM client")
    _extract_session_metadata(session)
    session["_trajectory"] = build_session_trajectory(session)

    used_skills = _session_used_skill_names(session)
    payload = build_analysis_payload(session)
    system_prompt = _compose_system(_effective_analyze_system(), used_skills)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        raw = await llm.chat(
            messages,
            **_analyze_call_options(),
            trace_name="team_skills.evolution.analyze_session",
            trace_tags=["evolve", "analyze"],
            trace_metadata={
                "component": "team_skills.evolution",
                "operation": "analyze_session",
                "source_session_id": str(session.get("session_id") or ""),
            },
        )
    except Exception as exc:  # noqa: BLE001 - normalize provider failures
        raise SessionAnalysisError(
            f"Session Analyze failed for {session.get('session_id') or '<unknown>'}: {exc}"
        ) from exc

    # All three tagged sections are mandatory. Persist nothing until the whole
    # response validates, so callers cannot observe a partially analyzed
    # session after a provider or schema failure.
    classification_raw = _section(raw, "classification")
    summary_raw = _section(raw, "summary")
    judge_raw = _section(raw, "judge")
    summary_text = (summary_raw if summary_raw is not None else "").strip()
    parsed = _extract_json_object(classification_raw) if classification_raw is not None else {}
    value_judge = _value_judge_from_payload(parsed, model=str(getattr(llm, "model", "") or ""))
    scores = _parse_scores(judge_raw) if judge_raw is not None else None

    missing: list[str] = []
    if value_judge is None:
        missing.append("classification")
    if not summary_text:
        missing.append("summary")
    if scores is None:
        missing.append("judge")
    if missing:
        raise SessionAnalysisError(
            "Session Analyze result incomplete for "
            f"{session.get('session_id') or '<unknown>'}: missing/invalid "
            + ", ".join(missing)
        )

    experiences = [
        item
        for item in scores.get("skill_experiences") or []
        if str(item.get("skill_name") or "") in used_skills
    ]
    if "skill_experiences" in scores:
        scores["skill_experiences"] = experiences

    session["_summary"] = summary_text
    scores["source"] = "llm"
    _apply_judge_scores(session, scores)
    session["value_judge"] = value_judge
    return value_judge


def session_has_merged_outputs(session: dict[str, Any]) -> bool:
    """True only for a complete merged result with an explicit model source.

    Lets the evolution cycle reuse a complete analysis already persisted at
    ingest time.
    """
    if not str(session.get("_summary") or "").strip():
        return False
    scores = session.get("_judge_scores")
    verdict = session.get("value_judge")
    if (
        not isinstance(scores, dict)
        or scores.get("source") != "llm"
        or not isinstance(verdict, dict)
        or verdict.get("mode") != "merged"
        or _value_judge_from_payload(verdict, model="") is None
    ):
        return False
    return all(
        isinstance(scores.get(key), (int, float))
        and not isinstance(scores[key], bool)
        and math.isfinite(scores[key])
        and 0.0 <= scores[key] <= 1.0
        for key in ("overall_score", "task_completion", "response_quality", "efficiency", "tool_usage")
    )
