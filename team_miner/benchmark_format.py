"""Strict Skill mining serialization for the canonical Test Dataset contract."""

from __future__ import annotations

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


def validate_document(payload: Any, *, expected_skill_name: str | None = None) -> list[str]:
    """Validate Test Dataset plus SkillMiner's stricter quality rules."""
    errors = validate_canonical_document(payload)
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
        return normalize_document(payload), []
    except ValueError as exc:
        return None, [str(exc)]
