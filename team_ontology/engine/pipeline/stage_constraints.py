"""约束挖掘阶段：三来源 ——
1. closed_values：语料枚举句式 → LLM 提取受控取值；
2. iron_law_constraints：语料"铁律"句式 → LLM 转为输出约束（默认 mode=log）；
3. tool_constraints：工具目录参数 schema → 确定性生成 tool_parameter 约束（无需 LLM）。
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ..corpus.loader import load_tool_catalog
from ..corpus.terms import find_chunks
from ..llm.prompts import (
    CLOSED_VALUES_SCHEMA,
    IRON_LAWS_SCHEMA,
    PROMPT_CLOSED_VALUES_V2,
    PROMPT_IRON_LAWS_V3,
    array_field,
    closed_values_system,
    closed_values_user,
    iron_laws_system,
    iron_laws_user,
)
from ..provenance import evidence_from_chunks
from ..validation.hugagentos_parity import tool_input_schema
from .context import ProjectContext


class LLM(Protocol):
    model: str

    def complete_json(
        self, *, system: str, user: str, json_schema: dict, prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]: ...


_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def _unique_id(raw: str, taken: set[str]) -> str:
    candidate = _ID_SAFE_RE.sub("_", raw)[:128]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", candidate):
        candidate = "Constraint"
    base, counter = candidate, 2
    while candidate in taken:
        candidate = f"{base}_{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# 1. closed_values（LLM）
# ---------------------------------------------------------------------------


def _closed_values(ctx: ProjectContext, llm: LLM, config, repair_notes: list[str] | None) -> list[dict]:
    concepts = ctx.read_jsonl("concepts")
    chunks = ctx.read_jsonl("chunks")
    rows: list[dict] = []
    seen_concepts: set[str] = set()
    batch_size = max(1, config.thresholds.concepts_per_llm_batch)
    repair_text = ""
    if repair_notes:
        repair_text = "\n\n上一轮结果被确定性校验拒绝，请修正以下问题：\n" + "\n".join(f"- {n}" for n in repair_notes)
    for i in range(0, len(concepts), batch_size):
        batch = concepts[i : i + batch_size]
        batch_ids = {c["concept"]["id"] for c in batch}
        batch_chunks: list[dict] = []
        seen_chunks: set[str] = set()
        for concept in batch:
            for chunk in find_chunks(concept["concept"]["name"], chunks, limit=3):
                if chunk["chunk_id"] not in seen_chunks:
                    seen_chunks.add(chunk["chunk_id"])
                    batch_chunks.append(chunk)
        response = llm.complete_json(
            system=closed_values_system(),
            user=closed_values_user(
                [{"id": c["concept"]["id"], "name": c["concept"]["name"]} for c in batch],
                batch_chunks,
            )
            + repair_text,
            json_schema=CLOSED_VALUES_SCHEMA,
            prompt_version=PROMPT_CLOSED_VALUES_V2,
        )
        for entry in array_field(response, "entries"):
            if not isinstance(entry, dict):
                continue
            concept_id, values = entry.get("concept_id"), entry.get("values", [])
            if concept_id not in batch_ids or concept_id in seen_concepts:
                continue
            seen_concepts.add(concept_id)
            values = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
            if len(values) < 2:
                continue
            name = next(c["concept"]["name"] for c in batch if c["concept"]["id"] == concept_id)
            evidence = [c for c in find_chunks(name, chunks, limit=8) if any(v in c["text"] for v in values)][:3]
            rows.append({"concept_id": concept_id, "values": values, "evidence": evidence_from_chunks(evidence)})
    ctx.write_jsonl("closed_values", rows)
    return rows


# ---------------------------------------------------------------------------
# 2. iron_law_constraints（LLM）
# ---------------------------------------------------------------------------


def _iron_laws(ctx: ProjectContext, llm: LLM, config, repair_notes: list[str] | None) -> list[dict]:
    concepts = ctx.read_jsonl("concepts")
    chunks = ctx.read_jsonl("chunks")
    concept_ids = {c["concept"]["id"] for c in concepts}
    rows: list[dict] = []
    taken_ids: set[str] = set()
    repair_text = ""
    if repair_notes:
        repair_text = "\n\n上一轮结果被确定性校验拒绝，请修正以下问题：\n" + "\n".join(f"- {n}" for n in repair_notes)
    # 按 chunk 批次喂给 LLM，全部概念清单作为候选引用
    batch_size = 8
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        response = llm.complete_json(
            system=iron_laws_system(),
            user=iron_laws_user(
                [{"id": c["concept"]["id"], "name": c["concept"]["name"]} for c in concepts],
                batch,
            )
            + repair_text,
            json_schema=IRON_LAWS_SCHEMA,
            prompt_version=PROMPT_IRON_LAWS_V3,
        )
        for raw in array_field(response, "constraints"):
            if not isinstance(raw, dict):
                continue
            concept_id = raw.get("concept_id")
            if concept_id and concept_id not in concept_ids:
                continue
            output_tag = (raw.get("output_tag") or "").strip()
            if not output_tag:
                continue
            schema = raw.get("schema") or {}
            if not isinstance(schema, dict):
                continue
            try:
                Draft202012Validator.check_schema(schema)
            except Exception:  # noqa: BLE001, S112 - SchemaError 类型随版本变化，非法即丢弃
                continue  # 非法 schema 直接丢弃，由校验/修复轮兜底重新提议
            constraint = {
                "id": _unique_id(raw.get("id") or "IronLaw", taken_ids),
                "name": (raw.get("name") or "输出要求").strip()[:255],
                "target": {"kind": "output", "output_tag": output_tag[:128]},
                "schema": schema,
                "concept_id": concept_id,
                "requires_citations": bool(raw.get("requires_citations")),
                "prerequisite_tools": [],
                "mode": "log",
                "risk": raw.get("risk") if raw.get("risk") in {"low", "medium", "high"} else "low",
                "message": (raw.get("message") or "输出不满足领域要求。").strip()[:2000],
                "suggestion": (raw.get("suggestion") or "").strip()[:2000],
                "enabled": True,
            }
            # 证据：输出标签/概念任意词面命中的 chunk
            key = output_tag
            surfaces = _concept_surfaces(concept_id, concepts) if concept_id else []
            evidence = [c for c in batch if key in c["text"] or any(surface in c["text"] for surface in surfaces)][:3]
            rows.append({"constraint": constraint, "evidence": evidence_from_chunks(evidence)})
    ctx.write_jsonl("iron_law_constraints", rows)
    return rows


def _concept_surfaces(concept_id: str, concepts: list[dict]) -> list[str]:
    for row in concepts:
        if row["concept"]["id"] == concept_id:
            return [row["concept"]["name"], *row["concept"].get("aliases", [])]
    return [concept_id]


# ---------------------------------------------------------------------------
# 3. tool_constraints（确定性，无 LLM）
# ---------------------------------------------------------------------------


def _tool_constraints(ctx: ProjectContext, config) -> list[dict]:
    rows: list[dict] = []
    if not config.tool_catalog_path:
        ctx.write_jsonl("tool_constraints", [])
        return rows
    from pathlib import Path

    catalog = load_tool_catalog(Path(config.tool_catalog_path))
    taken_ids: set[str] = set()
    for tool_name, tool_schema in sorted(catalog.items()):
        input_schema = tool_input_schema(tool_schema)
        properties = input_schema.get("properties") or {}
        required = set(input_schema.get("required") or [])
        description = tool_schema.get("description") or tool_schema.get("summary") or tool_name

        def add(tool_name: str, tool_description: str, param: str, fragment: dict, aspect: str) -> None:
            constraint = {
                "id": _unique_id(f"{tool_name}_{param}_{aspect}", taken_ids),
                "name": f"工具 {tool_name} 参数 {param} 必须满足 {aspect}",
                "target": {"kind": "tool_parameter", "tool": tool_name, "parameter": param},
                "schema": fragment,
                "concept_id": None,
                "requires_citations": False,
                "prerequisite_tools": [],
                "mode": "log",
                "risk": "low",
                "message": f"调用 {tool_name} 时参数 {param} 不满足约束。",
                "suggestion": f"按工具要求修正 {param}（{aspect}）后重试。",
                "enabled": True,
            }
            quote = json_dumps_compact(fragment)
            rows.append(
                {
                    "constraint": constraint,
                    "evidence": [
                        {"chunk_id": "", "doc": f"tool:{tool_name}", "quote": quote or tool_description[:160]}
                    ],
                }
            )

        for param, param_schema in properties.items():
            if not isinstance(param_schema, dict):
                continue
            if "enum" in param_schema:
                add(tool_name, description, param, {"enum": param_schema["enum"]}, "enum")
            if param in required:
                add(tool_name, description, param, {"required": [param]}, "required")
            for keyword in (
                "minLength",
                "maxLength",
                "minimum",
                "maximum",
                "minItems",
                "maxItems",
                "pattern",
                "format",
            ):
                if keyword in param_schema:
                    add(tool_name, description, param, {keyword: param_schema[keyword]}, keyword)
        if required:
            constraint = {
                "id": _unique_id(f"{tool_name}_required", taken_ids),
                "name": f"工具 {tool_name} 必填参数检查",
                "target": {"kind": "tool", "tool": tool_name},
                "schema": {"required": sorted(required)},
                "concept_id": None,
                "requires_citations": False,
                "prerequisite_tools": [],
                "mode": "log",
                "risk": "low",
                "message": f"调用 {tool_name} 缺少必填参数。",
                "suggestion": f"补齐必填参数：{'、'.join(sorted(required))}。",
                "enabled": True,
            }
            rows.append(
                {
                    "constraint": constraint,
                    "evidence": [{"chunk_id": "", "doc": f"tool:{tool_name}", "quote": description[:160]}],
                }
            )
    ctx.write_jsonl("tool_constraints", rows)
    return rows


def json_dumps_compact(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def generate_constraints(
    ctx: ProjectContext,
    llm: LLM,
    *,
    config,
    repair_notes: list[str] | None = None,
) -> dict[str, list[dict]]:
    closed = _closed_values(ctx, llm, config, repair_notes)
    iron = _iron_laws(ctx, llm, config, repair_notes)
    tool = _tool_constraints(ctx, config)
    return {"closed_values": closed, "iron_law_constraints": iron, "tool_constraints": tool}
