"""Prompt Studio: make the skill-evolution pipeline transparent and editable.

The skill-optimization pipeline runs a fixed chain of stages; five of them call
an LLM with a system prompt that fully determines behavior. Historically those
prompts were module-level constants — a black box from the console. This module
turns them into first-class, inspectable, editable, and testable objects:

    ingest → summarize → judge → group-by-skill → (evolve_skill | create_skill)
           → true-replay validate → publish

Responsibilities:
  * ``PIPELINE_STAGES`` — the chain graph for visualization (nodes + edges,
    which nodes call the LLM, and the prompt id each LLM node uses).
  * ``list_prompts`` / ``get_prompt`` — resolve a stage's DEFAULT system prompt
    from the owning stage module, plus any persisted override, plus the shared
    blocks that get injected and the ``{...}`` variables it expects.
  * ``set_override`` / ``reset_override`` — file-backed prompt overrides at
    ``~/.teamEvolver/prompt_overrides.json``.
  * ``effective_prompt(stage_id, default)`` — the single accessor the live call
    sites consult so an edited prompt actually drives the pipeline.
  * ``run_stage_test`` — build the REAL user message a stage would send for a
    given session and call the LLM, returning system + user + output so the
    operator can see inputs and outputs, not a black box.

Overrides are intentionally the raw system-prompt text. For the two skill-writing
stages the raw template still contains ``__GENERALIZATION_RULES__`` /
``__USER_OVERRIDE_RULE__`` / ``__EVIDENCE_ROUTING_RULES__`` sentinels; the shared
blocks are injected at resolve time exactly as the live pipeline does, so what
you edit is what runs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_OVERRIDES_ENV = "TEAMEVOLVER_PROMPT_OVERRIDES_PATH"


def _overrides_path() -> Path:
    override = str(os.environ.get(_OVERRIDES_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".teamEvolver" / "prompt_overrides.json"


# ------------------------------------------------------------------ #
# Pipeline chain (for visualization)                                  #
# ------------------------------------------------------------------ #
# Each node: id, label, kind ("io"|"llm"|"logic"|"gate"), optional prompt_id,
# and a short description. Edges are (from, to). This mirrors the real flow in
# EvolveServer.run_once → summarize → judge → group → evolve/create → validate.
PIPELINE_STAGES: dict[str, Any] = {
    "nodes": [
        {
            "id": "ingest",
            "label": "会话入队 Ingest",
            "kind": "io",
            "description": "会话经 /ingest_session 或 Langfuse 拉取入队，价值分类后进入进化批次。",
        },
        {
            "id": "summarize",
            "label": "会话总结 Summarize",
            "kind": "llm",
            "prompt_id": "summarize",
            "description": "为每个会话构建无损轨迹，并用 LLM 生成轨迹感知的分析摘要。",
        },
        {
            "id": "judge",
            "label": "会话评分 Judge",
            "kind": "llm",
            "prompt_id": "judge",
            "description": "对缺少可靠分数的会话补打分（任务完成/质量/效率/工具使用）。",
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
            "description": "对已有技能：基于会话证据决定 improve/optimize_description/create/skip。",
        },
        {
            "id": "create_skill",
            "label": "新建技能 Create",
            "kind": "llm",
            "prompt_id": "create_skill",
            "description": "对 no-skill 会话桶：判断是否存在可复用模式并生成新技能。",
        },
        {
            "id": "merge",
            "label": "冲突合并 Merge",
            "kind": "llm",
            "prompt_id": "merge",
            "description": "同名技能的两个进化版本冲突时，合并为一个更优版本。",
        },
        {
            "id": "dataset_synthesis",
            "label": "测试集生成 Dataset Synthesis",
            "kind": "llm",
            "prompt_id": "dataset_synthesis",
            "description": "使用同一批 Session、跨周期 SOP evidence 和候选 Skill，同步生成渐进披露的 test datasets。",
        },
        {
            "id": "validate",
            "label": "真回放校验 Validate",
            "kind": "gate",
            "description": "基于初始 Query 逐轮披露未满足 Checklist；完成后对比基线的轮次/工具调用/Token。",
        },
        {
            "id": "publish",
            "label": "发布 Publish",
            "kind": "io",
            "description": "通过校验的候选写入技能库并同步云端；不通过则进入人工复核。",
        },
    ],
    "edges": [
        {"from": "ingest", "to": "summarize"},
        {"from": "summarize", "to": "judge"},
        {"from": "judge", "to": "group"},
        {"from": "group", "to": "evolve_skill"},
        {"from": "group", "to": "create_skill"},
        {"from": "evolve_skill", "to": "merge"},
        {"from": "create_skill", "to": "merge"},
        {"from": "evolve_skill", "to": "dataset_synthesis"},
        {"from": "create_skill", "to": "dataset_synthesis"},
        {"from": "merge", "to": "dataset_synthesis"},
        {"from": "dataset_synthesis", "to": "validate"},
        {"from": "validate", "to": "publish"},
    ],
}


# ------------------------------------------------------------------ #
# Stage catalog                                                       #
# ------------------------------------------------------------------ #
# Each entry declares how to resolve its DEFAULT system prompt from the owning
# module. ``injects_shared_blocks`` marks the two skill-writing prompts whose
# raw template still carries the sentinels expanded by execute._inject_shared_blocks.


def _summarize_default() -> str:
    from .stages import summarize

    return summarize._SUMMARIZE_SESSION_SYSTEM


def _judge_default() -> str:
    from .stages import judge

    return judge._JUDGE_SYSTEM


def _evolve_default_raw() -> str:
    # Raw template BEFORE shared-block injection (kept as-is in the module after
    # injection is applied at import). We reconstruct the editable raw form so
    # operators edit the template, not the fully-expanded text.
    from .stages import execute

    return execute._EVOLVE_FROM_SESSIONS_SYSTEM


def _create_default_raw() -> str:
    from .stages import execute

    return execute._CREATE_FROM_SESSIONS_SYSTEM


def _merge_default() -> str:
    from .stages import execute

    return execute._MERGE_SKILL_SYSTEM


def _dataset_synthesis_default() -> str:
    from .. import dataset_synthesizer

    return dataset_synthesizer._SYNTHESIZE_SYSTEM


_STAGE_CATALOG: dict[str, dict[str, Any]] = {
    "summarize": {
        "id": "summarize",
        "label": "会话总结 Summarize",
        "module": "teamEvolver.evolve.stages.summarize",
        "symbol": "_SUMMARIZE_SESSION_SYSTEM",
        "resolver": _summarize_default,
        "injects_shared_blocks": False,
        "temperature": 0.2,
        "variables": ["session (JSON payload: interactions, tool calls, scores)"],
        "description": "对单个会话生成轨迹感知分析摘要，供后续评分与进化使用。",
    },
    "judge": {
        "id": "judge",
        "label": "会话评分 Judge",
        "module": "teamEvolver.evolve.stages.judge",
        "symbol": "_JUDGE_SYSTEM",
        "resolver": _judge_default,
        "injects_shared_blocks": False,
        "temperature": 0.1,
        "variables": ["session payload: trajectory, summary, artifacts, prior scores"],
        "description": "对缺少可靠分数的会话补打分，输出 JSON 维度分。",
    },
    "evolve_skill": {
        "id": "evolve_skill",
        "label": "改进技能 Evolve",
        "module": "teamEvolver.evolve.stages.execute",
        "symbol": "_EVOLVE_FROM_SESSIONS_SYSTEM",
        "resolver": _evolve_default_raw,
        "injects_shared_blocks": True,
        "temperature": 0.4,
        "variables": [
            "{skill_name}",
            "current skill block",
            "cross-cycle evidence",
            "evaluation cohort",
            "session evidence",
            "existing skill names",
        ],
        "description": "对已有技能：基于会话证据决定 improve / optimize_description / create / skip。",
    },
    "create_skill": {
        "id": "create_skill",
        "label": "新建技能 Create",
        "module": "teamEvolver.evolve.stages.execute",
        "symbol": "_CREATE_FROM_SESSIONS_SYSTEM",
        "resolver": _create_default_raw,
        "injects_shared_blocks": True,
        "temperature": 0.4,
        "variables": ["cross-cycle evidence", "evaluation cohort", "session evidence", "existing skill names"],
        "description": "对 no-skill 会话桶：判断是否存在可复用模式并生成新技能。",
    },
    "merge": {
        "id": "merge",
        "label": "冲突合并 Merge",
        "module": "teamEvolver.evolve.stages.execute",
        "symbol": "_MERGE_SKILL_SYSTEM",
        "resolver": _merge_default,
        "injects_shared_blocks": False,
        "temperature": 0.3,
        "variables": ["Version A (existing skill)", "Version B (incoming skill)"],
        "description": "同名技能两个进化版本冲突时合并为一个更优版本。",
    },
    "dataset_synthesis": {
        "id": "dataset_synthesis",
        "label": "测试集生成 Dataset Synthesis",
        "module": "teamEvolver.dataset_synthesizer",
        "symbol": "_SYNTHESIZE_SYSTEM",
        "resolver": _dataset_synthesis_default,
        "injects_shared_blocks": False,
        "temperature": 0.3,
        "variables": [
            "{case_count}",
            "{min_requirements}",
            "{max_requirements}",
            "candidate Skill",
            "Session trajectories",
            "team SOP evidence",
            "replay seeds",
        ],
        "description": "从 Session 与跨周期 SOP evidence 同步生成带 Checklist 的渐进式 test datasets。",
    },
}

STAGE_IDS = tuple(_STAGE_CATALOG.keys())


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
def _load_overrides() -> dict[str, str]:
    path = _overrides_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and str(k) in _STAGE_CATALOG}


def _save_overrides(overrides: dict[str, str]) -> None:
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(overrides, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def set_override(stage_id: str, prompt: str) -> None:
    """Persist a system-prompt override for a stage."""
    if stage_id not in _STAGE_CATALOG:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    text = str(prompt or "")
    if not text.strip():
        raise ValueError("prompt override must not be empty")
    overrides = _load_overrides()
    overrides[stage_id] = text
    _save_overrides(overrides)


def reset_override(stage_id: str) -> None:
    """Remove any override so the stage reverts to its module default."""
    if stage_id not in _STAGE_CATALOG:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    overrides = _load_overrides()
    if stage_id in overrides:
        overrides.pop(stage_id, None)
        _save_overrides(overrides)


def _raw_effective(stage_id: str) -> tuple[str, bool]:
    """Return (raw_prompt_text, overridden) BEFORE shared-block expansion."""
    overrides = _load_overrides()
    if stage_id in overrides and overrides[stage_id].strip():
        return overrides[stage_id], True
    return _default_prompt(stage_id), False


def _expand_shared_blocks(stage_id: str, text: str) -> str:
    entry = _STAGE_CATALOG.get(stage_id) or {}
    if not entry.get("injects_shared_blocks"):
        return text
    from .stages.execute import _inject_shared_blocks

    return _inject_shared_blocks(text)


def effective_prompt(stage_id: str, fallback: Optional[str] = None) -> str:
    """The system prompt the live pipeline should use for ``stage_id``.

    Call sites pass their in-module default as ``fallback``; if no override is
    stored we return that fallback verbatim so behavior is byte-identical to the
    original code path. When an override exists we return it, expanding shared
    blocks for the skill-writing stages exactly like the default path does.
    """
    overrides = _load_overrides()
    if stage_id in overrides and overrides[stage_id].strip():
        return _expand_shared_blocks(stage_id, overrides[stage_id])
    if fallback is not None:
        return fallback
    return _expand_shared_blocks(stage_id, _default_prompt(stage_id))


# ------------------------------------------------------------------ #
# Read APIs for the console                                           #
# ------------------------------------------------------------------ #
def _shared_blocks() -> dict[str, str]:
    from .stages import execute

    return {
        "__GENERALIZATION_RULES__": execute._GENERALIZATION_RULES,
        "__USER_OVERRIDE_RULE__": execute._USER_OVERRIDE_RULE,
        "__EVIDENCE_ROUTING_RULES__": execute._EVIDENCE_ROUTING_RULES,
    }


def list_prompts() -> list[dict[str, Any]]:
    """Summary of every editable stage prompt (no full bodies)."""
    overrides = _load_overrides()
    items: list[dict[str, Any]] = []
    for stage_id, entry in _STAGE_CATALOG.items():
        default = _default_prompt(stage_id)
        overridden = stage_id in overrides and overrides[stage_id].strip() != ""
        raw = overrides[stage_id] if overridden else default
        items.append(
            {
                "id": stage_id,
                "label": entry["label"],
                "description": entry["description"],
                "module": entry["module"],
                "symbol": entry["symbol"],
                "temperature": entry["temperature"],
                "injects_shared_blocks": entry["injects_shared_blocks"],
                "overridden": overridden,
                "char_count": len(raw),
                "default_char_count": len(default),
            }
        )
    return items


def get_prompt(stage_id: str) -> dict[str, Any]:
    """Full detail for one stage: default, effective raw, expanded, variables."""
    entry = _STAGE_CATALOG.get(stage_id)
    if not entry:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    default = _default_prompt(stage_id)
    raw, overridden = _raw_effective(stage_id)
    expanded = _expand_shared_blocks(stage_id, raw)
    payload: dict[str, Any] = {
        "id": stage_id,
        "label": entry["label"],
        "description": entry["description"],
        "module": entry["module"],
        "symbol": entry["symbol"],
        "temperature": entry["temperature"],
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


def pipeline_graph() -> dict[str, Any]:
    """Chain graph annotated with which nodes have editable prompts + override flags."""
    overrides = _load_overrides()
    nodes = []
    for node in PIPELINE_STAGES["nodes"]:
        item = dict(node)
        prompt_id = item.get("prompt_id")
        if prompt_id and prompt_id in _STAGE_CATALOG:
            item["overridden"] = prompt_id in overrides and overrides[prompt_id].strip() != ""
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

    if stage_id == "summarize":
        from .stages.summarize import _build_session_payload

        payload = _build_session_payload(session)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    if stage_id == "judge":
        from .stages.summarize import _extract_session_metadata, build_session_trajectory
        from .stages.judge import _build_judge_payload

        # Ensure the trajectory/summary/metadata the judge payload reads exist.
        _extract_session_metadata(session)
        if not session.get("_trajectory"):
            session["_trajectory"] = build_session_trajectory(session)
        session.setdefault("_summary", session.get("_summary") or "")
        payload = _build_judge_payload(session)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    if stage_id in {"evolve_skill", "create_skill"}:
        from .stages.summarize import _extract_session_metadata, build_session_trajectory
        from .stages import execute as ex

        _extract_session_metadata(session)
        if not session.get("_trajectory"):
            session["_trajectory"] = build_session_trajectory(session)
        session.setdefault("_summary", session.get("_summary") or "")
        evidence = ex._build_session_evidence([session])
        if stage_id == "evolve_skill":
            skill_name = str(session.get("_probe_skill_name") or "example-skill")
            system_prompt = system_prompt.replace("{skill_name}", skill_name)
            user_msg = (
                f"## Session evidence (1 sessions)\n\n{evidence}\n\n"
                f"## Existing skill names in the library\n\n{skill_name}\n"
            )
        else:
            user_msg = (
                f"## Session evidence (1 sessions)\n\n{evidence}\n\n"
                f"## Existing skill names in the library\n\n(none)\n"
            )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

    if stage_id == "merge":
        existing = {"name": "example-skill", "description": "existing", "content": "Existing body.", "_version": 1}
        incoming = {"name": "example-skill", "description": "incoming", "content": "Incoming body."}
        user_msg = (
            f"## Version A (currently in shared storage, v1)\n\n"
            f"Name: {existing['name']}\nContent:\n```\n{existing['content']}\n```\n\n---\n\n"
            f"## Version B (newly evolved)\n\nName: {incoming['name']}\nContent:\n```\n{incoming['content']}\n```"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

    if stage_id == "dataset_synthesis":
        from ..dataset_synthesizer import render_synthesis_prompt
        from .stages.summarize import (
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
            "candidate_skill": {
                "description": "Example candidate",
                "content": "Example evolved procedure.",
            },
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
            min_requirements=12,
            max_requirements=24,
        )
        return [
            {"role": "system", "content": resolved},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    raise KeyError(f"unsupported stage for test: {stage_id}")


async def run_stage_test(
    stage_id: str,
    session: dict[str, Any],
    *,
    system_prompt: Optional[str],
    llm_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Run one stage against a session and return system + user + model output.

    ``system_prompt`` overrides the effective prompt for this test only (so the
    operator can try edits before saving). ``llm_factory`` returns an
    ``AsyncLLMClient`` — injected so this module stays free of config coupling.
    """
    if stage_id not in _STAGE_CATALOG:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    entry = _STAGE_CATALOG[stage_id]

    # Resolve the system prompt to use: explicit test text, else effective.
    if system_prompt and system_prompt.strip():
        resolved_system = _expand_shared_blocks(stage_id, system_prompt)
    else:
        resolved_system = effective_prompt(stage_id)

    messages = build_stage_messages(stage_id, session, system_prompt=resolved_system)
    llm = llm_factory()
    output = await llm.chat(
        messages,
        max_tokens=8192,
        temperature=float(entry.get("temperature") or 0.3),
    )
    return {
        "stage_id": stage_id,
        "system_prompt": messages[0]["content"],
        "user_message": messages[1]["content"],
        "output": output,
    }
