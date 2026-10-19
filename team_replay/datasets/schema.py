"""Canonical Test Dataset contract."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

DATASET_SCHEMA = "team-replay.dataset"

_DOCUMENT_KEYS = {
    "schema_version",
    "dataset_id",
    "name",
    "description",
    "skills",
    "source",
    "cases",
    "created_at",
    "updated_at",
    "metadata",
}
_CASE_KEYS = {
    "case_id",
    "query",
    "skill_ids",
    "checks",
    "materials",
    "provenance",
    "replay",
    "metadata",
}


class DatasetSchemaError(ValueError):
    """Raised when a dataset does not match the current contract."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_items(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DatasetSchemaError("checks must be an array")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise DatasetSchemaError(f"checks[{index - 1}] must be an object")
        check_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not check_id or check_id in seen:
            raise DatasetSchemaError("check ids must be nonempty and unique")
        if kind not in {"output", "trajectory"}:
            raise DatasetSchemaError("check kind must be output or trajectory")
        if not text:
            raise DatasetSchemaError("check text is required")
        seen.add(check_id)
        check = {"id": check_id, "kind": kind, "text": text}
        if raw.get("polarity") == "must_not":
            check["polarity"] = "must_not"
        checks.append(check)
    if not checks:
        raise DatasetSchemaError("checks must contain at least one item")
    return checks


def output_requirements(case: Mapping[str, Any]) -> list[str]:
    return [str(item["text"]) for item in normalize_checks(case.get("checks")) if item["kind"] == "output"]


def trajectory_requirements(case: Mapping[str, Any]) -> list[str]:
    return [str(item["text"]) for item in normalize_checks(case.get("checks")) if item["kind"] == "trajectory"]


def normalize_skill_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise DatasetSchemaError("skills must be an array")
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            if set(item) - {"skill_id", "revision", "content_hash"}:
                raise DatasetSchemaError("unsupported Skill reference field")
            skill_id = str(item.get("skill_id") or "").strip()
            ref = {"skill_id": skill_id}
            for key in ("revision", "content_hash"):
                if item.get(key) not in (None, ""):
                    ref[key] = copy.deepcopy(item[key])
        else:
            skill_id = str(item or "").strip()
            ref = {"skill_id": skill_id}
        if not skill_id or skill_id in seen:
            raise DatasetSchemaError("Skill ids must be nonempty and unique")
        seen.add(skill_id)
        result.append(ref)
    result.sort(key=lambda item: item["skill_id"])
    return result


def document_skill_ids(document: Mapping[str, Any]) -> list[str]:
    return [item["skill_id"] for item in normalize_skill_refs(document.get("skills"))]


def normalize_case(
    payload: Mapping[str, Any],
    *,
    default_case_id: str = "",
    default_skill_ids: Iterable[str] = (),
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise DatasetSchemaError("case must be an object")
    unknown = sorted(set(payload) - _CASE_KEYS)
    if unknown:
        raise DatasetSchemaError("unsupported case fields: " + ", ".join(unknown))

    case_id = str(payload.get("case_id") or default_case_id).strip()
    query = str(payload.get("query") or "").strip()
    if not case_id:
        raise DatasetSchemaError("case_id is required")
    if not query:
        raise DatasetSchemaError(f"case {case_id} query is required")

    raw_skill_ids = payload.get("skill_ids") if "skill_ids" in payload else list(default_skill_ids)
    if not isinstance(raw_skill_ids, list) or any(not isinstance(item, str) for item in raw_skill_ids):
        raise DatasetSchemaError("skill_ids must be an array of strings")
    if len(raw_skill_ids) != len(set(raw_skill_ids)) or any(not item.strip() for item in raw_skill_ids):
        raise DatasetSchemaError("skill_ids must be nonempty and unique")
    skill_ids = sorted(text_items(raw_skill_ids))
    materials = payload.get("materials", [])
    provenance = payload.get("provenance", {})
    replay = payload.get("replay", {})
    metadata = payload.get("metadata", {})
    if not isinstance(materials, list):
        raise DatasetSchemaError("materials must be an array")
    if any(not isinstance(item, Mapping) or not item.get("path") for item in materials):
        raise DatasetSchemaError("each material requires a path")
    if not isinstance(provenance, Mapping):
        raise DatasetSchemaError("provenance must be an object")
    if not isinstance(replay, Mapping):
        raise DatasetSchemaError("replay must be an object")
    if not isinstance(metadata, Mapping):
        raise DatasetSchemaError("metadata must be an object")

    raw_disclosure = replay.get("progressive_disclosure", {})
    if not isinstance(raw_disclosure, Mapping):
        raise DatasetSchemaError("replay.progressive_disclosure must be an object")
    batch_size = raw_disclosure.get("batch_size", 4)
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise DatasetSchemaError("progressive disclosure batch_size must be 1..100")
    normalized_replay = copy.deepcopy(dict(replay))
    normalized_replay["progressive_disclosure"] = {
        "enabled": bool(raw_disclosure.get("enabled", True)),
        "initial_visibility": "query_only",
        "batch_size": batch_size,
        "stop_when": "all_checklist_items_satisfied",
    }

    return {
        "case_id": case_id,
        "query": query,
        "skill_ids": skill_ids,
        "checks": normalize_checks(payload.get("checks")),
        "materials": [
            copy.deepcopy(dict(item)) for item in materials if isinstance(item, Mapping) and item.get("path")
        ],
        "provenance": copy.deepcopy(dict(provenance)),
        "replay": normalized_replay,
        "metadata": copy.deepcopy(dict(metadata)),
    }


def make_document(
    *,
    dataset_id: str,
    name: str,
    cases: Iterable[Mapping[str, Any]],
    description: str = "",
    skills: Iterable[Any] | None = None,
    source: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    created_at: str = "",
    updated_at: str = "",
    allow_empty: bool = False,
) -> dict[str, Any]:
    skill_refs = normalize_skill_refs(list(skills or []))
    skill_ids = [item["skill_id"] for item in skill_refs]
    normalized_cases = [
        normalize_case(
            case,
            default_case_id=f"case-{index:03d}",
            default_skill_ids=skill_ids,
        )
        for index, case in enumerate(cases, start=1)
    ]
    if not normalized_cases and not allow_empty:
        raise DatasetSchemaError("dataset must contain at least one case")
    now = utc_now_iso()
    document = {
        "schema_version": DATASET_SCHEMA,
        "dataset_id": str(dataset_id or "").strip(),
        "name": str(name or "").strip(),
        "description": str(description or "").strip(),
        "skills": skill_refs,
        "source": copy.deepcopy(dict(source or {})),
        "cases": normalized_cases,
        "created_at": str(created_at or now),
        "updated_at": str(updated_at or created_at or now),
        "metadata": copy.deepcopy(dict(metadata or {})),
    }
    errors = validate_document(document, allow_empty=allow_empty)
    if errors:
        raise DatasetSchemaError("; ".join(errors))
    return document


def normalize_document(
    payload: Any,
    *,
    default_dataset_id: str = "",
    default_name: str = "",
    allow_empty: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise DatasetSchemaError("dataset document must be an object")
    unknown = sorted(set(payload) - _DOCUMENT_KEYS)
    if unknown:
        raise DatasetSchemaError("unsupported dataset fields: " + ", ".join(unknown))
    if payload.get("schema_version") != DATASET_SCHEMA:
        raise DatasetSchemaError(f"schema_version must be {DATASET_SCHEMA}")
    if not isinstance(payload.get("cases"), list):
        raise DatasetSchemaError("cases must be an array")
    if any(not isinstance(item, Mapping) for item in payload["cases"]):
        raise DatasetSchemaError("every case must be an object")
    for key in ("source", "metadata"):
        if key in payload and not isinstance(payload[key], Mapping):
            raise DatasetSchemaError(f"{key} must be an object")
    return make_document(
        dataset_id=str(payload.get("dataset_id") or default_dataset_id),
        name=str(payload.get("name") or default_name),
        description=str(payload.get("description") or ""),
        skills=payload.get("skills", []),
        source=payload.get("source") if isinstance(payload.get("source"), Mapping) else {},
        cases=payload["cases"],
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {},
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        allow_empty=allow_empty,
    )


def validate_document(payload: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["顶层必须是 JSON 对象"]
    errors: list[str] = []
    if payload.get("schema_version") != DATASET_SCHEMA:
        errors.append(f"schema_version 必须为 {DATASET_SCHEMA}")
    if set(payload) - _DOCUMENT_KEYS:
        errors.append("数据集包含不支持的字段")
    if not str(payload.get("dataset_id") or "").strip():
        errors.append("dataset_id 不能为空")
    if not str(payload.get("name") or "").strip():
        errors.append("name 不能为空")
    skills = payload.get("skills")
    if not isinstance(skills, list):
        errors.append("skills 必须是数组")
        skills = []
    skill_ids = [str(item.get("skill_id") or "").strip() for item in skills if isinstance(item, Mapping)]
    if len(skill_ids) != len(skills) or any(not item for item in skill_ids):
        errors.append("skills 中每项都必须包含 skill_id")
    if len(set(skill_ids)) != len(skill_ids):
        errors.append("skills 中的 skill_id 不能重复")
    if not isinstance(payload.get("source"), Mapping):
        errors.append("source 必须是对象")
    if not isinstance(payload.get("metadata"), Mapping):
        errors.append("metadata 必须是对象")
    cases = payload.get("cases")
    if not isinstance(cases, list) or (not cases and not allow_empty):
        errors.append("cases 必须是非空数组")
        return errors
    seen_case_ids: set[str] = set()
    for index, case in enumerate(cases):
        label = f"cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{label} 必须是对象")
            continue
        unknown = set(case) - _CASE_KEYS
        if unknown:
            errors.append(f"{label} 包含不支持的字段")
        case_id = str(case.get("case_id") or "").strip()
        if not case_id or case_id in seen_case_ids:
            errors.append(f"{label}.case_id 必须非空且唯一")
        seen_case_ids.add(case_id)
        if not str(case.get("query") or "").strip():
            errors.append(f"{label}.query 不能为空")
        case_skill_ids = case.get("skill_ids")
        if not isinstance(case_skill_ids, list):
            errors.append(f"{label}.skill_ids 必须是数组")
        else:
            normalized_ids = [str(item or "").strip() for item in case_skill_ids]
            if any(not item for item in normalized_ids) or len(set(normalized_ids)) != len(normalized_ids):
                errors.append(f"{label}.skill_ids 不能包含空值或重复值")
            unknown_ids = sorted(set(normalized_ids) - set(skill_ids))
            if unknown_ids:
                errors.append(f"{label}.skill_ids 包含未在顶层声明的 Skill：" + ", ".join(unknown_ids))
        try:
            normalize_checks(case.get("checks"))
        except DatasetSchemaError as exc:
            errors.append(f"{label}.{exc}")
        if not isinstance(case.get("materials"), list):
            errors.append(f"{label}.materials 必须是数组")
        if not isinstance(case.get("provenance"), Mapping):
            errors.append(f"{label}.provenance 必须是对象")
        if not isinstance(case.get("replay"), Mapping):
            errors.append(f"{label}.replay 必须是对象")
        if not isinstance(case.get("metadata"), Mapping):
            errors.append(f"{label}.metadata 必须是对象")
    return errors
