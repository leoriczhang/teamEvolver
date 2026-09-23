"""Synthesize progressive test datasets from sessions and team SOP evidence.

This module is intentionally standalone inside teamEvolver. It consumes the
same accumulated session/evidence context used to edit a Skill and emits test
datasets that become the candidate's True Replay contract.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from team_replay.datasets.materials import collect_session_materials
from team_replay.datasets.schema import (
    DATASET_SCHEMA_V2,
    legacy_case_view,
    make_document,
    normalize_case,
    normalize_document,
    validate_document,
)
from team_replay.datasets.store import (
    SkillDatasetStore,
    dataset_material_integrity,
)
from teamEvolver.storage import is_not_found_error

DATASET_FORMAT = DATASET_SCHEMA_V2
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*")
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)

_SYNTHESIZE_SYSTEM = """\
你为 Skill 演化生成留存测试（held-out test）数据集。

同一批积累的会话和团队 SOP 证据已被用于产出一个候选 Skill。请构建 {case_count} 个\
贴近真实的测试用例，评估该候选是否内化了那些可复用规程，同时初始提问不得泄露完整需求。

对每个测试用例：
1. query：一个自包含的初始用户请求。绝不能枚举或透露 checklist。
2. requirements：{min_requirements}-{max_requirements} 条扁平的、可独立验证的\
输出/内容要求。不要嵌套条目。每条要求都必须有会话或团队 SOP 证据支撑。
3. trajectory_requirements：对任务执行方式的扁平检查，包括资料读取、工具操作、\
计算、校验和产物写入（以证据支持为限）。
4. source_session_ids：支撑该用例的会话 ID。
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
      "requirements": ["..."],
      "trajectory_requirements": ["..."],
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


def configure_prompt_provider(
    *,
    effective_prompt: Any = None,
    stage_call_options: Any = None,
) -> None:
    """Register host prompt/option resolvers for dataset synthesis."""
    global _PROMPT_PROVIDER, _OPTIONS_PROVIDER
    _PROMPT_PROVIDER = effective_prompt
    _OPTIONS_PROVIDER = stage_call_options


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
) -> str:
    return (
        str(template)
        .replace("{case_count}", str(case_count))
        .replace("{min_requirements}", str(min_requirements))
        .replace("{max_requirements}", str(max_requirements))
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
) -> Optional[dict[str, Any]]:
    query = str(raw.get("query") or raw.get("instruction") or "").strip()
    if not query:
        return None
    requirements = flatten_requirements(raw.get("requirements"))[:max_requirements]
    trajectory = flatten_requirements(raw.get("trajectory_requirements"))
    if not requirements and not trajectory:
        return None
    if synthesis_mode == "model" and len(requirements) < min_requirements:
        return None
    source_ids = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw.get("source_session_ids") or []
            if str(item or "").strip()
        )
    )
    window = str(raw.get("evidence_window") or default_window).strip().lower()
    if window not in {"recent", "historical"}:
        window = default_window
    checklist = checklist_items(requirements, trajectory)
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
        "name": str(raw.get("name") or "").strip() or query.splitlines()[0][:80],
        "query": query,
        "skill_ids": [skill_name],
        "checks": checklist,
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
            "skill_name": skill_name,
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
    datasets: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds[: max(1, case_count)]):
        dataset = _normalize_dataset(
            {
                "name": f"{skill_name} test {index + 1}",
                "query": seed.get("instruction"),
                "requirements": grounded_requirements,
                "trajectory_requirements": [],
                "source_session_ids": [seed.get("session_id")],
                "evidence_window": seed.get("evidence_window"),
            },
            skill_name=skill_name,
            min_requirements=min_requirements,
            max_requirements=max_requirements,
            batch_size=batch_size,
            default_window="recent" if index == 0 else "historical",
            synthesis_mode="grounded_fallback",
            materials=materials,
        )
        if dataset:
            datasets.append(dataset)
    return datasets


async def synthesize_evolution_datasets(
    llm: Any,
    *,
    skill_name: str,
    sessions: list[dict[str, Any]],
    candidate_skill: Mapping[str, Any],
    evidence_context: Optional[Mapping[str, Any]] = None,
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]] = None,
    case_count: int = 2,
    min_requirements: int = 12,
    max_requirements: int = 24,
    batch_size: int = 4,
) -> list[dict[str, Any]]:
    """Create test datasets from the exact evidence used for Skill evolution."""
    case_count = max(1, min(6, int(case_count or 2)))
    min_requirements = max(1, int(min_requirements or 1))
    max_requirements = max(min_requirements, int(max_requirements or 24))
    batch_size = max(1, int(batch_size or 1))
    compact_sessions = [_session_payload(session) for session in sessions[:50]]
    source_materials = collect_session_materials(sessions)
    payload = {
        "skill_name": skill_name,
        "candidate_skill": {
            "description": str(candidate_skill.get("description") or ""),
            "content": str(candidate_skill.get("content") or "")[:32_000],
            "edit_summary": candidate_skill.get("edit_summary") or {},
        },
        "team_sop_evidence": {
            "context": dict(evidence_context or {}),
            "claims": _team_evidence_claims(candidate_skill),
        },
        "sessions": compact_sessions,
        "replay_seeds": _seed_cases(replay_windows),
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
    )


def dataset_to_replay_case(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Project a synthesized dataset into the True Replay job contract."""
    canonical = normalize_case(
        dataset,
        default_case_id=str(dataset.get("dataset_id") or ""),
    )
    view = legacy_case_view(canonical)
    source = canonical.get("provenance") or {}
    metadata = canonical.get("metadata") or {}
    checklist = [dict(item) for item in canonical["checks"]]
    source_ids = [
        str(item or "")
        for item in source.get("session_ids") or []
        if str(item or "")
    ]
    session_id = str(
        source.get("session_id")
        or (source_ids[0] if source_ids else "")
        or ""
    )
    return {
        "case_id": canonical["case_id"],
        "dataset_id": canonical["case_id"],
        "dataset_format": DATASET_FORMAT,
        "skill_ids": list(canonical.get("skill_ids") or []),
        "skill_name": (
            str(dataset.get("skill_name") or metadata.get("skill_name") or "")
            if len(canonical.get("skill_ids") or []) <= 1
            else ""
        ),
        "session_id": session_id,
        "source_session_ids": source_ids,
        "turn_num": int(source.get("turn_num") or 1),
        "instruction": canonical["query"],
        "query": canonical["query"],
        "requirements": view["requirements"],
        "trajectory_requirements": view["trajectory_requirements"],
        "checklist": checklist,
        "progressive_disclosure": dict(canonical["replay"]["progressive_disclosure"]),
        "materials": [dict(item) for item in canonical["materials"]],
        "evidence_window": str(
            source.get("evidence_window") or "recent"
        ),
    }


class SynthesizedDatasetStore:
    """Persist generated datasets independently from validation job indexes."""

    def __init__(self, bucket: Any, *, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")

    def _key(self, skill_name: str, generation_id: str) -> str:
        digest = hashlib.sha256(skill_name.encode("utf-8")).hexdigest()[:12]
        safe_generation = re.sub(
            r"[^A-Za-z0-9._-]+", "-", str(generation_id or "")
        ).strip("-")
        return (
            f"{self._prefix}evolution_datasets/"
            f"{digest}/{safe_generation or 'generation'}.json"
        )

    def save_generation(
        self,
        *,
        skill_name: str,
        generation_id: str,
        datasets: list[dict[str, Any]],
        source_session_ids: list[str],
        candidate_revision: int,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        canonical_cases: list[dict[str, Any]] = []
        stored_cases = []
        for raw in datasets:
            item = normalize_case(
                raw,
                default_case_id=str(raw.get("dataset_id") or ""),
            )
            canonical_cases.append(item)
            stored_item = copy.deepcopy(item)
            stored_item["materials"] = [
                {
                    key: material.get(key)
                    for key in (
                        "path",
                        "size",
                        "sha256",
                        "source_session_id",
                    )
                    if material.get(key) is not None
                }
                for material in raw.get("materials") or []
                if isinstance(material, Mapping) and material.get("path")
            ]
            stored_cases.append(stored_item)
        payload = make_document(
            dataset_id=generation_id,
            name=f"{skill_name} Test Dataset",
            skills=[skill_name],
            source={
                "kind": "skill_evolution",
                "session_ids": list(dict.fromkeys(source_session_ids)),
            },
            cases=stored_cases,
            metadata={
                "generation_id": generation_id,
                "candidate_revision": int(candidate_revision or 1),
            },
            created_at=now,
            updated_at=now,
        )
        errors = validate_document(payload)
        if errors:
            raise ValueError("invalid Test Dataset: " + "; ".join(errors))
        self._bucket.put_object(
            self._key(skill_name, generation_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        repository = SkillDatasetStore(self._bucket, prefix=self._prefix)
        for raw in canonical_cases:
            dataset_id = str(raw.get("case_id") or "")
            existing = repository.load_dataset(
                skill_name=skill_name,
                dataset_id=dataset_id,
            )
            existing_source = (
                existing.get("source")
                if isinstance((existing or {}).get("source"), Mapping)
                else {}
            )
            # Editable lab datasets may be selected as fixed regressions. The
            # generation audit references them but must not make them read-only.
            if existing and (
                str(existing_source.get("kind") or "") != "evolution"
                or not bool(existing.get("read_only", True))
                or bool(existing_source.get("user_edited"))
            ):
                continue
            generation_ids = list(
                dict.fromkeys(
                    [
                        *(
                            existing_source.get("generation_ids")
                            if isinstance(
                                existing_source.get("generation_ids"),
                                list,
                            )
                            else []
                        ),
                        generation_id,
                    ]
                )
            )
            provenance = (
                raw.get("provenance")
                if isinstance(raw.get("provenance"), Mapping)
                else {}
            )
            dataset_source_ids = [
                str(item or "")
                for item in provenance.get("session_ids") or []
                if str(item or "")
            ]
            decoded_materials: list[tuple[str, bytes]] = []
            material_sources: dict[str, str] = {}
            for material in raw.get("materials") or []:
                if not isinstance(material, Mapping) or not material.get("path"):
                    continue
                rel_path = str(material.get("path") or "")
                try:
                    data = base64.b64decode(
                        str(material.get("content_b64") or ""),
                        validate=True,
                    )
                except (binascii.Error, ValueError):
                    continue
                decoded_materials.append((rel_path, data))
                material_sources[rel_path] = str(
                    material.get("source_session_id") or ""
                )
            material_records = (
                repository.replace_materials(
                    skill_name=skill_name,
                    dataset_id=dataset_id,
                    files=decoded_materials,
                )
                if decoded_materials
                else list((existing or {}).get("materials") or [])
            )
            for record in material_records:
                source_session_id = material_sources.get(
                    str(record.get("path") or "")
                )
                if source_session_id:
                    record["source_session_id"] = source_session_id
            dataset_payload = copy.deepcopy(dict(raw))
            dataset_payload["materials"] = material_records
            dataset_payload["provenance"] = {
                **dict(provenance),
                "kind": "evolution",
                "job_id": generation_id,
                "generation_ids": generation_ids,
                "session_ids": dataset_source_ids,
                "session_id": (
                    dataset_source_ids[0]
                    if dataset_source_ids
                    else ""
                ),
                "evidence_window": str(
                    provenance.get("evidence_window") or "recent"
                ),
                "candidate_revision": int(candidate_revision or 1),
            }
            dataset_payload["metadata"] = {
                **dict(dataset_payload.get("metadata") or {}),
                "read_only": True,
                "enabled_for_evolution": False,
            }
            repository.save_dataset(
                make_document(
                    dataset_id=dataset_id,
                    name=str(raw.get("name") or ""),
                    skills=[skill_name],
                    source=dict(dataset_payload["provenance"]),
                    cases=[dataset_payload],
                    metadata={},
                    created_at=str(
                        (existing or {}).get("created_at")
                        or now
                    ),
                    updated_at=now,
                )
            )
        return payload

    def load_generation(
        self,
        *,
        skill_name: str,
        generation_id: str,
    ) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(
                self._bucket.get_object(
                    self._key(skill_name, generation_id)
                ).read().decode("utf-8")
            )
        except Exception as exc:
            if is_not_found_error(exc):
                return None
            raise
        if not isinstance(payload, dict):
            return None
        return normalize_document(
            payload,
            default_dataset_id=generation_id,
            default_name=f"{skill_name} Test Dataset",
            default_subject={"kind": "skill", "id": skill_name},
        )
