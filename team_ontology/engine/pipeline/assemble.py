"""组装：工件 JSONL → AnnotatedPack（v1.0 pack + 证据 + 元数据）。"""

from __future__ import annotations

from ..provenance import AnnotatedPack, ElementMeta, Evidence
from ..schemas import (
    Concept,
    Constraint,
    OntologyPackDocument,
    PackConfig,
    Relation,
    Workflow,
)
from .context import ProjectContext


def _evidence_of(row: dict) -> list[Evidence]:
    return [Evidence.model_validate(e) for e in row.get("evidence", [])]


def _meta_of(row: dict, *, prompt_version: str = "", model: str = "", confidence: float = 1.0) -> ElementMeta:
    votes = row.get("votes") or {}
    return ElementMeta(
        confidence=row.get("confidence", confidence),
        prompt_version=prompt_version,
        model=model,
        votes=votes,
    )


def assemble_pack(
    ctx: ProjectContext,
    *,
    config,
    model: str = "",
    prompt_versions: dict[str, str] | None = None,
) -> AnnotatedPack:
    prompt_versions = prompt_versions or {}

    # 概念
    concept_rows = ctx.read_jsonl("concepts")
    closed_by_id: dict[str, list[str]] = {}
    for row in ctx.read_jsonl("closed_values"):
        closed_by_id[row["concept_id"]] = row["values"]
    parent_by_id: dict[str, str | None] = {}
    edge_meta: dict[str, dict] = {}
    for row in ctx.read_jsonl("taxonomy_edges"):
        edge = row["edge"]
        parent_by_id[edge["child"]] = edge["parent"]
        edge_meta[edge["child"]] = row

    concepts: list[Concept] = []
    for row in concept_rows:
        payload = dict(row["concept"])
        payload["parent_id"] = parent_by_id.get(payload["id"])
        if payload["id"] in closed_by_id:
            payload["closed_values"] = closed_by_id[payload["id"]]
        concepts.append(Concept.model_validate(payload))
    concept_ids = {c.id for c in concepts}

    # 确定性兜底：悬空关系端点直接丢弃、悬空 concept_id 置空，
    # 保证组装永不因 LLM 数据崩溃（parity 校验对最终产物兜底）。
    relation_rows = ctx.read_jsonl("relations")
    relations: list[Relation] = []
    for row in relation_rows:
        payload = dict(row["relation"])
        if payload.get("subject") not in concept_ids or payload.get("object") not in concept_ids:
            continue
        relations.append(Relation.model_validate(payload))

    constraints: list[Constraint] = []
    for name in ("iron_law_constraints", "tool_constraints"):
        for row in ctx.read_jsonl(name):
            payload = dict(row["constraint"])
            if payload.get("concept_id") and payload["concept_id"] not in concept_ids:
                payload["concept_id"] = None
            constraints.append(Constraint.model_validate(payload))
    workflows: list[Workflow] = [Workflow.model_validate(row["workflow"]) for row in ctx.read_jsonl("workflows")]

    thresholds = config.thresholds
    # max_concepts 是运行时注入预算（1..50），不是概念数量硬上限
    injection_budget = max(thresholds.max_concepts, min(len(concepts), 50))
    pack = OntologyPackDocument(
        pack_id=config.pack_id,
        name=config.pack_name,
        version=config.version,
        domain=config.domain,
        description=config.description,
        config=PackConfig(max_concepts=injection_budget),
        concepts=concepts,
        relations=relations,
        constraints=constraints,
        workflows=workflows,
    )

    annotated = AnnotatedPack(pack=pack)
    for i, row in enumerate(concept_rows):
        annotated.set_element(
            f"concepts.{i}",
            evidence=_evidence_of(row),
            prompt_version=prompt_versions.get("terms", ""),
            model=model,
            confidence=0.9,
        )
        if row["concept"]["id"] in edge_meta:
            edge_row = edge_meta[row["concept"]["id"]]
            annotated.set_element(
                f"concepts.{i}.parent",
                evidence=_evidence_of(edge_row),
                prompt_version=prompt_versions.get("taxonomy", ""),
                model=model,
                votes=edge_row.get("edge", {}).get("vote", {}),
                confidence=_edge_confidence(edge_row.get("edge", {})),
            )
    for i, row in enumerate(ctx.read_jsonl("relations")):
        annotated.set_element(
            f"relations.{i}",
            evidence=_evidence_of(row),
            prompt_version=prompt_versions.get("relations", ""),
            model=model,
        )
    for i, row in enumerate(ctx.read_jsonl("iron_law_constraints")):
        annotated.set_element(
            f"constraints.{i}",
            evidence=_evidence_of(row),
            prompt_version=prompt_versions.get("iron_laws", ""),
            model=model,
        )
    base = len(ctx.read_jsonl("iron_law_constraints"))
    for j, row in enumerate(ctx.read_jsonl("tool_constraints")):
        annotated.set_element(
            f"constraints.{base + j}",
            evidence=_evidence_of(row),
            prompt_version=prompt_versions.get("tool_params", ""),
            model=model,
        )
    for i, row in enumerate(ctx.read_jsonl("workflows")):
        annotated.set_element(
            f"workflows.{i}",
            evidence=_evidence_of(row),
            prompt_version=prompt_versions.get("workflows", ""),
            model=model,
        )

    annotated.coverage = {"terms": {t["term"]: t["weight"] for t in ctx.read_jsonl("terms")}}
    return annotated


def _edge_confidence(edge: dict) -> float:
    vote: dict[str, int] = edge.get("vote", {})
    total = sum(vote.values())
    if not total:
        return 0.5
    winner = max(vote.values())
    return winner / total
