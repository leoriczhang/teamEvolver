"""Canonical Test Dataset schema and legacy adapters.

All persisted datasets use one document envelope. ``skills`` is an unordered
peer set and each case explicitly selects its participating ``skill_ids``.
Legacy shapes remain readable through :func:`normalize_document`, while callers
that still expose old API fields can use :func:`legacy_case_view` at their
boundary.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


DATASET_SCHEMA_V2 = "team-replay.dataset.v2"
LEGACY_PROGRESSIVE_FORMAT = "teamEvolver-progressive-test-v1"
LEGACY_SKILL_DATASET_FORMAT = "teamEvolver-skill-dataset-v1"
LEGACY_BENCHMARK_FORMAT = "teamEvolver-benchmark-v1"
LEGACY_SESSION_SCHEMA = "team-replay.session-dataset.v1"

_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*")


class DatasetSchemaError(ValueError):
    """Raised when a dataset cannot be normalized to the canonical schema."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_items(value: Any) -> list[str]:
    """Normalize strings, lists and checklist objects into unique text rows."""
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("text") or item.get("requirement") or ""
        for line in str(item or "").splitlines():
            text = _LIST_PREFIX_RE.sub("", line).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def _check_id(kind: str, index: int) -> str:
    return f"{'T' if kind == 'trajectory' else 'R'}{index:02d}"


def normalize_checks(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the single canonical Checklist representation for one case."""
    explicit = payload.get("checks")
    if not isinstance(explicit, list) or not explicit:
        explicit = payload.get("checklist")

    checks: list[dict[str, Any]] = []
    seen_text: set[tuple[str, str]] = set()
    used_ids: set[str] = set()
    counters = {"output": 0, "trajectory": 0}

    def append(text: Any, *, kind: str, check_id: Any = "", polarity: str = "must") -> None:
        normalized_kind = "trajectory" if kind == "trajectory" else "output"
        normalized_text = str(text or "").strip()
        key = (normalized_kind, normalized_text)
        if not normalized_text or key in seen_text:
            return
        seen_text.add(key)
        counters[normalized_kind] += 1
        normalized_id = str(check_id or "").strip()
        if not normalized_id or normalized_id in used_ids:
            normalized_id = _check_id(normalized_kind, counters[normalized_kind])
            while normalized_id in used_ids:
                counters[normalized_kind] += 1
                normalized_id = _check_id(normalized_kind, counters[normalized_kind])
        used_ids.add(normalized_id)
        check = {
            "id": normalized_id,
            "kind": normalized_kind,
            "text": normalized_text,
        }
        if polarity == "must_not":
            check["polarity"] = "must_not"
        checks.append(check)

    if isinstance(explicit, list):
        for item in explicit:
            if isinstance(item, Mapping):
                append(
                    item.get("text") or item.get("requirement"),
                    kind=str(item.get("kind") or "output"),
                    check_id=item.get("id"),
                    polarity=str(item.get("polarity") or "must"),
                )
            else:
                append(item, kind="output")
        if checks:
            return checks

    for text in text_items(payload.get("requirements")):
        append(text, kind="output")
    for text in text_items(payload.get("trajectory_requirements")):
        append(text, kind="trajectory")

    gold = payload.get("gold") if isinstance(payload.get("gold"), Mapping) else {}
    if not any(item["kind"] == "output" for item in checks):
        for text in text_items(gold.get("must_hit")):
            append(text, kind="output")
        for text in text_items(gold.get("must_avoid")):
            append(text, kind="output", polarity="must_not")
        expected = gold.get("expected_label")
        if isinstance(expected, Mapping):
            for key, value in expected.items():
                append(f"{key}应为{value}", kind="output")
    return checks


def output_requirements(case: Mapping[str, Any]) -> list[str]:
    return [
        str(item.get("text") or "")
        for item in normalize_checks(case)
        if item.get("kind") == "output"
    ]


def trajectory_requirements(case: Mapping[str, Any]) -> list[str]:
    return [
        str(item.get("text") or "")
        for item in normalize_checks(case)
        if item.get("kind") == "trajectory"
    ]


def normalize_skill_refs(value: Any) -> list[dict[str, Any]]:
    """Normalize peer Skill bindings without assigning ownership roles."""
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            skill_id = str(
                item.get("skill_id") or item.get("id") or item.get("name") or ""
            ).strip()
            ref = {"skill_id": skill_id}
            for key in ("revision", "content_hash"):
                if item.get(key) not in (None, ""):
                    ref[key] = copy.deepcopy(item[key])
        else:
            skill_id = str(item or "").strip()
            ref = {"skill_id": skill_id}
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        result.append(ref)
    result.sort(key=lambda item: item["skill_id"])
    return result


def document_skill_ids(document: Mapping[str, Any]) -> list[str]:
    return [
        str(item.get("skill_id") or "")
        for item in normalize_skill_refs(document.get("skills"))
    ]


def _source_session_ids(payload: Mapping[str, Any], provenance: Mapping[str, Any]) -> list[str]:
    values = (
        payload.get("source_session_ids")
        or provenance.get("source_session_ids")
        or provenance.get("session_ids")
        or []
    )
    result = text_items(values)
    session_id = str(payload.get("session_id") or provenance.get("session_id") or "").strip()
    if session_id and session_id not in result:
        result.insert(0, session_id)
    return result


def normalize_case(
    payload: Mapping[str, Any],
    *,
    default_case_id: str = "",
    default_name: str = "",
    default_skill_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Normalize one current or legacy case to the v2 case contract."""
    case_id = str(
        payload.get("case_id")
        or payload.get("item_id")
        or payload.get("dataset_id")
        or payload.get("id")
        or default_case_id
    ).strip()
    query = str(
        payload.get("query")
        or payload.get("instruction")
        or payload.get("input")
        or ""
    ).strip()
    if not case_id:
        raise DatasetSchemaError("case_id is required")
    if not query:
        raise DatasetSchemaError(f"case {case_id} query is required")

    raw_provenance = (
        payload.get("provenance")
        if isinstance(payload.get("provenance"), Mapping)
        else payload.get("source")
        if isinstance(payload.get("source"), Mapping)
        else {}
    )
    provenance = copy.deepcopy(dict(raw_provenance))
    source_session_ids = _source_session_ids(payload, provenance)
    if source_session_ids:
        provenance["session_ids"] = source_session_ids
    field_map = {
        "trace_id": "trace_id",
        "turn_num": "turn_num",
        "timestamp": "timestamp",
        "ingested_at": "ingested_at",
        "evidence_window": "evidence_window",
        "synthesis_mode": "synthesis_mode",
    }
    for source_key, target_key in field_map.items():
        value = payload.get(source_key)
        if value not in (None, "", []):
            provenance[target_key] = copy.deepcopy(value)
    if isinstance(payload.get("session"), Mapping):
        provenance["session_snapshot"] = copy.deepcopy(dict(payload["session"]))
    elif isinstance(payload.get("source_snapshot"), Mapping):
        provenance["session_snapshot"] = copy.deepcopy(dict(payload["source_snapshot"]))

    replay = copy.deepcopy(
        dict(payload.get("replay"))
        if isinstance(payload.get("replay"), Mapping)
        else {}
    )
    disclosure = (
        replay.get("progressive_disclosure")
        if isinstance(replay.get("progressive_disclosure"), Mapping)
        else payload.get("progressive_disclosure")
        if isinstance(payload.get("progressive_disclosure"), Mapping)
        else {}
    )
    replay["progressive_disclosure"] = {
        "enabled": bool(disclosure.get("enabled", True)),
        "initial_visibility": "query_only",
        "batch_size": max(1, int(disclosure.get("batch_size") or 4)),
        "stop_when": "all_checklist_items_satisfied",
    }

    metadata = copy.deepcopy(
        dict(payload.get("metadata"))
        if isinstance(payload.get("metadata"), Mapping)
        else {}
    )
    for key in (
        "split",
        "minimum_requirement_target",
        "read_only",
        "enabled_for_evolution",
        "target_dimensions",
        "difficulty",
        "gold",
        "customer_sim",
        "in_corpus",
        "origin",
        "requirements_source",
        "used_skills",
        "user_alias",
        "judge",
    ):
        if key in payload and key not in metadata:
            metadata[key] = copy.deepcopy(payload[key])
    if isinstance(payload.get("source"), str) and payload.get("source"):
        metadata.setdefault("source_label", str(payload["source"]))
    skill_ids = sorted(text_items(
        payload.get("skill_ids")
        or payload.get("required_skill_ids")
        or payload.get("skill_name")
        or list(default_skill_ids)
    ))

    return {
        "case_id": case_id,
        "name": str(
            payload.get("name")
            or payload.get("title")
            or default_name
            or query.splitlines()[0][:80]
        ),
        "query": query,
        "skill_ids": skill_ids,
        "checks": normalize_checks(payload),
        "materials": [
            copy.deepcopy(dict(item))
            for item in payload.get("materials") or []
            if isinstance(item, Mapping) and item.get("path")
        ],
        "provenance": provenance,
        "replay": replay,
        "metadata": metadata,
    }


def make_document(
    *,
    dataset_id: str,
    name: str,
    cases: Iterable[Mapping[str, Any]],
    description: str = "",
    skills: Iterable[Any] | None = None,
    subject: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    created_at: str = "",
    updated_at: str = "",
) -> dict[str, Any]:
    skill_refs = normalize_skill_refs(list(skills or []))
    if not skill_refs and isinstance(subject, Mapping):
        subject_kind = str(subject.get("kind") or "")
        subject_id = str(subject.get("id") or "").strip()
        if subject_kind == "skill" and subject_id:
            skill_refs = [{"skill_id": subject_id}]
    skill_ids = [item["skill_id"] for item in skill_refs]
    normalized_cases = [
        normalize_case(
            case,
            default_case_id=f"case-{index:03d}",
            default_skill_ids=skill_ids,
        )
        for index, case in enumerate(cases, start=1)
    ]
    if not normalized_cases:
        raise DatasetSchemaError("dataset must contain at least one case")
    now = utc_now_iso()
    return {
        "schema_version": DATASET_SCHEMA_V2,
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


def normalize_document(
    payload: Any,
    *,
    default_dataset_id: str = "",
    default_name: str = "",
    default_subject: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt every persisted v1 shape to the canonical v2 document."""
    if not isinstance(payload, Mapping):
        raise DatasetSchemaError("dataset document must be an object")

    if (
        payload.get("schema_version") == DATASET_SCHEMA_V2
        and isinstance(payload.get("cases"), list)
    ):
        raw_cases = payload.get("cases")
        skills = normalize_skill_refs(payload.get("skills"))
        if not skills and isinstance(payload.get("subject"), Mapping):
            skills = normalize_skill_refs([payload["subject"]])
        return make_document(
            dataset_id=str(payload.get("dataset_id") or default_dataset_id),
            name=str(payload.get("name") or default_name),
            description=str(payload.get("description") or ""),
            skills=skills,
            subject=default_subject,
            source=payload.get("source") if isinstance(payload.get("source"), Mapping) else {},
            cases=[item for item in raw_cases if isinstance(item, Mapping)],
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {},
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )

    if isinstance(payload.get("items"), list):
        metadata = (
            copy.deepcopy(dict(payload.get("metadata")))
            if isinstance(payload.get("metadata"), Mapping)
            else {}
        )
        legacy_schema = str(payload.get("schema_version") or LEGACY_SESSION_SCHEMA)
        if legacy_schema != DATASET_SCHEMA_V2:
            metadata["legacy_schema"] = legacy_schema
        return make_document(
            dataset_id=str(payload.get("dataset_id") or default_dataset_id),
            name=str(payload.get("name") or default_name),
            description=str(payload.get("description") or ""),
            skills=payload.get("skills") or [],
            subject=default_subject,
            source=payload.get("source") if isinstance(payload.get("source"), Mapping) else {},
            cases=[item for item in payload["items"] if isinstance(item, Mapping)],
            metadata=metadata,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )

    if isinstance(payload.get("datasets"), list):
        skill_name = str(payload.get("skill_name") or "")
        return make_document(
            dataset_id=str(
                payload.get("dataset_id")
                or payload.get("generation_id")
                or default_dataset_id
            ),
            name=str(payload.get("name") or default_name or f"{skill_name} Test Dataset"),
            skills=[skill_name] if skill_name else [],
            subject=default_subject,
            source={
                "kind": "skill_evolution",
                "session_ids": text_items(payload.get("source_session_ids")),
            },
            cases=[item for item in payload["datasets"] if isinstance(item, Mapping)],
            metadata={
                "legacy_schema": str(payload.get("dataset_format") or LEGACY_PROGRESSIVE_FORMAT),
                "generation_id": payload.get("generation_id"),
                "candidate_revision": payload.get("candidate_revision"),
            },
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or payload.get("created_at") or ""),
        )

    skill_name = str(payload.get("skill_name") or "")
    dataset_id = str(
        payload.get("dataset_id")
        or payload.get("case_id")
        or payload.get("id")
        or default_dataset_id
    )
    metadata = (
        copy.deepcopy(dict(payload.get("metadata")))
        if isinstance(payload.get("metadata"), Mapping)
        else {}
    )
    legacy_schema = str(
        payload.get("dataset_format")
        or payload.get("schema_version")
        or ""
    )
    if legacy_schema and legacy_schema != DATASET_SCHEMA_V2:
        metadata["legacy_schema"] = legacy_schema
    return make_document(
        dataset_id=dataset_id,
        name=str(payload.get("name") or default_name or payload.get("title") or ""),
        skills=[skill_name] if skill_name else [],
        subject=default_subject,
        source=payload.get("source") if isinstance(payload.get("source"), Mapping) else {},
        cases=[payload],
        metadata=metadata,
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or payload.get("created_at") or ""),
    )


def validate_document(payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["顶层必须是 JSON 对象"]
    errors: list[str] = []
    if payload.get("schema_version") != DATASET_SCHEMA_V2:
        errors.append(f"schema_version 必须为 {DATASET_SCHEMA_V2}")
    if not str(payload.get("dataset_id") or "").strip():
        errors.append("dataset_id 不能为空")
    if not str(payload.get("name") or "").strip():
        errors.append("name 不能为空")
    skills = payload.get("skills")
    if not isinstance(skills, list):
        errors.append("skills 必须是数组")
        skills = []
    skill_ids: set[str] = set()
    for index, skill in enumerate(skills):
        label = f"skills[{index}]"
        if not isinstance(skill, Mapping):
            errors.append(f"{label} 必须是对象")
            continue
        skill_id = str(skill.get("skill_id") or "").strip()
        if not skill_id:
            errors.append(f"{label}.skill_id 不能为空")
        elif skill_id in skill_ids:
            errors.append(f"{label}.skill_id 重复")
        skill_ids.add(skill_id)
    if not isinstance(payload.get("source"), Mapping):
        errors.append("source 必须是对象")
    if not isinstance(payload.get("metadata"), Mapping):
        errors.append("metadata 必须是对象")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases 必须是非空数组")
        return errors
    seen_case_ids: set[str] = set()
    for index, case in enumerate(cases):
        label = f"cases[{index}]"
        if not isinstance(case, Mapping):
            errors.append(f"{label} 必须是对象")
            continue
        case_id = str(case.get("case_id") or "").strip()
        if not case_id:
            errors.append(f"{label}.case_id 不能为空")
        elif case_id in seen_case_ids:
            errors.append(f"{label}.case_id 重复")
        seen_case_ids.add(case_id)
        if not str(case.get("query") or "").strip():
            errors.append(f"{label}.query 不能为空")
        if not str(case.get("name") or "").strip():
            errors.append(f"{label}.name 不能为空")
        case_skill_ids = case.get("skill_ids")
        if not isinstance(case_skill_ids, list):
            errors.append(f"{label}.skill_ids 必须是数组")
        else:
            normalized_case_skill_ids = [
                str(skill_id or "").strip()
                for skill_id in case_skill_ids
            ]
            if any(not skill_id for skill_id in normalized_case_skill_ids):
                errors.append(f"{label}.skill_ids 不能包含空值")
            if skill_ids and not normalized_case_skill_ids:
                errors.append(f"{label}.skill_ids 至少需要引用一个 Skill")
            if len(set(normalized_case_skill_ids)) != len(normalized_case_skill_ids):
                errors.append(f"{label}.skill_ids 不能重复")
            unknown = sorted(set(normalized_case_skill_ids) - skill_ids)
            if unknown:
                errors.append(
                    f"{label}.skill_ids 包含未在顶层声明的 Skill："
                    + ", ".join(unknown)
                )
        if not isinstance(case.get("materials"), list):
            errors.append(f"{label}.materials 必须是数组")
        if not isinstance(case.get("provenance"), Mapping):
            errors.append(f"{label}.provenance 必须是对象")
        replay = case.get("replay")
        if not isinstance(replay, Mapping):
            errors.append(f"{label}.replay 必须是对象")
        elif not isinstance(replay.get("progressive_disclosure"), Mapping):
            errors.append(f"{label}.replay.progressive_disclosure 必须是对象")
        if not isinstance(case.get("metadata"), Mapping):
            errors.append(f"{label}.metadata 必须是对象")
        checks = case.get("checks")
        if not isinstance(checks, list) or not checks:
            errors.append(f"{label}.checks 必须是非空数组")
            continue
        check_ids: set[str] = set()
        for check_index, check in enumerate(checks):
            check_label = f"{label}.checks[{check_index}]"
            if not isinstance(check, Mapping):
                errors.append(f"{check_label} 必须是对象")
                continue
            check_id = str(check.get("id") or "").strip()
            if not check_id:
                errors.append(f"{check_label}.id 不能为空")
            elif check_id in check_ids:
                errors.append(f"{check_label}.id 重复")
            check_ids.add(check_id)
            if check.get("kind") not in {"output", "trajectory"}:
                errors.append(f"{check_label}.kind 必须为 output 或 trajectory")
            if not str(check.get("text") or "").strip():
                errors.append(f"{check_label}.text 不能为空")
    return errors


def legacy_case_view(
    case: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
    text_requirements: bool = False,
    include_snapshot: bool = True,
) -> dict[str, Any]:
    """Project one canonical case onto transitional flat API fields."""
    normalized = normalize_case(case)
    provenance = copy.deepcopy(dict(normalized.get("provenance") or {}))
    session_snapshot = provenance.pop("session_snapshot", None)
    metadata = copy.deepcopy(dict(normalized.get("metadata") or {}))
    output = output_requirements(normalized)
    trajectory = trajectory_requirements(normalized)
    skill_refs = (
        normalize_skill_refs((document or {}).get("skills"))
        if document is not None
        else []
    )
    skill_ids = list(normalized.get("skill_ids") or [])
    if not skill_ids:
        skill_ids = [item["skill_id"] for item in skill_refs]
    if not skill_refs:
        skill_refs = normalize_skill_refs(skill_ids)
    result = {
        "case_id": normalized["case_id"],
        "dataset_id": normalized["case_id"],
        "item_id": normalized["case_id"],
        "dataset_format": DATASET_SCHEMA_V2,
        "skills": skill_refs,
        "skill_ids": skill_ids,
        "skill_name": skill_ids[0] if len(skill_ids) == 1 else "",
        "name": normalized["name"],
        "title": normalized["name"],
        "query": normalized["query"],
        "requirements": "\n".join(output) if text_requirements else output,
        "trajectory_requirements": (
            "\n".join(trajectory) if text_requirements else trajectory
        ),
        "checklist": copy.deepcopy(normalized["checks"]),
        "checks": copy.deepcopy(normalized["checks"]),
        "materials": copy.deepcopy(normalized["materials"]),
        "source": provenance,
        "source_session_ids": list(provenance.get("session_ids") or []),
        "session_id": str(
            provenance.get("session_id")
            or next(iter(provenance.get("session_ids") or []), "")
        ),
        "trace_id": str(provenance.get("trace_id") or ""),
        "turn_num": provenance.get("turn_num") or 1,
        "timestamp": str(provenance.get("timestamp") or ""),
        "ingested_at": str(provenance.get("ingested_at") or ""),
        "evidence_window": str(provenance.get("evidence_window") or ""),
        "synthesis_mode": str(provenance.get("synthesis_mode") or ""),
        "progressive_disclosure": copy.deepcopy(
            normalized["replay"]["progressive_disclosure"]
        ),
        **metadata,
    }
    if document is not None:
        result["created_at"] = str(document.get("created_at") or "")
        result["updated_at"] = str(document.get("updated_at") or "")
        result["description"] = str(document.get("description") or "")
    if include_snapshot and isinstance(session_snapshot, Mapping):
        result["session"] = copy.deepcopy(dict(session_snapshot))
    return result


def legacy_document_view(
    document: Mapping[str, Any],
    *,
    item_key: str = "items",
    text_requirements: bool = False,
    include_snapshot: bool = True,
) -> dict[str, Any]:
    normalized = normalize_document(document)
    return {
        "schema_version": DATASET_SCHEMA_V2,
        "dataset_id": normalized["dataset_id"],
        "name": normalized["name"],
        "description": normalized["description"],
        "created_at": normalized["created_at"],
        "updated_at": normalized["updated_at"],
        "source": copy.deepcopy(normalized["source"]),
        "skills": copy.deepcopy(normalized["skills"]),
        "metadata": copy.deepcopy(normalized["metadata"]),
        item_key: [
            legacy_case_view(
                case,
                document=normalized,
                text_requirements=text_requirements,
                include_snapshot=include_snapshot,
            )
            for case in normalized["cases"]
        ],
    }
