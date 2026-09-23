"""层级归纳阶段：LLM 提议 parent_id → 自洽性多数投票 → 确定性无环约束。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Protocol

from ..corpus.terms import find_chunks
from ..llm.prompts import PROMPT_TAXONOMY_V1, TAXONOMY_SCHEMA, taxonomy_system, taxonomy_user
from ..provenance import evidence_from_chunks
from .context import ProjectContext


class LLM(Protocol):
    model: str

    def complete_json(
        self, *, system: str, user: str, json_schema: dict, prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]: ...


def _propose_edges(
    llm: LLM, concepts: list[dict], chunks: list[dict], samples: int, repair_text: str = ""
) -> list[dict]:
    """返回 [{child, parent, votes:{parent:count}}]（每次采样仅输入概念清单）。"""
    concept_view = [
        {"id": c["concept"]["id"], "name": c["concept"]["name"], "definition": c["concept"]["definition"]}
        for c in concepts
    ]
    votes: dict[str, Counter] = {c["concept"]["id"]: Counter() for c in concepts}
    for _ in range(samples):
        response = llm.complete_json(
            system=taxonomy_system(),
            user=taxonomy_user(concept_view, chunks) + repair_text,
            json_schema=TAXONOMY_SCHEMA,
            prompt_version=PROMPT_TAXONOMY_V1,
        )
        for edge in response.get("edges", []):
            child, parent = edge.get("child"), edge.get("parent")
            if child in votes and (parent is None or parent in votes):
                votes[child][parent] += 1
    result: list[dict] = []
    for child, counter in votes.items():
        if not counter:
            result.append({"child": child, "parent": None, "vote": {}})
            continue
        winner, count = counter.most_common(1)[0]
        majority = count > samples / 2
        result.append(
            {
                "child": child,
                "parent": winner if majority else None,
                "vote": {str(k): v for k, v in counter.items()},
            }
        )
    return result


def _apply_acyclic(proposals: list[dict], concept_ids: set[str]) -> list[dict]:
    """确定性无环约束：按提议顺序加边，成环则退回 null 父。"""
    parent: dict[str, str | None] = {p["child"]: None for p in proposals}

    def would_cycle(child: str, candidate: str) -> bool:
        current = candidate
        while current:
            if current == child:
                return True
            current = parent.get(current)
        return False

    for proposal in proposals:
        candidate = proposal["parent"]
        if candidate and candidate in concept_ids and not would_cycle(proposal["child"], candidate):
            parent[proposal["child"]] = candidate
    return [
        {"child": child, "parent": parent[child], "vote": next(p["vote"] for p in proposals if p["child"] == child)}
        for child in parent
    ]


def generate_taxonomy(
    ctx: ProjectContext,
    llm: LLM,
    *,
    config,
    repair_notes: list[str] | None = None,
) -> list[dict]:
    concepts = ctx.read_jsonl("concepts")
    chunks = ctx.read_jsonl("chunks")
    samples = config.thresholds.taxonomy_self_consistency
    repair_text = ""
    if repair_notes:
        repair_text = "\n\n上一轮结果被确定性校验拒绝，请修正以下问题：\n" + "\n".join(f"- {n}" for n in repair_notes)

    all_ids = {c["concept"]["id"] for c in concepts}
    proposals: list[dict] = []
    batch_size = max(1, config.thresholds.concepts_per_llm_batch)
    for i in range(0, len(concepts), batch_size):
        batch = concepts[i : i + batch_size]
        raw = _propose_edges(llm, batch, chunks, samples, repair_text)
        proposals.extend(raw)

    edges = _apply_acyclic(proposals, all_ids)
    rows: list[dict] = []
    name_by_id = {c["concept"]["id"]: c["concept"]["name"] for c in concepts}
    for edge in edges:
        child, parent = edge["child"], edge["parent"]
        evidence: list[dict] = []
        if parent:
            co = find_chunks(name_by_id[child], chunks, limit=10)
            parent_key = re.sub(r"\s+", "", name_by_id[parent]).lower()
            evidence = [c for c in co if parent_key in re.sub(r"\s+", "", c["text"]).lower()][:3]
        rows.append({"edge": edge, "evidence": evidence_from_chunks(evidence)})

    ctx.write_jsonl("taxonomy_edges", rows)
    return rows
