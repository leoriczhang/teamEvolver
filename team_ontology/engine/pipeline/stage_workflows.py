"""工作流提案阶段：概念+约束聚类 → 触发词/工具/输出标签 → 确定性风险映射与资产标签。"""

from __future__ import annotations

import re
from typing import Any, Protocol

from ..corpus.loader import load_tool_catalog
from ..corpus.terms import find_chunks
from ..llm.prompts import (
    PROMPT_WORKFLOWS_V4,
    WORKFLOWS_SCHEMA,
    array_field,
    workflows_system,
    workflows_user,
)
from ..provenance import evidence_from_chunks
from ..validation.extended import normalize_term
from .context import ProjectContext


class LLM(Protocol):
    model: str

    def complete_json(
        self, *, system: str, user: str, json_schema: dict, prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]: ...


def _unique_id(raw: str, taken: set[str]) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)[:128]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", candidate):
        candidate = "Workflow"
    base, counter = candidate, 2
    while candidate in taken:
        candidate = f"{base}_{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


def _coerce_strings(items: Any) -> list[str]:
    """数组元素强转字符串：兼容模型输出对象（取 tag/name/value/id 字段）的场景。"""
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            value = item.get("tag") or item.get("name") or item.get("value") or item.get("id")
            if value:
                result.append(str(value))
        else:
            result.append(str(item))
    return result


def _concepts_from_triggers(concepts: list[dict], triggers: list[str]) -> list[str]:
    """触发词命中的概念（概念名/别名为触发词子串），用于风险聚合兜底。"""
    hits: list[str] = []
    for row in concepts:
        concept = row["concept"]
        surfaces = [concept["name"], *concept.get("aliases", [])]
        if any(surface and any(surface in trigger for trigger in triggers) for surface in surfaces):
            hits.append(concept["id"])
    return hits


def _relevant_chunks(concepts: list[dict], chunks: list[dict], limit: int) -> list[dict]:
    surfaces = [surface for c in concepts for surface in [c["concept"]["name"], *c["concept"].get("aliases", [])]]
    selected: list[dict] = []
    for chunk in chunks:
        if any(surface in chunk["text"] for surface in surfaces):
            selected.append(chunk)
            if len(selected) >= limit:
                break
    return selected or chunks[:limit]


def generate_workflows(
    ctx: ProjectContext,
    llm: LLM,
    *,
    config,
    repair_notes: list[str] | None = None,
) -> list[dict]:
    concepts = ctx.read_jsonl("concepts")
    chunks = ctx.read_jsonl("chunks")
    concept_ids = {c["concept"]["id"] for c in concepts}
    name_by_id = {c["concept"]["id"]: c["concept"]["name"] for c in concepts}

    iron_constraints = ctx.read_jsonl("iron_law_constraints")
    output_constraints = [
        row["constraint"] for row in iron_constraints if row["constraint"]["target"]["kind"] == "output"
    ]
    output_tags = {c["target"]["output_tag"] for c in output_constraints if c["target"].get("output_tag")}

    tools: list[dict] = []
    if config.tool_catalog_path:
        from pathlib import Path

        catalog = load_tool_catalog(Path(config.tool_catalog_path))
        for name, schema in sorted(catalog.items()):
            tools.append(
                {
                    "name": name,
                    "description": (schema.get("description") or schema.get("summary") or name)[:200],
                }
            )

    repair_text = ""
    if repair_notes:
        repair_text = "\n\n上一轮结果被确定性校验拒绝，请修正以下问题：\n" + "\n".join(f"- {n}" for n in repair_notes)

    concept_view = [
        {
            "id": c["concept"]["id"],
            "name": c["concept"]["name"],
            "aliases": c["concept"].get("aliases", []),
        }
        for c in concepts
    ]
    # 控制上下文预算：只送与概念相关的 chunk（上限 60）
    relevant_chunks = _relevant_chunks(concepts, chunks, limit=60)
    response = llm.complete_json(
        system=workflows_system(),
        user=workflows_user(concept_view, output_constraints, tools, relevant_chunks) + repair_text,
        json_schema=WORKFLOWS_SCHEMA,
        prompt_version=PROMPT_WORKFLOWS_V4,
    )

    review_by_risk = config.review_level_by_risk
    known_tools = {t["name"] for t in tools}
    rows: list[dict] = []
    taken_ids: set[str] = set()
    seen_triggers: set[str] = set()

    for raw in array_field(response, "workflows"):
        if not isinstance(raw, dict):
            continue
        involved = [cid for cid in _coerce_strings(raw.get("concept_ids")) if cid in concept_ids]
        # 概念未显式关联时，从触发词回推涉及概念（子串命中概念名/别名）
        if not involved:
            involved = _concepts_from_triggers(concepts, _coerce_strings(raw.get("triggers")))
        risk = raw.get("risk") if raw.get("risk") in {"low", "medium", "high"} else "low"
        # 确定性风险映射：聚合涉及概念的风险
        concept_risks = [c["concept"].get("risk", "low") for c in concepts if c["concept"]["id"] in involved]
        if "high" in concept_risks:
            risk = "high"
        elif "medium" in concept_risks:
            risk = "medium"
        review_level = review_by_risk.get(risk, "none")

        triggers = []
        for trigger in _coerce_strings(raw.get("triggers")):
            trigger = (trigger or "").strip()
            key = normalize_term(trigger)
            if trigger and key not in seen_triggers:
                seen_triggers.add(key)
                triggers.append(trigger[:255])
        if not triggers:
            continue

        required_tools = [t for t in _coerce_strings(raw.get("required_tools")) if t in known_tools]
        forbidden_tools = [t for t in _coerce_strings(raw.get("forbidden_tools")) if t in known_tools]
        output_tags_used = [t for t in _coerce_strings(raw.get("output_tags")) if t in output_tags]

        # 资产触发：involved 概念 → ontology:ConceptId 标签（skill 默认）
        asset_triggers = (
            [
                {
                    "kind": "skill",
                    "ids": [],
                    "tags_any": [f"ontology:{cid}" for cid in involved],
                }
            ]
            if involved
            else []
        )

        workflow = {
            "id": _unique_id(raw.get("id") or "Workflow", taken_ids),
            "name": (raw.get("name") or "领域工作流").strip()[:255],
            "triggers": triggers,
            "asset_triggers": asset_triggers,
            "required_tools": required_tools,
            "forbidden_tools": forbidden_tools,
            "output_tags": output_tags_used,
            "review_level": review_level,
            "risk": risk,
        }

        evidence: list[dict] = []
        for trigger in triggers[:3]:
            evidence.extend(find_chunks(trigger, chunks, limit=2))
        if not evidence and involved:
            evidence.extend(find_chunks(name_by_id[involved[0]], chunks, limit=2))
        rows.append(
            {
                "workflow": workflow,
                "concept_ids": involved,
                "evidence": evidence_from_chunks(evidence[:3]),
            }
        )

    ctx.write_jsonl("workflows", rows)
    return rows
