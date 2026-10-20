"""Current Prompt catalog, rendered from the pinned PG configuration in prompt_context."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)







# ------------------------------------------------------------------ #
# Pipeline chain (for visualization)                                  #
# ------------------------------------------------------------------ #
# Each node: id, label, kind ("io"|"llm"|"logic"|"gate"), optional prompt_id,
# and a short description. Edges are (from, to) and mirror the live flow:
# analyze → group → (evolve/create || dataset synthesis) → validate → publish.
PIPELINE_STAGES: dict[str, Any] = {
    "nodes": [
        {
            "id": "ingest",
            "label": "会话入队 Ingest",
            "kind": "io",
            "description": "会话经 /ingest_session 或 Langfuse 拉取后进入合并分析。",
        },
        {
            "id": "analyze_session",
            "label": "会话分析 Analyze（分类+摘要+评分合一）",
            "kind": "llm",
            "prompt_id": "analyze_session",
            "description": "每个 Session 入库前必须完成一次价值分类、轨迹摘要与会话评分。",
        },
        {
            "id": "analyze_trace_badcase",
            "label": "Trace 级全量分析",
            "kind": "llm",
            "prompt_id": "analyze_trace_badcase",
            "description": "对每个会话的全部 Trace 输出分析结果，并按结果触发上报钩子。",
        },
        {
            "id": "group",
            "label": "按技能分组 Group",
            "kind": "logic",
            "description": "按被引用/注入的技能把会话分组；无技能的会话归入 no-skill 桶。",
        },
        {
            "id": "evolve_skill",
            "label": "改进技能 Evolve",
            "kind": "llm",
            "prompt_id": "evolve_skill",
            "conditional": True,
            "parallel_group": "evidence_generation",
            "branch": "skill",
            "description": "对已有技能：基于会话证据决定 improve/optimize_description/create/skip。",
        },
        {
            "id": "create_skill",
            "label": "新建技能 Create",
            "kind": "llm",
            "prompt_id": "create_skill",
            "conditional": True,
            "parallel_group": "evidence_generation",
            "branch": "skill",
            "description": "对 no-skill 会话桶：判断是否存在可复用模式并生成新技能。",
        },
        {
            "id": "merge",
            "label": "冲突合并 Merge",
            "kind": "llm",
            "prompt_id": "merge",
            "conditional": True,
            "description": "同名技能的两个进化版本冲突时，合并为一个更优版本。",
        },
        {
            "id": "dataset_synthesis",
            "label": "测试集生成 Dataset Synthesis",
            "kind": "llm",
            "prompt_id": "dataset_synthesis",
            "parallel_group": "evidence_generation",
            "branch": "dataset",
            "description": "与技能进化并行，基于同一批 Session 和跨周期 Evidence 独立生成测试集，不读取本轮候选内容。",
        },
        {
            "id": "validate",
            "label": "真回放校验 Validate",
            "kind": "gate",
            "join": "all",
            "description": (
                "等待候选技能和测试集均完成后执行 True Replay，校验 Checklist 并比较轮次、工具调用与 Token。"
            ),
        },
        {
            "id": "replay_checklist",
            "label": "Checklist 裁判",
            "kind": "llm",
            "prompt_id": "replay_checklist",
            "description": "依据回复、工具轨迹和真实工作区产物逐条判断 Checklist 是否满足。",
        },
        {
            "id": "publish",
            "label": "发布 Publish",
            "kind": "io",
            "description": "通过校验的候选写入技能库并同步云端；不通过则进入人工复核。",
        },
    ],
    "edges": [
        {"from": "ingest", "to": "analyze_session"},
        {"from": "analyze_session", "to": "group"},
        {"from": "analyze_session", "to": "analyze_trace_badcase"},
        {"from": "group", "to": "evolve_skill"},
        {"from": "group", "to": "create_skill"},
        {"from": "evolve_skill", "to": "merge"},
        {"from": "create_skill", "to": "merge"},
        {"from": "group", "to": "dataset_synthesis"},
        {"from": "evolve_skill", "to": "validate"},
        {"from": "create_skill", "to": "validate"},
        {"from": "merge", "to": "validate"},
        {"from": "dataset_synthesis", "to": "validate"},
        {"from": "validate", "to": "replay_checklist"},
        {"from": "replay_checklist", "to": "publish"},
    ],
}


# ------------------------------------------------------------------ #
# Stage catalog                                                       #
# ------------------------------------------------------------------ #
# Each entry declares how to resolve its DEFAULT system prompt from the owning
# module. ``injects_shared_blocks`` marks the two skill-writing prompts whose
# raw template still carries the sentinels expanded by execute._inject_shared_blocks.


def _evolve_default_raw() -> str:
    # Raw template BEFORE shared-block injection (kept as-is in the module after
    # injection is applied at import). We reconstruct the editable raw form so
    # operators edit the template, not the fully-expanded text.
    from team_skills.evolution.stages import execute

    return execute._EVOLVE_FROM_SESSIONS_SYSTEM


def _create_default_raw() -> str:
    from team_skills.evolution.stages import execute

    return execute._CREATE_FROM_SESSIONS_SYSTEM


def _merge_default() -> str:
    from team_skills.evolution.stages import execute

    return execute._MERGE_SKILL_SYSTEM


def _dataset_synthesis_default() -> str:
    from team_replay.datasets import synthesis as dataset_synthesizer

    return dataset_synthesizer._SYNTHESIZE_SYSTEM


def _analyze_session_default() -> str:
    from team_skills.evolution.stages import analyze

    return analyze._ANALYZE_SYSTEM


def _analyze_trace_badcase_default() -> str:
    from team_skills.evolution.stages import trace_analyze

    return trace_analyze._TRACE_ANALYZE_SYSTEM


def _replay_checklist_default() -> str:
    from team_replay import engine as true_replay

    return true_replay._CHECKLIST_JUDGE_SYSTEM


_STAGE_CATALOG: dict[str, dict[str, Any]] = {
    "analyze_session": {
        "id": "analyze_session",
        "label": "会话分析 Analyze（分类+摘要+评分合一）",
        "module": "team_skills.evolution.stages.analyze",
        "symbol": "_ANALYZE_SYSTEM",
        "resolver": _analyze_session_default,
        "injects_shared_blocks": False,
        "temperature": 0.1,
        # One merged call carries classification + summary + full judge JSON;
        # keep the budget high so reasoning models do not truncate the combined
        # tagged output.
        "max_tokens": 32768,
        "variables": [
            "full session trajectory",
            "interactions, tool calls, artifacts",
            "used_skills split (existing-skill critique vs new-SOP discovery)",
        ],
        "description": (
            "每个 Session 入库前必须通过一次 LLM 调用完成价值分类、轨迹摘要与会话评分；"
            "三段结果缺一则入库失败。prompt 中段按是否使用团队 Skill 分支：用了则关注该 "
            "Skill 本身的表现与缺陷，没用则关注是否有可进化为新 Skill 的团队 SOP。"
        ),
    },
    "analyze_trace_badcase": {
        "id": "analyze_trace_badcase",
        "label": "Trace 级全量分析",
        "module": "team_skills.evolution.stages.trace_analyze",
        "symbol": "_TRACE_ANALYZE_SYSTEM",
        "resolver": _analyze_trace_badcase_default,
        "injects_shared_blocks": False,
        "temperature": 0.1,
        # Each bounded batch returns one JSON object covering every supplied
        # trace; the payload carries per-trace signals, not full artifacts.
        "max_tokens": 8192,
        "variables": [
            "per-trace prompt/response/score",
            "tool errors per trace",
            "session summary",
        ],
        "description": (
            "对每个会话逐 Trace 分析：每个 Trace 都产出结果，存在问题时给出受控 "
            "problem_type 与中文 problem_description，供 Trace 级结果上报（如上传 Doris）。"
        ),
    },
    "evolve_skill": {
        "id": "evolve_skill",
        "label": "改进技能 Evolve",
        "module": "team_skills.evolution.stages.execute",
        "symbol": "_EVOLVE_FROM_SESSIONS_SYSTEM",
        "resolver": _evolve_default_raw,
        "injects_shared_blocks": True,
        "temperature": 0.4,
        "max_tokens": 16384,
        "variables": [
            "{skill_name}",
            "round-1 session index card (one line per session)",
            "current skill outline card",
            "cross-cycle evidence",
            "evaluation cohort",
            "existing skill names",
        ],
        "description": (
            "对已有技能的多轮 agent 循环：第 1 轮基于会话索引卡输出 plan"
            "（improve / optimize_description / create / skip），再按需用工具加载证据"
            "并经 propose_edits 暂存编辑，final 提交。未涉及正文逐字节保留。"
        ),
    },
    "create_skill": {
        "id": "create_skill",
        "label": "新建技能 Create",
        "module": "team_skills.evolution.stages.execute",
        "symbol": "_CREATE_FROM_SESSIONS_SYSTEM",
        "resolver": _create_default_raw,
        "injects_shared_blocks": True,
        "temperature": 0.4,
        "max_tokens": 16384,
        "variables": [
            "round-1 session index card (one line per session)",
            "cross-cycle evidence",
            "evaluation cohort",
            "existing skill names",
        ],
        "description": (
            "对 no-skill 会话桶的多轮 agent 循环：基于会话索引卡判断是否存在可复用模式，"
            "可读取既有技能做差异化（read_library_skill）并校验新名（check_name）后创建新技能。"
        ),
    },
    "merge": {
        "id": "merge",
        "label": "冲突合并 Merge",
        "module": "team_skills.evolution.stages.execute",
        "symbol": "_MERGE_SKILL_SYSTEM",
        "resolver": _merge_default,
        "injects_shared_blocks": False,
        "temperature": 0.3,
        "max_tokens": 8192,
        "variables": ["Version A (existing skill)", "Version B (incoming skill)"],
        "description": "同名技能两个进化版本冲突时，按合并原则自检后输出合并版本。",
    },
    "dataset_synthesis": {
        "id": "dataset_synthesis",
        "label": "测试集生成 Dataset Synthesis",
        "module": "team_replay.datasets.synthesis",
        "symbol": "_SYNTHESIZE_SYSTEM",
        "resolver": _dataset_synthesis_default,
        "injects_shared_blocks": False,
        "temperature": 0.3,
        "max_tokens": 16384,
        "variables": [
            "{case_count}",
            "{min_requirements}",
            "{max_requirements}",
            "Session trajectories",
            "team SOP evidence",
            "replay seeds",
        ],
        "description": "与技能进化并行，从相同 Session 与跨周期 Evidence 独立生成带 Checklist 的渐进披露测试集。",
    },
    "replay_checklist": {
        "id": "replay_checklist",
        "label": "真回放 Checklist 裁判",
        "module": "team_replay.engine",
        "symbol": "_CHECKLIST_JUDGE_SYSTEM",
        "resolver": _replay_checklist_default,
        "injects_shared_blocks": False,
        "temperature": 0.0,
        "max_tokens": 8192,
        "variables": ["checklist", "interactions", "tool trajectory", "workspace artifacts"],
        "description": "逐条核验真回放结果是否满足 Checklist；只允许依据可观察证据判定。",
    },
}

def _default_prompt(stage_id: str) -> str:
    entry = _STAGE_CATALOG.get(stage_id)
    if not entry:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    try:
        return str(entry["resolver"]() or "")
    except Exception as exc:  # noqa: BLE001 - resolver must never crash the API
        logger.warning("[PromptStudio] failed to resolve default for %s: %s", stage_id, exc)
        return ""


# ------------------------------------------------------------------ #
# Overrides persistence                                               #
# ------------------------------------------------------------------ #
def _load_overrides(config=None):
    from .prompt_context import settings
    return dict((settings() or {}).get("prompt_overrides", {}))




def _load_stage_settings(config=None):
    from .prompt_context import settings
    return dict((settings() or {}).get("stage_settings", {}))










def stage_call_options(stage_id: str, config: Any = None) -> dict[str, Any]:
    """Return live model and sampling kwargs for one stage.

    Connection fields are omitted when empty so the shared client inherits the
    current tenant's default model endpoint and credential.
    """
    entry = _STAGE_CATALOG.get(stage_id)
    if not entry:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    override = _load_stage_settings(config).get(stage_id, {})
    options: dict[str, Any] = {
        "stage_id": stage_id,
        "temperature": float(
            override.get("temperature", entry["temperature"])
        ),
        "max_tokens": int(override.get("max_tokens", entry["max_tokens"])),
    }
    model = str(override.get("model") or "").strip()
    if model:
        options["model"] = model
    base_url = str(override.get("base_url") or "").strip()
    if base_url:
        options["base_url"] = base_url
    api_key = str(override.get("api_key") or "").strip()
    if api_key:
        options["api_key"] = api_key
    return options






def _raw_effective(stage_id: str, config: Any = None) -> tuple[str, bool]:
    """Return (raw_prompt_text, overridden) BEFORE shared-block expansion."""
    overrides = _load_overrides(config)
    if stage_id in overrides and overrides[stage_id].strip():
        return overrides[stage_id], True
    return _default_prompt(stage_id), False


def _expand_shared_blocks(stage_id: str, text: str) -> str:
    entry = _STAGE_CATALOG.get(stage_id) or {}
    if not entry.get("injects_shared_blocks"):
        return text
    from team_skills.evolution.stages.execute import _inject_shared_blocks

    return _inject_shared_blocks(text)


def effective_prompt(
    stage_id: str,
    fallback: Optional[str] = None,
    config: Any = None,
) -> str:
    """The system prompt the live pipeline should use for ``stage_id``.

    Call sites pass their in-module default as ``fallback``; if no override is
    stored we return that fallback verbatim so behavior is byte-identical to the
    original code path. When an override exists we return it, expanding shared
    blocks for the skill-writing stages exactly like the default path does.
    """
    overrides = _load_overrides(config)
    if stage_id in overrides and overrides[stage_id].strip():
        return _expand_shared_blocks(stage_id, overrides[stage_id])
    if fallback is not None:
        return fallback
    return _expand_shared_blocks(stage_id, _default_prompt(stage_id))


# ------------------------------------------------------------------ #
# Read APIs for the console                                           #
# ------------------------------------------------------------------ #
def _shared_blocks() -> dict[str, str]:
    from team_skills.evolution.stages import execute

    return {
        "__GENERALIZATION_RULES__": execute._GENERALIZATION_RULES,
        "__USER_OVERRIDE_RULE__": execute._USER_OVERRIDE_RULE,
        "__EVIDENCE_ROUTING_RULES__": execute._EVIDENCE_ROUTING_RULES,
        "__OUTPUT_LANGUAGE_RULE__": execute._OUTPUT_LANGUAGE_RULE,
    }


def list_prompts(config: Any = None) -> list[dict[str, Any]]:
    """Summary of every editable stage prompt (no full bodies)."""
    overrides = _load_overrides(config)
    stage_settings = _load_stage_settings(config)
    items: list[dict[str, Any]] = []
    for stage_id, entry in _STAGE_CATALOG.items():
        default = _default_prompt(stage_id)
        overridden = stage_id in overrides and overrides[stage_id].strip() != ""
        raw = overrides[stage_id] if overridden else default
        runtime = stage_call_options(stage_id, config)
        items.append(
            {
                "id": stage_id,
                "label": entry["label"],
                "description": entry["description"],
                "module": entry["module"],
                "symbol": entry["symbol"],
                "temperature": runtime["temperature"],
                "max_tokens": runtime["max_tokens"],
                "model": runtime.get("model", ""),
                "base_url": runtime.get("base_url", ""),
                "api_key_present": bool(stage_settings.get(stage_id, {}).get("secret_ref")),
                "settings_overridden": stage_id in stage_settings,
                "injects_shared_blocks": entry["injects_shared_blocks"],
                "overridden": overridden,
                "char_count": len(raw),
                "default_char_count": len(default),
            }
        )
    return items


def get_prompt(stage_id: str, config: Any = None) -> dict[str, Any]:
    """Full detail for one stage: default, effective raw, expanded, variables."""
    entry = _STAGE_CATALOG.get(stage_id)
    if not entry:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    default = _default_prompt(stage_id)
    raw, overridden = _raw_effective(stage_id, config)
    expanded = _expand_shared_blocks(stage_id, raw)
    runtime = stage_call_options(stage_id, config)
    stage_settings = _load_stage_settings(config)
    payload: dict[str, Any] = {
        "id": stage_id,
        "label": entry["label"],
        "description": entry["description"],
        "module": entry["module"],
        "symbol": entry["symbol"],
        "temperature": runtime["temperature"],
        "max_tokens": runtime["max_tokens"],
        "model": runtime.get("model", ""),
        "base_url": runtime.get("base_url", ""),
        "api_key_present": bool(stage_settings.get(stage_id, {}).get("secret_ref")),
        "default_temperature": float(entry["temperature"]),
        "default_max_tokens": int(entry["max_tokens"]),
        "settings_overridden": stage_id in stage_settings,
        "injects_shared_blocks": entry["injects_shared_blocks"],
        "variables": list(entry["variables"]),
        "overridden": overridden,
        "default_prompt": default,
        "effective_prompt": raw,
        "expanded_prompt": expanded,
    }
    if entry["injects_shared_blocks"]:
        payload["shared_blocks"] = _shared_blocks()
    return payload


def pipeline_graph(config: Any = None) -> dict[str, Any]:
    """Chain graph annotated with which nodes have editable prompts + override flags."""
    overrides = _load_overrides(config)
    settings = _load_stage_settings(config)
    nodes = []
    for node in PIPELINE_STAGES["nodes"]:
        item = dict(node)
        prompt_id = item.get("prompt_id")
        if prompt_id and prompt_id in _STAGE_CATALOG:
            item["overridden"] = prompt_id in overrides and overrides[prompt_id].strip() != ""
            item["settings_overridden"] = prompt_id in settings
        nodes.append(item)
    return {"nodes": nodes, "edges": list(PIPELINE_STAGES["edges"])}


# ------------------------------------------------------------------ #
# Test runner                                                         #
# ------------------------------------------------------------------ #
def build_stage_messages(stage_id: str, session: dict[str, Any], *, system_prompt: str) -> list[dict[str, str]]:
    """Build the exact [system, user] messages a stage would send for a session.

    ``system_prompt`` is used verbatim (already the edited/effective text). The
    user message is reconstructed with the SAME helpers the live pipeline uses,
    so the test reflects reality. For skill-writing stages we synthesize a
    minimal-but-real evidence block from the given session.
    """
    if stage_id not in _STAGE_CATALOG:
        raise KeyError(f"unknown prompt stage: {stage_id}")

    if stage_id == "analyze_session":
        from team_skills.evolution.stages.analyze import build_analysis_payload
        from team_skills.evolution.stages.summarize import (
            _extract_session_metadata,
            build_session_trajectory,
        )

        # Reproduce the live analyze payload: metadata + trajectory then the
        # merged full-trajectory payload the stage sends.
        _extract_session_metadata(session)
        if not session.get("_trajectory"):
            session["_trajectory"] = build_session_trajectory(session)
        payload = build_analysis_payload(session)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    if stage_id == "analyze_trace_badcase":
        from team_skills.evolution.stages.summarize import _extract_session_metadata
        from team_skills.evolution.stages.trace_analyze import build_trace_payload

        # Same per-trace payload the live localization call sends.
        _extract_session_metadata(session)
        payload = build_trace_payload(session)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    if stage_id in {"evolve_skill", "create_skill"}:
        from team_skills.evolution.agent.cards import build_round1_user_msg
        from team_skills.evolution.stages.summarize import _extract_session_metadata, build_session_trajectory

        _extract_session_metadata(session)
        if not session.get("_trajectory"):
            session["_trajectory"] = build_session_trajectory(session)
        session.setdefault("_summary", session.get("_summary") or "")
        if stage_id == "evolve_skill":
            skill_name = str(session.get("_probe_skill_name") or "example-skill")
            system_prompt = system_prompt.replace("{skill_name}", skill_name)
            user_msg = build_round1_user_msg(
                stage="evolve_skill",
                skill_name=skill_name,
                sessions=[session],
                current_skill=None,
                existing_skill_names=[skill_name],
                evolution_context=None,
            )
        else:
            user_msg = build_round1_user_msg(
                stage="create_skill",
                skill_name="",
                sessions=[session],
                current_skill=None,
                existing_skill_names=[],
                evolution_context=None,
            )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

    if stage_id == "merge":
        from team_skills.evolution.agent.cards import build_merge_card

        existing = {"name": "example-skill", "description": "existing", "content": "Existing body.", "_version": 1}
        incoming = {"name": "example-skill", "description": "incoming", "content": "Incoming body."}
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_merge_card(existing, incoming)},
        ]

    if stage_id == "dataset_synthesis":
        from team_replay.datasets.synthesis import render_synthesis_prompt
        from team_skills.evolution.stages.summarize import (
            _extract_session_metadata,
            build_session_trajectory,
        )

        _extract_session_metadata(session)
        if not session.get("_trajectory"):
            session["_trajectory"] = build_session_trajectory(session)
        payload = {
            "skill_name": str(
                session.get("_probe_skill_name") or "example-skill"
            ),
            "team_sop_evidence": {"context": {}, "claims": []},
            "sessions": [
                {
                    "session_id": str(session.get("session_id") or ""),
                    "initial_query": str(
                        ((session.get("turns") or [{}])[0] or {}).get(
                            "prompt_text"
                        )
                        or ""
                    ),
                    "summary": str(session.get("_summary") or ""),
                    "trajectory": str(session.get("_trajectory") or ""),
                }
            ],
            "replay_seeds": [],
        }
        resolved = render_synthesis_prompt(
            system_prompt,
            case_count=2,
            min_requirements=6,
            max_requirements=8,
            min_source_sessions=2,
        )
        return [
            {"role": "system", "content": resolved},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    if stage_id == "replay_checklist":
        checklist = [
            {"id": "C01", "text": "完成用户要求并给出可验证结果", "kind": "output"}
        ]
        payload = {
            "checklist": checklist,
            "interactions": [
                {
                    "interaction_num": 1,
                    "prompt": str(
                        ((session.get("turns") or [{}])[0] or {}).get(
                            "prompt_text"
                        )
                        or ""
                    ),
                    "response": str(
                        ((session.get("turns") or [{}])[0] or {}).get(
                            "response_text"
                        )
                        or ""
                    ),
                }
            ],
            "tool_trajectory": "(test runner reconstructs this in live replay)",
            "workspace_artifacts": [],
        }
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    raise KeyError(f"unsupported stage for test: {stage_id}")
