"""The plan → act → submit agent loop runner for skill evolution stages.

One runner serves all three stages (evolve_skill / create_skill / merge);
the stage only selects the round-1 card, the tool set, and the final
schema (see tools.build_protocol_appendix). Round structure:

* Round 1 forces a plan; the plan gate exits early on skip or on missing
  team-skill evidence (one correction round first).
* Act rounds execute batched tool calls and feed observations back.
* A final turn is validated (contract.validate_final + file_changes
  pre-validation + content/staging finalize); errors become one correction
  round.
* A round cap with a must-submit warning on the penultimate round and a
  best-effort close from the staged state at exhaustion.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from team_skills.evolution.kernel.enums import DecisionAction
from team_skills.evolution.agent.cards import build_merge_card, build_round1_user_msg, render_observation_message
from team_skills.evolution.agent.contract import (
    legacy_suppress,
    normalize_classification,
    parse_turn,
    validate_final,
)
from team_skills.evolution.agent.tools import ToolContext, ToolRegistry, build_protocol_appendix

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROUNDS = 12
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 8


def _round_trace(trace_kwargs: Optional[dict], round_no: int) -> dict:
    """Reuse one trace_session per loop; annotate the round in metadata."""
    if not trace_kwargs:
        return {}
    kwargs = dict(trace_kwargs)
    metadata = dict(kwargs.get("trace_metadata") or {})
    metadata["round"] = round_no
    kwargs["trace_metadata"] = metadata
    return kwargs


def _dump(stem: str, suffix: str, system: str, user_msg: str, raw: Optional[str]) -> None:
    if not stem:
        return
    try:
        from team_skills.evolution.stages.execute import _write_debug_dump

        _write_debug_dump(f"{stem}{suffix}", system, user_msg, raw)
    except Exception:  # noqa: BLE001 - debug dumps must never break the loop
        pass


def _loop_state(ctx: ToolContext, plan_action: Optional[str], round_no: int, max_rounds: int) -> dict:
    return {
        "round": f"{round_no}/{max_rounds}",
        "stage": ctx.stage,
        "plan_action": plan_action,
        "staged": {
            "applied_edits": ctx.applied_edits,
            "content_chars": len(ctx.staged_content),
        },
        "sessions_read": list(ctx.read_session_ids),
    }


def _prevalidate_file_changes(skill: dict, ctx: ToolContext) -> str:
    """Run materialize_bundle_changes as a submit-time pre-check; '' when ok."""
    if not ctx.current_skill:
        return ""  # create stage: no bundle to validate against
    try:
        from team_skills.library.bundle import candidate_skill_bundle
        from team_skills.evolution.kernel.bundle_changes import materialize_bundle_changes

        materialize_bundle_changes(
            skill,
            current_bundle=candidate_skill_bundle(ctx.current_skill),
            file_changes=skill.get("file_changes") or [],
            editable_paths=ctx.editable_files.keys(),
            extensions=ctx.bundle_contract.get("extensions") or [".py", ".sh"],
            max_file_bytes=int(ctx.bundle_contract.get("max_file_bytes") or 262144),
            allow_delete=bool(ctx.bundle_contract.get("allow_delete", True)),
        )
        return ""
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a correction
        return f"file_changes 校验失败：{exc}"


def _accept_final(
    normalized: dict,
    ctx: ToolContext,
    skill_name: str,
    stage: str,
) -> tuple[Optional[dict], list[str]]:
    """Turn a validated decision into the final stage result (or errors)."""
    if stage == "merge" or normalized.get("action") == DecisionAction.SKIP:
        return normalized, []

    action = normalized["action"]
    skill = dict(normalized.get("skill") or {})
    if action == DecisionAction.IMPROVE:
        if not str(skill.get("content") or "").strip():
            # Only fall back to staged content when edits were actually
            # applied, or when this is a bundle-only improve (body unchanged).
            if ctx.applied_edits > 0 or skill.get("file_changes"):
                skill["content"] = ctx.staged_content
            else:
                return None, [
                    "improve_skill 既没有 content，也没有已暂存的编辑；"
                    "请先调用 propose_edits，或提供完整 content，或改提交 skip"
                ]
        skill.pop("edits", None)
        if not skill.get("description"):
            skill["description"] = str((ctx.current_skill or {}).get("description") or "")
        if not skill.get("category"):
            skill["category"] = str((ctx.current_skill or {}).get("category") or "general")
        skill["edit_stats"] = {
            "applied": ctx.applied_edits,
            "attempts": max(1, ctx.propose_attempts),
        }

    if skill.get("file_changes"):
        error = _prevalidate_file_changes(skill, ctx)
        if error:
            return None, [error]

    normalized["skill"] = skill
    return normalized, []


def _best_effort(
    stage: str,
    skill_name: str,
    ctx: ToolContext,
    plan_record: Optional[dict],
    last_final: Optional[dict],
) -> Optional[dict]:
    """Close the loop from staged state when rounds ran out without a final."""
    if stage == "merge":
        # Old semantics: a failed merge keeps the incoming version.
        return None

    classification = normalize_classification(
        (plan_record or {}).get("evidence_classification")
    )
    if (
        ctx.applied_edits > 0
        and ctx.staged_content
        and classification["team_skill"]
    ):
        skill = {
            "name": skill_name or str((ctx.current_skill or {}).get("name") or ""),
            "content": ctx.staged_content,
            "description": str((ctx.current_skill or {}).get("description") or ""),
            "category": str((ctx.current_skill or {}).get("category") or "general"),
            "edit_stats": {"applied": ctx.applied_edits, "attempts": max(1, ctx.propose_attempts)},
        }
        return {
            "action": DecisionAction.IMPROVE,
            "skill": skill,
            "rationale": "Agent loop 轮数耗尽；提交已暂存的编辑（best-effort）。",
            "evidence_classification": classification,
        }

    if plan_record is not None and str(plan_record.get("action_candidate")) == DecisionAction.SKIP:
        return {
            "action": DecisionAction.SKIP,
            "rationale": str(plan_record.get("rationale") or ""),
            "evidence_classification": classification,
        }

    if last_final is not None:
        return legacy_suppress(last_final, skill_name)
    return None


def _plan_correction_message(plan: dict) -> str:
    return render_observation_message(
        {},
        [
            {
                "tool": "(plan-gate)",
                "ok": False,
                "error": (
                    "你的 plan 声称需要 "
                    f"{str(plan.get('action_candidate') or '(未给出)')!r}，"
                    "但 evidence_classification.team_skill 为空。"
                    "只有 team_skill 证据才能修改共享 Skill。"
                    "请重新输出 plan：要么补全 team_skill 证据"
                    "（含 supporting_session_ids 与因果联系），要么选择 skip。"
                ),
            }
        ],
    )


async def run_skill_agent(
    llm: Any,
    *,
    stage: str,
    system_prompt: str,
    skill_name: str = "",
    sessions: Optional[list[dict]] = None,
    current_skill: Optional[dict] = None,
    existing_skill_names: Optional[list[str]] = None,
    evolution_context: Optional[dict] = None,
    library_reader: Optional[Callable[[str], Any]] = None,
    merge_versions: Optional[tuple[dict, dict]] = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    max_tool_calls_per_round: int = DEFAULT_MAX_TOOL_CALLS_PER_ROUND,
    call_options: Optional[dict] = None,
    trace_kwargs: Optional[dict] = None,
    bundle_contract: Optional[dict] = None,
    debug_stem: str = "",
    round_log: Optional[list[dict]] = None,
) -> Optional[dict]:
    """Run the loop for one stage; returns the stage decision or None."""
    if stage not in {"evolve_skill", "create_skill", "merge"}:
        raise ValueError(f"unknown stage: {stage}")
    if stage == "merge" and not merge_versions:
        raise ValueError("merge stage requires merge_versions")

    sessions = list(sessions or [])
    existing_skill_names = list(existing_skill_names or [])
    call_options = dict(call_options or {})
    ctx = ToolContext(
        stage=stage,
        skill_name=skill_name,
        sessions=sessions,
        current_skill=current_skill,
        existing_skill_names=existing_skill_names,
        library_reader=library_reader,
        bundle_contract=dict(bundle_contract or {}),
    )
    registry = ToolRegistry(ctx, max_calls_per_round=max_tool_calls_per_round)
    appendix = build_protocol_appendix(
        stage, max_tool_calls_per_round=max_tool_calls_per_round
    )
    system = str(system_prompt or "").replace("{skill_name}", skill_name)
    if system:
        system = f"{system}\n\n{appendix}"

    if stage == "merge":
        round1_user = build_merge_card(merge_versions[0], merge_versions[1])
    else:
        round1_user = build_round1_user_msg(
            stage=stage,
            skill_name=skill_name,
            sessions=sessions,
            current_skill=current_skill,
            existing_skill_names=existing_skill_names,
            evolution_context=evolution_context,
        )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": round1_user},
    ]
    _dump(debug_stem, "", system, round1_user, None)

    plan_record: Optional[dict] = None
    plan_action: Optional[str] = None
    plan_corrected = False
    last_final: Optional[dict] = None

    def log_round(round_no: int, kind: str, **extra: Any) -> None:
        if round_log is not None:
            round_log.append({"round": round_no, "type": kind, **extra})

    for round_no in range(1, max_rounds + 1):
        raw = await llm.chat(messages, **call_options, **_round_trace(trace_kwargs, round_no))
        _dump(
            debug_stem,
            "" if round_no == 1 else f"_round{round_no:02d}",
            system,
            str(messages[-1].get("content") or ""),
            raw,
        )

        turn = parse_turn(raw)
        if not turn.get("ok") and round_no == max_rounds:
            turn = parse_turn(raw, allow_repair=True)
        if not turn.get("ok"):
            log_round(round_no, "parse_error", error=str(turn.get("error")))
            messages.append(
                {
                    "role": "user",
                    "content": render_observation_message(
                        _loop_state(ctx, plan_action, round_no, max_rounds),
                        [
                            {
                                "tool": "(protocol)",
                                "ok": False,
                                "error": (
                                    "上一轮无法解析为协议 JSON："
                                    f"{turn.get('error')}。请重新输出一个合法的 "
                                    "JSON 对象（plan / tool_calls / final），不要围栏。"
                                ),
                            }
                        ],
                        must_submit=round_no + 1 >= max_rounds,
                    ),
                }
            )
            continue

        turn_type = turn["type"]

        if turn_type == "plan":
            plan = turn["plan"]
            candidate = str(plan.get("action_candidate") or "").strip()
            classification = normalize_classification(plan.get("evidence_classification"))
            if candidate == DecisionAction.SKIP:
                log_round(round_no, "plan_skip")
                return {
                    "action": DecisionAction.SKIP,
                    "rationale": str(plan.get("rationale") or ""),
                    "evidence_classification": classification,
                }
            if not classification["team_skill"] and stage != "merge":
                if not plan_corrected:
                    plan_corrected = True
                    log_round(round_no, "plan_correction")
                    messages.append({"role": "user", "content": _plan_correction_message(plan)})
                    continue
                log_round(round_no, "plan_gate_skip")
                return {
                    "action": DecisionAction.SKIP,
                    "rationale": (
                        "Plan gate: action_candidate 非空但 team_skill 证据仍为空，终局 skip。"
                        + str(plan.get("rationale") or "")
                    ).strip(),
                    "evidence_classification": classification,
                }
            plan_record = plan
            plan_action = candidate or None
            log_round(round_no, "plan_accepted", action=candidate)
            messages.append(
                {
                    "role": "user",
                    "content": render_observation_message(
                        _loop_state(ctx, plan_action, round_no, max_rounds),
                        [
                            {
                                "tool": "(loop)",
                                "ok": True,
                                "message": "计划已确认。现在按计划调用工具，完成后提交 final。",
                            }
                        ],
                    ),
                }
            )
            continue

        if turn_type == "tool_calls":
            observations = await registry.execute(turn["calls"])
            log_round(
                round_no,
                "tool_calls",
                tools=[str(call.get("tool") or "") for call in turn["calls"] if isinstance(call, dict)],
            )
            messages.append(
                {
                    "role": "user",
                    "content": render_observation_message(
                        _loop_state(ctx, plan_action, round_no, max_rounds),
                        observations,
                        must_submit=round_no + 1 >= max_rounds,
                    ),
                }
            )
            continue

        # final
        decision = turn["decision"]
        last_final = decision
        normalized, errors = validate_final(decision, skill_name, stage=stage)
        if not errors:
            finalized, errors = _accept_final(normalized, ctx, skill_name, stage)
            if finalized is not None:
                log_round(round_no, "final", action=str(finalized.get("action") or "merge"))
                return finalized
        log_round(round_no, "final_rejected", errors=errors)
        if round_no < max_rounds:
            messages.append(
                {
                    "role": "user",
                    "content": render_observation_message(
                        _loop_state(ctx, plan_action, round_no, max_rounds),
                        [{"tool": "(validation)", "ok": False, "errors": errors}],
                        must_submit=round_no + 1 >= max_rounds,
                    ),
                }
            )
            continue
        break

    return _best_effort(stage, skill_name, ctx, plan_record, last_final)
