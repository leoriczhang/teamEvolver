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

from team_skills.evolution.agent.cards import build_merge_card, build_round1_user_msg, render_observation_message
from team_skills.evolution.agent.contract import (
    normalize_classification,
    validate_final,
)
from team_skills.evolution.agent.loop import LoopPlanResult, run_editor_loop
from team_skills.evolution.agent.tools import ToolContext, ToolRegistry, build_protocol_appendix
from team_skills.evolution.kernel.enums import DecisionAction

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
        from team_skills.evolution.kernel.bundle_changes import materialize_bundle_changes
        from team_skills.library.bundle import candidate_skill_bundle

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
) -> Optional[dict]:
    """Close the loop from staged state when rounds ran out without a final."""
    if stage == "merge":
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


class _SkillLoopAdapter:
    """Keep Skill evidence gates, tools and final materialization asset-owned."""

    final_action_default = "merge"

    def __init__(self, *, stage: str, skill_name: str, ctx: ToolContext, registry: ToolRegistry):
        self.stage = stage
        self.skill_name = skill_name
        self.ctx = ctx
        self.registry = registry
        self.plan_record: Optional[dict] = None
        self.plan_action: Optional[str] = None
        self.plan_corrected = False

    def plan(self, plan: dict, *, round_no: int, max_rounds: int) -> LoopPlanResult:
        candidate = str(plan.get("action_candidate") or "").strip()
        classification = normalize_classification(plan.get("evidence_classification"))
        if candidate == DecisionAction.SKIP:
            return LoopPlanResult(done=True, log_kind="plan_skip", result={
                "action": DecisionAction.SKIP,
                "rationale": str(plan.get("rationale") or ""),
                "evidence_classification": classification,
            })
        if not classification["team_skill"] and self.stage != "merge":
            if not self.plan_corrected:
                self.plan_corrected = True
                return LoopPlanResult(log_kind="plan_correction", message=_plan_correction_message(plan))
            return LoopPlanResult(done=True, log_kind="plan_gate_skip", result={
                "action": DecisionAction.SKIP,
                "rationale": (
                    "Plan gate: action_candidate 非空但 team_skill 证据仍为空，终局 skip。"
                    + str(plan.get("rationale") or "")
                ).strip(),
                "evidence_classification": classification,
            })
        self.plan_record = plan
        self.plan_action = candidate or None
        return LoopPlanResult(log_fields={"action": candidate}, message=self.observe(
            [{"tool": "(loop)", "ok": True, "message": "计划已确认。现在按计划调用工具，完成后提交 final。"}],
            round_no=round_no, max_rounds=max_rounds,
        ))

    async def tools(self, calls: list[dict]) -> list[dict]:
        return await self.registry.execute(calls)

    def finalize(self, decision: dict) -> tuple[Optional[dict], list[str]]:
        normalized, errors = validate_final(decision, stage=self.stage)
        if errors:
            return None, errors
        return _accept_final(normalized, self.ctx, self.stage)

    def observe(self, observations: list[dict], *, round_no: int, max_rounds: int,
                must_submit: bool = False) -> str:
        return render_observation_message(
            _loop_state(self.ctx, self.plan_action, round_no, max_rounds),
            observations, must_submit=must_submit,
        )

    def fallback(self) -> Optional[dict]:
        return _best_effort(self.stage, self.skill_name, self.ctx, self.plan_record)


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

    def dump(round_no: int, system: str, user_msg: str, raw: Optional[str]) -> None:
        suffix = "" if round_no <= 1 else f"_round{round_no:02d}"
        _dump(debug_stem, suffix, system, user_msg, raw)

    return await run_editor_loop(
        llm,
        adapter=_SkillLoopAdapter(stage=stage, skill_name=skill_name, ctx=ctx, registry=registry),
        system=system,
        first_user=round1_user,
        max_rounds=max_rounds,
        call_options=call_options,
        trace_kwargs=trace_kwargs,
        debug_dump=dump,
        round_log=round_log,
    )
