"""Strict Skill mining serialization for the canonical Test Dataset contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from team_replay.datasets.schema import (
    DATASET_SCHEMA,
    document_skill_ids,
    normalize_document,
    output_requirements,
    trajectory_requirements,
)
from team_replay.datasets.schema import (
    validate_document as validate_canonical_document,
)

SCHEMA_VERSION = DATASET_SCHEMA
DATASET_FORMAT = DATASET_SCHEMA
MINIMUM_REQUIREMENT_TARGET = 12
MAXIMUM_REQUIREMENT_COUNT = 24
PROGRESSIVE_DISCLOSURE = {
    "enabled": True,
    "initial_visibility": "query_only",
    "batch_size": 4,
    "stop_when": "all_checklist_items_satisfied",
}


def inline_material_path(case_index: int, material_index: int) -> str:
    """Deterministic candidate-package location; no file is claimed by this helper."""
    return f"references/mining-materials/case-{case_index + 1:06d}-material-{material_index + 1:06d}.txt"


def canonical_projection(payload: Any) -> Any:
    """Validate historical inline material content against the platform contract.

    Mining output retains its original inline content. This transient projection
    only supplies the shape required by the platform validator; the candidate
    bridge must separately create and verify the actual material files.
    """
    projected = copy.deepcopy(payload)
    if not isinstance(projected, dict) or not isinstance(projected.get("cases"), list):
        return projected
    for case_index, case in enumerate(projected["cases"]):
        if not isinstance(case, dict):
            continue
        from .bounded_compile_plan import normalize_case_materials

        case, _changed = normalize_case_materials(case)
        projected["cases"][case_index] = case
        if not isinstance(case.get("materials"), list):
            continue
        for material_index, material in enumerate(case["materials"]):
            if isinstance(material, dict) and not material.get("path") and isinstance(material.get("content"), str):
                material["path"] = inline_material_path(case_index, material_index)
    return projected


def validate_document(payload: Any, *, expected_skill_name: str | None = None) -> list[str]:
    """Validate Test Dataset plus SkillMiner's stricter quality rules."""
    errors = validate_canonical_document(canonical_projection(payload))
    if errors or not isinstance(payload, dict):
        return errors
    skill_ids = document_skill_ids(payload)
    if not skill_ids:
        errors.append("skills 至少需要一个 Skill")
    elif expected_skill_name and expected_skill_name not in skill_ids:
        errors.append(f"skills 必须包含 {expected_skill_name}")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if not isinstance(metadata.get("candidate_revision"), int):
        errors.append("metadata.candidate_revision 必须是整数")

    for index, case in enumerate(payload.get("cases") or []):
        label = f"cases[{index}]"
        raw_checks = case.get("checks") or []
        if any(not isinstance(check.get("text"), str) for check in raw_checks):
            errors.append(f"{label}.checks 的 text 必须为非空字符串")
        requirements = output_requirements(case)
        if len(set(requirements)) != len(requirements):
            errors.append(f"{label}.checks 中的 output 文本不能重复")
        trajectory = trajectory_requirements(case)
        if not MINIMUM_REQUIREMENT_TARGET <= len(requirements) <= MAXIMUM_REQUIREMENT_COUNT:
            errors.append(
                f"{label}.checks 中的 output 项需要 {MINIMUM_REQUIREMENT_TARGET}-"
                f"{MAXIMUM_REQUIREMENT_COUNT} 项"
            )
        if not trajectory:
            errors.append(f"{label}.checks 至少需要一个 trajectory 项")
        case_metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
        if case_metadata.get("split") != "test":
            errors.append(f"{label}.metadata.split 必须为 test")
        if case_metadata.get("minimum_requirement_target") != MINIMUM_REQUIREMENT_TARGET:
            errors.append(
                f"{label}.metadata.minimum_requirement_target 必须为 "
                f"{MINIMUM_REQUIREMENT_TARGET}"
            )
        replay = case.get("replay") if isinstance(case.get("replay"), dict) else {}
        if replay.get("progressive_disclosure") != PROGRESSIVE_DISCLOSURE:
            errors.append(f"{label}.replay.progressive_disclosure 不符合规范")
    return errors


def write_document(path: Path, payload: dict[str, Any]) -> None:
    errors = validate_document(payload)
    if errors:
        raise ValueError("; ".join(errors))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_document(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"不是有效 UTF-8 JSON：{exc}"]
    errors = validate_document(payload)
    if errors:
        return None, errors
    try:
        normalized = normalize_document(canonical_projection(payload))
        for original, case in zip(payload["cases"], normalized["cases"], strict=True):
            case["materials"] = copy.deepcopy(original.get("materials", []))
        return normalized, []
    except ValueError as exc:
        return None, [str(exc)]
