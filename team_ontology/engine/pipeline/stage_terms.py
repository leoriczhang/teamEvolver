"""概念提取阶段：统计候选术语 → LLM 整合为受控概念 → 确定性清洗 + 证据挂接。"""

from __future__ import annotations

import re
from typing import Any, Protocol

from ..corpus.terms import find_chunks
from ..llm.prompts import PROMPT_TERMS_V1, TERMS_SCHEMA, terms_system, terms_user
from ..provenance import evidence_from_chunks
from ..validation.extended import normalize_term
from .context import ProjectContext


class LLM(Protocol):
    model: str

    def complete_json(
        self, *, system: str, user: str, json_schema: dict, prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]: ...


_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:-]")

# Concept v1.0 允许的字段（其余 LLM 返回的键一律丢弃，防止污染组装）
_ALLOWED_CONCEPT_KEYS = {"id", "name", "aliases", "definition", "parent_id", "closed_values", "tags", "risk"}


def _dedupe_concept_id(raw_id: str, taken: set[str]) -> str:
    candidate = raw_id
    counter = 2
    while candidate in taken:
        candidate = f"{raw_id}_{counter}"
        counter += 1
    return candidate


def _clean_concept(concept: dict, taken_ids: set[str]) -> dict:
    # 白名单清洗：LLM 可能夹带 evidence 等额外键（宽 schema 下不受阻）
    concept = {k: v for k, v in concept.items() if k in _ALLOWED_CONCEPT_KEYS}
    raw_id = concept.get("id") or "Concept"
    raw_id = _ID_SAFE_RE.sub("", raw_id)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", raw_id):
        raw_id = "Concept"
    concept["id"] = _dedupe_concept_id(raw_id, taken_ids)
    taken_ids.add(concept["id"])
    name = (concept.get("name") or "").strip() or concept["id"]
    aliases = [a.strip() for a in concept.get("aliases", []) if a and a.strip() != name]
    concept["name"] = name[:255]
    concept["aliases"] = list(dict.fromkeys(aliases))[:32]
    concept["definition"] = (concept.get("definition") or "").strip()[:4000] or "语料中未给出完整定义。"
    if concept.get("risk") not in {"low", "medium", "high"}:
        concept["risk"] = "low"
    concept["source_terms"] = [t.strip() for t in concept.get("source_terms", []) if t.strip()]
    return concept


def generate_concepts(
    ctx: ProjectContext,
    llm: LLM,
    *,
    config,
    repair_notes: list[str] | None = None,
) -> list[dict]:
    """生成 concepts.jsonl：[{concept, source_terms, evidence}]。"""
    terms = ctx.read_jsonl("terms")
    chunks = ctx.read_jsonl("chunks")
    batch_size = config.thresholds.terms_per_llm_batch
    taken_ids: set[str] = set()
    taken_names: set[str] = set()
    rows: list[dict] = []

    batches = [terms[i : i + batch_size] for i in range(0, len(terms), batch_size)]
    for batch in batches:
        batch_terms = [t["term"] for t in batch]
        batch_chunks: list[dict] = []
        for term in batch_terms:
            batch_chunks.extend(find_chunks(term, chunks, limit=2))
        # 去重 chunk
        seen_chunk_ids: set[str] = set()
        unique_chunks: list[dict] = []
        for chunk in batch_chunks:
            if chunk["chunk_id"] not in seen_chunk_ids:
                seen_chunk_ids.add(chunk["chunk_id"])
                unique_chunks.append(chunk)

        repair_text = ""
        if repair_notes:
            repair_text = "\n\n上一轮结果被确定性校验拒绝，请修正以下问题：\n" + "\n".join(
                f"- {n}" for n in repair_notes
            )
        user = terms_user(batch_terms, unique_chunks) + repair_text
        response = llm.complete_json(
            system=terms_system(),
            user=user,
            json_schema=TERMS_SCHEMA,
            prompt_version=PROMPT_TERMS_V1,
        )

        for raw in response.get("concepts", []):
            concept = _clean_concept(raw, taken_ids)
            # 词面冲突消解：名称被已有概念占用 → 视为重复概念，整体丢弃；
            # 别名冲突则仅剔除冲突别名
            name_key = normalize_term(concept["name"])
            if name_key in taken_names:
                continue
            taken_names.add(name_key)
            concept["aliases"] = [a for a in concept["aliases"] if normalize_term(a) not in taken_names]
            for alias in concept["aliases"]:
                taken_names.add(normalize_term(alias))

            evidence_chunks = []
            for surface in [concept["name"], *concept["aliases"]]:
                evidence_chunks.extend(find_chunks(surface, chunks, limit=3))
                if evidence_chunks:
                    break
            rows.append(
                {
                    "concept": concept,
                    "source_terms": concept.pop("source_terms"),
                    "evidence": evidence_from_chunks(evidence_chunks),
                }
            )

    ctx.write_jsonl("concepts", rows)
    return rows
