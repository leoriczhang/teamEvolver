"""SkillMiner benchmark serialization for teamEvolver progressive tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DATASET_FORMAT = "teamEvolver-progressive-test-v1"
MINIMUM_REQUIREMENT_TARGET = 12
MAXIMUM_REQUIREMENT_COUNT = 24
PROGRESSIVE_DISCLOSURE = {
    "enabled": True,
    "initial_visibility": "query_only",
    "batch_size": 4,
    "stop_when": "all_checklist_items_satisfied",
}


def _unique_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _negative_requirement(text: str) -> str:
    negative_prefixes = ("不", "不得", "不能", "禁止", "避免", "严禁", "切勿")
    if text.startswith(negative_prefixes):
        return text
    return f"不得{text}"


def _requirements(question: dict[str, Any]) -> list[str]:
    explicit = _unique_texts(question.get("requirements"))
    if explicit:
        return explicit[:MAXIMUM_REQUIREMENT_COUNT]

    gold = question.get("gold") if isinstance(question.get("gold"), dict) else {}
    requirements = []
    labels = gold.get("expected_label")
    if isinstance(labels, dict):
        requirements.extend(f"{key}应为{value}" for key, value in labels.items())
    requirements.extend(_unique_texts(gold.get("must_hit")))
    requirements.extend(_negative_requirement(item) for item in _unique_texts(gold.get("must_avoid")))
    return _unique_texts(requirements)[:MAXIMUM_REQUIREMENT_COUNT]


def _trajectory_requirements(question: dict[str, Any]) -> list[str]:
    explicit = _unique_texts(question.get("trajectory_requirements"))
    if explicit:
        return explicit

    sim = question.get("customer_sim") if isinstance(question.get("customer_sim"), dict) else {}
    result = [
        f"主动询问并确认隐藏事实：{fact}"
        for fact in _unique_texts(sim.get("hidden_facts"))
    ]
    reveal_rules = str(sim.get("reveal_rules") or "").strip()
    if reveal_rules:
        result.append(f"按信息透露规则推进对话：{reveal_rules}")
    pressure = _unique_texts(sim.get("pressure_tactics"))
    if pressure:
        result.append(f"面对施压仍遵守规则与权限边界：{'；'.join(pressure)}")
    stop_when = str(sim.get("stop_when") or "").strip()
    if stop_when:
        result.append(f"完成处理前确认结束条件：{stop_when}")
    return _unique_texts(result) or ["信息不足时主动澄清，不凭空补全事实。"]


def _source_session_ids(question: dict[str, Any]) -> list[str]:
    return _unique_texts(question.get("source_session_ids"))


def _dataset_name(question: dict[str, Any], index: int) -> str:
    explicit = str(question.get("name") or "").strip()
    if explicit:
        return explicit
    query = str(question.get("input") or question.get("query") or "").strip()
    first_line = query.splitlines()[0].strip() if query else "评测场景"
    return f"{question.get('id') or f'BM-{index:02d}'} · {first_line[:60]}"


def build_document(
    skill_name: str,
    questions: list[dict[str, Any]],
    *,
    candidate_revision: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Convert SkillMiner's in-memory questions to the progressive-test schema."""
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    datasets = []
    all_session_ids = []
    seen_session_ids = set()

    for index, question in enumerate(questions, start=1):
        query = str(question.get("input") or question.get("query") or "").strip()
        requirements = _requirements(question)
        trajectory = _trajectory_requirements(question)
        session_ids = _source_session_ids(question)
        for session_id in session_ids:
            if session_id not in seen_session_ids:
                seen_session_ids.add(session_id)
                all_session_ids.append(session_id)

        identity = json.dumps(
            {"skill_name": skill_name, "query": query, "requirements": requirements},
            ensure_ascii=False,
            sort_keys=True,
        )
        dataset_id = f"synth-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
        checklist = [
            {"id": f"R{item_index:02d}", "text": text, "kind": "output"}
            for item_index, text in enumerate(requirements, start=1)
        ] + [
            {"id": f"T{item_index:02d}", "text": text, "kind": "trajectory"}
            for item_index, text in enumerate(trajectory, start=1)
        ]
        datasets.append({
            "dataset_id": dataset_id,
            "dataset_format": DATASET_FORMAT,
            "skill_name": skill_name,
            "split": "test",
            "name": _dataset_name(question, index),
            "query": query,
            "requirements": requirements,
            "trajectory_requirements": trajectory,
            "checklist": checklist,
            "source_session_ids": session_ids,
            "evidence_window": str(question.get("evidence_window") or (
                "historical" if question.get("in_corpus", True) else "recent"
            )),
            "synthesis_mode": str(question.get("synthesis_mode") or "model"),
            "requirement_count": len(requirements),
            "minimum_requirement_target": MINIMUM_REQUIREMENT_TARGET,
            "progressive_disclosure": dict(PROGRESSIVE_DISCLOSURE),
            "created_at": timestamp,
        })

    generation_seed = "|".join(dataset["dataset_id"] for dataset in datasets)
    generation_digest = hashlib.sha256(generation_seed.encode("utf-8")).hexdigest()[:8]
    try:
        generation_stamp = datetime.fromisoformat(timestamp).astimezone(timezone.utc).strftime(
            "%Y%m%d%H%M%S"
        )
    except ValueError:
        generation_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_format": DATASET_FORMAT,
        "skill_name": skill_name,
        "generation_id": f"{generation_stamp}-{skill_name}-{generation_digest}",
        "candidate_revision": candidate_revision,
        "source_session_ids": all_session_ids,
        "datasets": datasets,
        "created_at": timestamp,
    }


def validate_document(payload: Any, *, expected_skill_name: str | None = None) -> list[str]:
    """Validate the structural contract shown by teamEvolver progressive tests."""
    if not isinstance(payload, dict):
        return ["顶层必须是 JSON 对象"]

    errors = []
    required_top = {
        "schema_version", "dataset_format", "skill_name", "generation_id",
        "candidate_revision", "source_session_ids", "datasets", "created_at",
    }
    missing_top = sorted(required_top - set(payload))
    if missing_top:
        errors.append(f"顶层缺少字段：{', '.join(missing_top)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {SCHEMA_VERSION}")
    if payload.get("dataset_format") != DATASET_FORMAT:
        errors.append(f"dataset_format 必须为 {DATASET_FORMAT}")
    skill_name = str(payload.get("skill_name") or "").strip()
    if not skill_name:
        errors.append("skill_name 不能为空")
    elif expected_skill_name and skill_name != expected_skill_name:
        errors.append(f"skill_name 应为 {expected_skill_name}")
    if not isinstance(payload.get("candidate_revision"), int):
        errors.append("candidate_revision 必须是整数")
    if not isinstance(payload.get("source_session_ids"), list):
        errors.append("source_session_ids 必须是数组")

    datasets = payload.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append("datasets 必须是非空数组")
        return errors

    required_dataset = {
        "dataset_id", "dataset_format", "skill_name", "split", "name", "query",
        "requirements", "trajectory_requirements", "checklist", "source_session_ids",
        "evidence_window", "synthesis_mode", "requirement_count",
        "minimum_requirement_target", "progressive_disclosure", "created_at",
    }
    seen_ids = set()
    for index, dataset in enumerate(datasets, start=1):
        label = f"datasets[{index - 1}]"
        if not isinstance(dataset, dict):
            errors.append(f"{label} 必须是对象")
            continue
        missing = sorted(required_dataset - set(dataset))
        if missing:
            errors.append(f"{label} 缺少字段：{', '.join(missing)}")
        dataset_id = str(dataset.get("dataset_id") or "").strip()
        if not dataset_id:
            errors.append(f"{label}.dataset_id 不能为空")
        elif dataset_id in seen_ids:
            errors.append(f"{label}.dataset_id 重复")
        seen_ids.add(dataset_id)
        if dataset.get("dataset_format") != DATASET_FORMAT:
            errors.append(f"{label}.dataset_format 不正确")
        if dataset.get("skill_name") != skill_name:
            errors.append(f"{label}.skill_name 与顶层不一致")
        if dataset.get("split") != "test":
            errors.append(f"{label}.split 必须为 test")
        if not str(dataset.get("name") or "").strip():
            errors.append(f"{label}.name 不能为空")
        if not str(dataset.get("query") or "").strip():
            errors.append(f"{label}.query 不能为空")

        requirements = dataset.get("requirements")
        trajectory = dataset.get("trajectory_requirements")
        if (
            not isinstance(requirements, list)
            or not MINIMUM_REQUIREMENT_TARGET <= len(requirements) <= MAXIMUM_REQUIREMENT_COUNT
        ):
            errors.append(
                f"{label}.requirements 需要 {MINIMUM_REQUIREMENT_TARGET}-"
                f"{MAXIMUM_REQUIREMENT_COUNT} 项"
            )
            requirements = requirements if isinstance(requirements, list) else []
        if not isinstance(trajectory, list) or not trajectory:
            errors.append(f"{label}.trajectory_requirements 必须是非空数组")
            trajectory = trajectory if isinstance(trajectory, list) else []
        if dataset.get("requirement_count") != len(requirements):
            errors.append(f"{label}.requirement_count 与 requirements 数量不一致")
        if dataset.get("minimum_requirement_target") != MINIMUM_REQUIREMENT_TARGET:
            errors.append(
                f"{label}.minimum_requirement_target 必须为 {MINIMUM_REQUIREMENT_TARGET}"
            )
        if dataset.get("progressive_disclosure") != PROGRESSIVE_DISCLOSURE:
            errors.append(f"{label}.progressive_disclosure 不符合规范")
        if not isinstance(dataset.get("source_session_ids"), list):
            errors.append(f"{label}.source_session_ids 必须是数组")

        expected_checklist = [
            {"id": f"R{item_index:02d}", "text": str(text), "kind": "output"}
            for item_index, text in enumerate(requirements, start=1)
        ] + [
            {"id": f"T{item_index:02d}", "text": str(text), "kind": "trajectory"}
            for item_index, text in enumerate(trajectory, start=1)
        ]
        if dataset.get("checklist") != expected_checklist:
            errors.append(f"{label}.checklist 与 requirements/trajectory_requirements 不一致")
    return errors


def write_document(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_document(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"不是有效 UTF-8 JSON：{exc}"]
    errors = validate_document(payload)
    return payload if isinstance(payload, dict) else None, errors


def to_runner_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt the canonical document to the legacy in-memory scoring interface."""
    questions = []
    for dataset in payload.get("datasets") or []:
        requirements = _unique_texts(dataset.get("requirements"))
        must_avoid = [
            item for item in requirements
            if item.startswith(("不", "不得", "不能", "禁止", "避免", "严禁", "切勿"))
        ]
        must_hit = [item for item in requirements if item not in must_avoid]
        questions.append({
            "id": dataset.get("dataset_id"),
            "name": dataset.get("name"),
            "target_dimensions": [],
            "difficulty": "medium",
            "input": dataset.get("query"),
            "requirements": requirements,
            "trajectory_requirements": _unique_texts(dataset.get("trajectory_requirements")),
            "gold": {"expected_label": {}, "must_hit": must_hit, "must_avoid": must_avoid},
            "customer_sim": {},
            "source": ",".join(_unique_texts(dataset.get("source_session_ids"))),
            "source_session_ids": _unique_texts(dataset.get("source_session_ids")),
            "in_corpus": dataset.get("evidence_window") == "historical",
            "evidence_window": dataset.get("evidence_window"),
            "synthesis_mode": dataset.get("synthesis_mode"),
        })
    return questions
