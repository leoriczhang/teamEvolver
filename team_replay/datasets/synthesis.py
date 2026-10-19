"""Synthesize progressive test datasets from sessions and team SOP evidence.

This module is intentionally standalone inside teamEvolver. It consumes the
same accumulated session/evidence context used to edit a Skill and emits test
datasets that become the candidate's True Replay contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from team_replay.datasets.integrity import (
    dataset_material_integrity,
)
from team_replay.datasets.materials import collect_session_materials
from team_replay.datasets.schema import (
    normalize_case,
    normalize_checks,
)

_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*")
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)

_SYNTHESIZE_SYSTEM = """\
你为 Skill 演化生成留存测试（held-out test）数据集。

测试集生成与 Skill 进化基于同一批积累的会话和团队 SOP 证据独立进行。请只从这些证据\
构建 {case_count} 个贴近真实的测试用例，用于检验可复用规程，同时初始提问不得泄露完整需求。\
不要等待或推测本轮候选 Skill，不得把候选内容或编辑结果当作需求证据。
每个用例都必须归纳至少 {min_source_sessions} 条不同 Session 共同出现的典型任务模式，\
不能直接复制某一条 Session 的完整请求，也不能把所有 Session 逐条转换为用例。

对每个测试用例：
1. query：一个自包含的初始用户请求。绝不能枚举或透露 checklist。
2. checks：{min_requirements}-{max_requirements} 条扁平、可独立验证的检查项。\
输出/内容要求使用 kind=output；资料读取、工具操作、计算、校验和产物写入使用\
kind=trajectory。每条检查都必须有会话或团队 SOP 证据支撑。
4. source_session_ids：至少 {min_source_sessions} 个支撑该共性模式的不同会话 ID。
5. evidence_window："recent" 或 "historical"。
6. name：简洁、人类可读的测试名称。

每个生成的用例都必须能凭数据集本身独立运行。如果源任务使用了文件或目录，但其字节内容\
未包含在所提供的证据中，不要引用这些路径。改为在 query 中以 `材料：` 小节内联一段\
紧凑且贴近真实的夹具（fixture）。绝不要虚构回放环境无法提供的文件名、压缩包、\
输入目录或材料。

回放协议只在第 1 轮交互中展示 query。之后每轮交互由独立的 checklist 评审者指出未满足的\
要求，且只披露下一批未满足的要求。因此每条要求必须具体到可以依据回复、工具轨迹和产物\
来判定。

不要添加证据不支持的通用格式规则。不要包含训练/测试打分、权重或汇总分。

只返回 JSON：
{
  "test_datasets": [
    {
      "name": "...",
      "query": "...",
      "checks": [
        {"id": "R01", "kind": "output", "text": "..."},
        {"id": "T01", "kind": "trajectory", "text": "..."}
      ],
      "source_session_ids": ["..."],
      "evidence_window": "recent"
    }
  ]
}
"""


# Prompt/stage-option provider is injected by the host so this module carries
# no static dependency on the Skill-evolution package. Defaults fall back to the
# built-in constant and stage defaults, preserving standalone behavior.
_PROMPT_PROVIDER: Any = None
_OPTIONS_PROVIDER: Any = None


def _effective_synthesize_system() -> str:
    if _PROMPT_PROVIDER is not None:
        try:
            return _PROMPT_PROVIDER("dataset_synthesis", _SYNTHESIZE_SYSTEM)
        except Exception:  # noqa: BLE001 - retain built-in default on any failure
            return _SYNTHESIZE_SYSTEM
    return _SYNTHESIZE_SYSTEM


def _synthesis_call_options() -> dict[str, Any]:
    if _OPTIONS_PROVIDER is not None:
        try:
            return _OPTIONS_PROVIDER("dataset_synthesis")
        except Exception:  # noqa: BLE001 - retain stable stage defaults
            return {"max_tokens": 16_384, "temperature": 0.3}
    return {"max_tokens": 16_384, "temperature": 0.3}


def render_synthesis_prompt(
    template: str,
    *,
    case_count: int,
    min_requirements: int,
    max_requirements: int,
    min_source_sessions: int = 1,
) -> str:
    return (
        str(template)
        .replace("{case_count}", str(case_count))
        .replace("{min_requirements}", str(min_requirements))
        .replace("{max_requirements}", str(max_requirements))
        .replace("{min_source_sessions}", str(min_source_sessions))
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def flatten_requirements(raw: Any) -> list[str]:
    """Normalize list/numbered Markdown into unique flat requirement strings."""
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("requirement") or ""
        for line in str(value or "").splitlines():
            text = _LIST_PREFIX_RE.sub("", line).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def checklist_items(
    requirements: Iterable[Any],
    trajectory_requirements: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """Build stable checklist ids shared by synthesizer, replay, and UI."""
    output = flatten_requirements(list(requirements))
    trajectory = flatten_requirements(list(trajectory_requirements))
    items = [
        {
            "id": f"R{index:02d}",
            "text": text,
            "kind": "output",
        }
        for index, text in enumerate(output, start=1)
    ]
    items.extend(
        {
            "id": f"T{index:02d}",
            "text": text,
            "kind": "trajectory",
        }
        for index, text in enumerate(trajectory, start=1)
    )
    return items


def _first_prompt(session: Mapping[str, Any]) -> str:
    for turn in session.get("turns") or []:
        if isinstance(turn, Mapping):
            text = str(
                turn.get("prompt_text") or turn.get("instruction") or ""
            ).strip()
            if text:
                return text
    return ""


def _session_payload(session: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(session.get("session_id") or ""),
        "initial_query": _first_prompt(session)[:12_000],
        "summary": str(session.get("_summary") or "")[:16_000],
        "trajectory": str(session.get("_trajectory") or "")[:32_000],
        "evidence_window": str(session.get("_evidence_window") or "recent"),
        "has_tool_errors": bool(session.get("_has_tool_errors")),
    }


def _seed_cases(
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for window in ("recent", "historical"):
        for raw in (replay_windows or {}).get(window) or []:
            if not isinstance(raw, Mapping):
                continue
            seeds.append(
                {
                    "session_id": str(raw.get("session_id") or ""),
                    "turn_num": int(raw.get("turn_num") or 0),
                    "instruction": str(raw.get("instruction") or "")[:12_000],
                    "reference_response": str(
                        raw.get("reference_response") or ""
                    )[:16_000],
                    "evidence_window": window,
                }
            )
    return seeds


def _team_evidence_claims(candidate_skill: Mapping[str, Any]) -> list[str]:
    classification = (
        candidate_skill.get("_evidence_classification")
        if isinstance(candidate_skill.get("_evidence_classification"), Mapping)
        else candidate_skill.get("evidence_classification")
        if isinstance(candidate_skill.get("evidence_classification"), Mapping)
        else {}
    )
    claims: list[str] = []
    for raw in classification.get("team_skill") or []:
        value = (
            raw.get("claim") or raw.get("text") or raw.get("requirement")
            if isinstance(raw, Mapping)
            else raw
        )
        text = str(value or "").strip()
        if text and text not in claims:
            claims.append(text)
    return claims


def _parse_json_object(raw: str) -> Optional[dict[str, Any]]:
    clean = _FENCE_RE.sub("", str(raw or "").strip())
    candidates = [clean]
    start, end = clean.find("{"), clean.rfind("}")
    if start >= 0 and end > start:
        candidates.append(clean[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _dataset_id(
    *,
    skill_name: str,
    query: str,
    source_session_ids: list[str],
) -> str:
    digest = hashlib.sha256(
        "\0".join([skill_name, query, *source_session_ids]).encode("utf-8")
    ).hexdigest()
    return f"synth-{digest[:20]}"


def _normalize_dataset(
    raw: Mapping[str, Any],
    *,
    skill_name: str,
    min_requirements: int,
    max_requirements: int,
    batch_size: int,
    default_window: str,
    synthesis_mode: str,
    materials: Optional[list[dict[str, Any]]] = None,
    min_source_sessions: int = 1,
) -> Optional[dict[str, Any]]:
    query = str(raw.get("query") or "").strip()
    if not query:
        return None
    try:
        checks = normalize_checks(raw.get("checks"))
    except ValueError:
        return None
    checks = checks[:max_requirements]
    if len(checks) < min_requirements:
        return None
    source_ids = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw.get("source_session_ids") or []
            if str(item or "").strip()
        )
    )
    if len(source_ids) < max(1, int(min_source_sessions)):
        return None
    window = str(raw.get("evidence_window") or default_window).strip().lower()
    if window not in {"recent", "historical"}:
        window = default_window
    dataset_id = _dataset_id(
        skill_name=skill_name,
        query=query,
        source_session_ids=source_ids,
    )
    dataset_materials = [
        dict(item)
        for item in materials or []
        if isinstance(item, Mapping)
        and item.get("path")
        and (
            not source_ids
            or not item.get("source_session_id")
            or str(item.get("source_session_id")) in source_ids
        )
    ]
    dataset = normalize_case({
        "case_id": dataset_id,
        "query": query,
        "skill_ids": [skill_name],
        "checks": checks,
        "materials": dataset_materials,
        "provenance": {
            "session_ids": source_ids,
            "evidence_window": window,
            "synthesis_mode": synthesis_mode,
        },
        "replay": {
            "progressive_disclosure": {
                "enabled": True,
                "initial_visibility": "query_only",
                "batch_size": max(1, int(batch_size)),
                "stop_when": "all_checklist_items_satisfied",
            },
        },
        "metadata": {
            "split": "test",
            "minimum_requirement_target": min_requirements,
            "name": str(raw.get("name") or "").strip() or query.splitlines()[0][:80],
        },
    })
    if not dataset_material_integrity(
        dataset,
        available_paths=[
            str(item.get("path") or "")
            for item in dataset_materials
            if item.get("content_b64")
        ],
    )["complete"]:
        return None
    return dataset


def _requirements_from_session(session: Mapping[str, Any]) -> list[str]:
    requirements: list[str] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        prompt = str(turn.get("prompt_text") or turn.get("instruction") or "")
        match = re.search(
            r"(?ims)^###\s*要求\s*$\s*(.*?)(?=^###\s+|\Z)",
            prompt,
        )
        if match:
            requirements.extend(flatten_requirements(match.group(1)))
    return requirements


def _fallback_datasets(
    *,
    skill_name: str,
    sessions: list[dict[str, Any]],
    candidate_skill: Mapping[str, Any],
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]],
    min_requirements: int,
    max_requirements: int,
    batch_size: int,
    case_count: int,
    materials: Optional[list[dict[str, Any]]] = None,
    min_source_sessions: int = 1,
) -> list[dict[str, Any]]:
    grounded_requirements: list[str] = []
    for claim in _team_evidence_claims(candidate_skill):
        grounded_requirements.extend(flatten_requirements(claim))
    for session in sessions:
        grounded_requirements.extend(_requirements_from_session(session))
    grounded_requirements = list(dict.fromkeys(grounded_requirements))[
        :max_requirements
    ]

    seeds = _seed_cases(replay_windows)
    if not seeds:
        seeds = [
            {
                "instruction": _first_prompt(session),
                "session_id": str(session.get("session_id") or ""),
                "evidence_window": str(
                    session.get("_evidence_window") or "recent"
                ),
            }
            for session in sessions
            if _first_prompt(session)
        ]
    all_source_ids = list(dict.fromkeys(
        str(session.get("session_id") or "") for session in sessions
        if str(session.get("session_id") or "").strip()
    ))
    datasets: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds[: max(1, case_count)]):
        dataset = _normalize_dataset(
            {
                "name": f"{skill_name} test {index + 1}",
                "query": seed.get("instruction"),
                "checks": checklist_items(grounded_requirements),
                "source_session_ids": (
                    all_source_ids[:max(1, min_source_sessions)]
                    if min_source_sessions > 1 else [seed.get("session_id")]
                ),
                "evidence_window": seed.get("evidence_window"),
            },
            skill_name=skill_name,
            min_requirements=min_requirements,
            max_requirements=max_requirements,
            batch_size=batch_size,
            default_window="recent" if index == 0 else "historical",
            synthesis_mode="grounded_fallback",
            materials=materials,
            min_source_sessions=min_source_sessions,
        )
        if dataset:
            datasets.append(dataset)
    return datasets


async def synthesize_evolution_datasets(
    llm: Any,
    *,
    skill_name: str,
    sessions: list[dict[str, Any]],
    candidate_skill: Optional[Mapping[str, Any]] = None,
    evidence_context: Optional[Mapping[str, Any]] = None,
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]] = None,
    case_count: int = 2,
    min_requirements: int = 12,
    max_requirements: int = 24,
    batch_size: int = 4,
    min_source_sessions: int = 1,
) -> list[dict[str, Any]]:
    """Create test datasets from the exact evidence used for Skill evolution."""
    case_count = max(1, min(6, int(case_count or 2)))
    min_requirements = max(1, int(min_requirements or 1))
    max_requirements = max(min_requirements, int(max_requirements or 24))
    batch_size = max(1, int(batch_size or 1))
    min_source_sessions = max(1, int(min_source_sessions or 1))
    candidate_skill = candidate_skill or {}
    compact_sessions = [_session_payload(session) for session in sessions[:50]]
    source_materials = collect_session_materials(sessions)
    payload = {
        "skill_name": skill_name,
        "team_sop_evidence": {
            "context": dict(evidence_context or {}),
            "claims": _team_evidence_claims(candidate_skill),
        },
        "sessions": compact_sessions,
        "replay_seeds": _seed_cases(replay_windows),
    }
    if candidate_skill:
        # Standalone Dataset synthesis can supply an already frozen Skill.
        # Evolution passes no proposal, so its held-out Cases remain independent.
        payload["candidate_skill"] = {
            "description": str(candidate_skill.get("description") or ""),
            "content": str(candidate_skill.get("content") or "")[:32_000],
            "edit_summary": candidate_skill.get("edit_summary") or {},
        }
    parsed: Optional[dict[str, Any]] = None
    try:
        raw = await llm.chat(
            [
                {
                    "role": "system",
                    "content": render_synthesis_prompt(
                        _effective_synthesize_system(),
                        case_count=case_count,
                        min_requirements=min_requirements,
                        max_requirements=max_requirements,
                        min_source_sessions=min_source_sessions,
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            **_synthesis_call_options(),
            trace_name=f"team-skill-evolver:dataset_synthesis:{skill_name}",
            trace_tags=[
                "team-skill-evolver",
                "dataset-synthesis",
                f"skill:{skill_name}",
            ],
            trace_metadata={
                "skill_name": skill_name,
                "session_count": len(compact_sessions),
                "case_count": case_count,
            },
        )
        parsed = _parse_json_object(raw)
    except Exception:
        parsed = None

    datasets: list[dict[str, Any]] = []
    raw_datasets = (
        parsed.get("test_datasets")
        if isinstance(parsed, Mapping)
        and isinstance(parsed.get("test_datasets"), list)
        else []
    )
    for index, raw_dataset in enumerate(raw_datasets[:case_count]):
        if not isinstance(raw_dataset, Mapping):
            continue
        dataset = _normalize_dataset(
            raw_dataset,
            skill_name=skill_name,
            min_requirements=min_requirements,
            max_requirements=max_requirements,
            batch_size=batch_size,
            default_window="recent" if index == 0 else "historical",
            synthesis_mode="model",
            materials=source_materials,
            min_source_sessions=min_source_sessions,
        )
        if dataset:
            datasets.append(dataset)
    if datasets:
        return datasets
    return _fallback_datasets(
        skill_name=skill_name,
        sessions=sessions,
        candidate_skill=candidate_skill,
        replay_windows=replay_windows,
        min_requirements=min_requirements,
        max_requirements=max_requirements,
        batch_size=batch_size,
        case_count=case_count,
        materials=source_materials,
        min_source_sessions=min_source_sessions,
    )
