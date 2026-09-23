"""关系提取阶段：LLM 提议三元组 → 端点/基数/ID 确定性清洗 → 共现证据挂接。"""

from __future__ import annotations

import re
from typing import Any, Protocol

from ..corpus.terms import find_chunks
from ..llm.prompts import (
    PROMPT_RELATIONS_V2,
    RELATIONS_SCHEMA,
    array_field,
    relations_system,
    relations_user,
)
from ..provenance import evidence_from_chunks
from .context import ProjectContext

_NEGATION_RE = re.compile(r"不得|禁止|不能|严禁|不应|不允许")
_MIN_HINT_RE = re.compile(r"至少|最少|以上|一个或多个|多个")
_MAX_HINT_RE = re.compile(r"最多|不超过|以下|以内")


class LLM(Protocol):
    model: str

    def complete_json(
        self, *, system: str, user: str, json_schema: dict, prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]: ...


def generate_relations(
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
    repair_text = ""
    if repair_notes:
        repair_text = "\n\n上一轮结果被确定性校验拒绝，请修正以下问题：\n" + "\n".join(f"- {n}" for n in repair_notes)

    concept_view = [{"id": c["concept"]["id"], "name": c["concept"]["name"]} for c in concepts]
    rows: list[dict] = []
    taken_ids: set[str] = set()
    batch_size = max(1, config.thresholds.concepts_per_llm_batch)
    for i in range(0, len(concepts), batch_size):
        batch = concepts[i : i + batch_size]
        batch_ids = {c["concept"]["id"] for c in batch}
        # 相关语料：批次内概念命中的 chunk
        batch_chunks: list[dict] = []
        seen: set[str] = set()
        for concept in batch:
            for chunk in find_chunks(concept["concept"]["name"], chunks, limit=3):
                if chunk["chunk_id"] not in seen:
                    seen.add(chunk["chunk_id"])
                    batch_chunks.append(chunk)

        response = llm.complete_json(
            system=relations_system(),
            user=relations_user(concept_view, batch_chunks) + repair_text,
            json_schema=RELATIONS_SCHEMA,
            prompt_version=PROMPT_RELATIONS_V2,
        )

        for raw in array_field(response, "relations"):
            subject, object_ = raw.get("subject"), raw.get("object")
            if subject not in concept_ids or object_ not in concept_ids:
                continue
            if subject not in batch_ids and object_ not in batch_ids:
                continue  # 与本批次无关（防止批次间重复提议）
            relation_id = raw.get("id") or f"{subject}_{raw.get('predicate', 'rel')}_{object_}"
            relation_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", relation_id)[:128]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", relation_id):
                relation_id = "Rel"
            base = relation_id
            counter = 2
            while relation_id in taken_ids:
                relation_id = f"{base}_{counter}"
                counter += 1
            taken_ids.add(relation_id)

            min_c = _parse_min_cardinality(raw.get("min_cardinality"))
            max_c = _parse_max_cardinality(raw.get("max_cardinality"))
            if min_c is not None and max_c is not None and min_c > max_c:
                min_c, max_c = None, None  # 冲突时放弃基数声明
            relation = {
                "id": relation_id,
                "subject": subject,
                "predicate": (raw.get("predicate") or "关联").strip()[:128] or "关联",
                "object": object_,
                "description": (raw.get("description") or "").strip()[:2000],
                "min_cardinality": min_c,
                "max_cardinality": max_c,
                "forbidden": bool(raw.get("forbidden")),
            }
            evidence = _relation_evidence(relation, name_by_id, chunks)
            rows.append({"relation": relation, "evidence": evidence_from_chunks(evidence)})

    ctx.write_jsonl("relations", rows)
    return rows


def _parse_min_cardinality(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        numbers = re.findall(r"\d+", value)
        if _MIN_HINT_RE.search(value) and numbers:
            return int(numbers[0])
        if numbers and re.fullmatch(r"\d+", value.strip()):
            return int(numbers[0])
    return None


def _parse_max_cardinality(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        numbers = re.findall(r"\d+", value)
        if _MAX_HINT_RE.search(value) and numbers:
            return int(numbers[0])
        if numbers and re.fullmatch(r"\d+", value.strip()):
            return int(numbers[0])
    return None


def _relation_evidence(relation: dict, name_by_id: dict, chunks: list[dict]) -> list[dict]:
    subject_name = name_by_id.get(relation["subject"], relation["subject"])
    object_name = name_by_id.get(relation["object"], relation["object"])
    key_s = re.sub(r"\s+", "", subject_name).lower()
    key_o = re.sub(r"\s+", "", object_name).lower()
    hits: list[dict] = []
    for chunk in chunks:
        text_key = re.sub(r"\s+", "", chunk["text"]).lower()
        if key_s in text_key and key_o in text_key:
            if relation["forbidden"] and not _NEGATION_RE.search(chunk["text"]):
                continue
            hits.append(chunk)
            if len(hits) >= 3:
                break
    return hits
