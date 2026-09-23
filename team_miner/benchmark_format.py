"""SkillMiner serialization for the canonical Test Dataset v2 contract."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from team_replay.datasets.schema import (
    DATASET_SCHEMA_V2,
    document_skill_ids,
    legacy_case_view,
    make_document,
    normalize_document,
    output_requirements,
    trajectory_requirements,
    validate_document as validate_canonical_document,
)

SCHEMA_VERSION = DATASET_SCHEMA_V2
DATASET_FORMAT = DATASET_SCHEMA_V2
MINIMUM_REQUIREMENT_TARGET = 12
MAXIMUM_REQUIREMENT_COUNT = 24
PROGRESSIVE_DISCLOSURE = {
    "enabled": True,
    "initial_visibility": "query_only",
    "batch_size": 4,
    "stop_when": "all_checklist_items_satisfied",
}
_NEGATIVE_PREFIXES = ("不", "不得", "不能", "禁止", "避免", "严禁", "切勿")

# The progressive-test format needs enough independent checks to make an
# automated score meaningful.  A model occasionally returns 11 (or fewer)
# otherwise-valid checks.  We preserve its checks and only append conservative
# evidence/guardrail checks; the companion quality report makes that downgrade
# visible to the reviewer instead of dropping the whole mining result.
_SUPPLEMENTAL_REQUIREMENT_TEMPLATES = (
    "说明结论的适用前提、信息来源和适用范围。",
    "信息不足时主动澄清，不把未验证信息当作事实。",
    "明确权限边界；需要授权或升级时给出下一步。",
    "给出可执行的处理步骤及完成条件。",
    "说明关键例外、风险或不可承诺的部分。",
    "避免编造规则、数值、时限或处理结果。",
    "将建议与可追溯证据或待核验项对应。",
    "在结论无法确认时明确标注不确定性和人工复核建议。",
    "覆盖用户当前诉求，不遗漏关键限制条件。",
    "输出清晰、可核验且不与已知材料矛盾。",
    "遵守安全、合规和隐私保护边界。",
    "在处理结束前确认后续责任人、渠道或复核方式。",
)


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
    if text.startswith(_NEGATIVE_PREFIXES):
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


def _complete_requirements(requirements: list[str]) -> tuple[list[str], list[str]]:
    """Pad a short rubric with explicitly conservative review requirements.

    The original model output stays first and is never rewritten.  The return
    value includes only the generated supplement so callers can disclose that
    the resulting benchmark is lower-confidence.
    """
    completed = list(requirements[:MAXIMUM_REQUIREMENT_COUNT])
    added: list[str] = []
    for candidate in _SUPPLEMENTAL_REQUIREMENT_TEMPLATES:
        if len(completed) >= MINIMUM_REQUIREMENT_TARGET:
            break
        if candidate in completed:
            continue
        completed.append(candidate)
        added.append(candidate)
    return completed, added


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
    """Convert SkillMiner's in-memory questions to Test Dataset v2."""
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    cases = []
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
        cases.append({
            "case_id": dataset_id,
            "name": _dataset_name(question, index),
            "query": query,
            "checks": checklist,
            "materials": [],
            "provenance": {
                "session_ids": session_ids,
                "evidence_window": str(question.get("evidence_window") or (
                    "historical" if question.get("in_corpus", True) else "recent"
                )),
                "synthesis_mode": str(question.get("synthesis_mode") or "model"),
            },
            "replay": {
                "progressive_disclosure": dict(PROGRESSIVE_DISCLOSURE),
            },
            "metadata": {
                "split": "test",
                "minimum_requirement_target": MINIMUM_REQUIREMENT_TARGET,
                "target_dimensions": list(question.get("target_dimensions") or []),
                "difficulty": str(question.get("difficulty") or "medium"),
                "gold": question.get("gold") if isinstance(question.get("gold"), dict) else {},
                "customer_sim": (
                    question.get("customer_sim")
                    if isinstance(question.get("customer_sim"), dict)
                    else {}
                ),
                "in_corpus": bool(question.get("in_corpus", True)),
                "source_label": str(question.get("source") or ""),
            },
        })

    generation_seed = "|".join(case["case_id"] for case in cases)
    generation_digest = hashlib.sha256(generation_seed.encode("utf-8")).hexdigest()[:8]
    try:
        generation_stamp = datetime.fromisoformat(timestamp).astimezone(timezone.utc).strftime(
            "%Y%m%d%H%M%S"
        )
    except ValueError:
        generation_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    generation_id = f"{generation_stamp}-{skill_name}-{generation_digest}"
    return make_document(
        dataset_id=generation_id,
        name=f"{skill_name} Test Dataset",
        skills=[skill_name],
        source={"kind": "skill_miner", "session_ids": all_session_ids},
        cases=cases,
        metadata={
            "generation_id": generation_id,
            "candidate_revision": candidate_revision,
        },
        created_at=timestamp,
        updated_at=timestamp,
    )


def build_document_with_quality(
    skill_name: str,
    questions: list[dict[str, Any]],
    *,
    candidate_revision: int = 1,
    created_at: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a format-valid document and report any conservative repairs.

    This is intentionally separate from :func:`build_document`: callers that
    need strict validation can still use the exact model rubric, while the
    mining lifecycle can finish with a clearly marked, review-required output.
    """
    normalized: list[dict[str, Any]] = []
    quality_warnings: list[dict[str, Any]] = []
    for index, question in enumerate(questions, start=1):
        item = dict(question)
        original = _requirements(item)
        completed, added = _complete_requirements(original)
        item["requirements"] = completed
        normalized.append(item)
        if added:
            quality_warnings.append({
                "code": "requirements_auto_completed",
                "dataset_index": index,
                "dataset_name": _dataset_name(item, index),
                "original_requirement_count": len(original),
                "added_requirement_count": len(added),
                "message": (
                    f"第 {index} 个 Benchmark 的模型评分锚点只有 {len(original)} 项；"
                    f"已补入 {len(added)} 项保守核验要求，需人工复核。"
                ),
            })
    return build_document(
        skill_name,
        normalized,
        candidate_revision=candidate_revision,
        created_at=created_at,
    ), quality_warnings


def validate_document(payload: Any, *, expected_skill_name: str | None = None) -> list[str]:
    """Validate Test Dataset v2 plus SkillMiner's stricter quality rules."""
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
        requirements = output_requirements(case)
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_document(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"不是有效 UTF-8 JSON：{exc}"]
    try:
        document = normalize_document(payload)
    except ValueError as exc:
        return None, [str(exc)]
    errors = validate_document(document)
    return document, errors


def to_runner_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt the canonical document to the legacy in-memory scoring interface."""
    questions = []
    for case in payload.get("cases") or []:
        view = legacy_case_view(case, document=payload)
        requirements = output_requirements(case)
        must_avoid = [
            item["text"]
            for item in case.get("checks") or []
            if item.get("kind") == "output" and item.get("polarity") == "must_not"
        ]
        if not must_avoid:
            must_avoid = [
                item for item in requirements
                if item.startswith(_NEGATIVE_PREFIXES)
            ]
        must_hit = [item for item in requirements if item not in must_avoid]
        metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
        gold = metadata.get("gold") if isinstance(metadata.get("gold"), dict) else {}
        questions.append({
            "id": case.get("case_id"),
            "name": case.get("name"),
            "skill_ids": list(case.get("skill_ids") or []),
            "target_dimensions": list(metadata.get("target_dimensions") or []),
            "difficulty": str(metadata.get("difficulty") or "medium"),
            "input": case.get("query"),
            "requirements": requirements,
            "trajectory_requirements": trajectory_requirements(case),
            "gold": {
                "expected_label": gold.get("expected_label") or {},
                "must_hit": gold.get("must_hit") or must_hit,
                "must_avoid": gold.get("must_avoid") or must_avoid,
            },
            "customer_sim": metadata.get("customer_sim") or {},
            "source": str(metadata.get("source_label") or ""),
            "source_session_ids": view["source_session_ids"],
            "in_corpus": bool(metadata.get("in_corpus", True)),
            "evidence_window": view.get("evidence_window"),
            "synthesis_mode": view.get("synthesis_mode"),
        })
    return questions
