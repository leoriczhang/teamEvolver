"""工具目录驱动的参数约束（确定性、无 LLM）+ 工作流工具引用门控。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from team_ontology.engine.config import ProjectConfig, Thresholds
from team_ontology.engine.llm.client import ScriptedLLM
from team_ontology.engine.pipeline.context import ProjectContext
from team_ontology.engine.pipeline.ingest import run_ingest
from team_ontology.engine.pipeline.runner import ALL_STAGES, PipelineRunner
from team_ontology.engine.pipeline.stage_constraints import _tool_constraints
from team_ontology.engine.validation.hugagentos_parity import DomainPackValidator

FIXTURES = Path(__file__).parent / "fixtures"


def _make_context(tmp_path: Path, with_tools: bool = True) -> tuple[ProjectContext, ProjectConfig]:
    config = ProjectConfig(
        pack_id="tool-pack",
        pack_name="工具包",
        domain="procurement",
        languages=["zh"],
        thresholds=Thresholds(min_term_frequency=1),
        tool_catalog_path=str(FIXTURES / "tools" / "catalog.json") if with_tools else None,
    )
    return ProjectContext.create(config, root=tmp_path / ".ontology-enhancer"), config


def test_tool_constraints_deterministic(tmp_path):
    ctx, config = _make_context(tmp_path)
    rows = _tool_constraints(ctx, config)
    assert len(rows) >= 4  # tool 级 required + supplier_name required/minLength + risk_level enum

    constraints = [row["constraint"] for row in rows]
    for constraint in constraints:
        assert constraint["mode"] == "log"
        assert constraint["target"]["tool"] == "supplier_check"
    kinds = {(c["target"].get("parameter"), tuple(sorted(c["schema"]))) for c in constraints}
    assert ("supplier_name", ("minLength",)) in kinds
    assert ("supplier_name", ("required",)) in kinds
    assert ("risk_level", ("enum",)) in kinds
    assert (None, ("required",)) in kinds  # tool 级必填检查

    # 证据为工具目录出处
    evidences = [row["evidence"] for row in rows]
    assert all(e["doc"] == "tool:supplier_check" for ev in evidences for e in ev)

    # 组装后 parity（带真实工具目录）零 error
    from team_ontology.engine.corpus.loader import load_tool_catalog
    from team_ontology.engine.pipeline.assemble import assemble_pack

    annotated = assemble_pack(ctx, config=config, model="tool-test")
    _, report = DomainPackValidator().validate(
        annotated.clean_pack_dict(),
        tool_schemas=load_tool_catalog(FIXTURES / "tools" / "catalog.json"),
    )
    assert report.valid, report.errors


def test_workflow_required_tools_gated_by_catalog(tmp_path):
    """工作流只保留目录中真实存在的工具；未知工具被剔除，产物 parity 有效。"""
    ctx, config = _make_context(tmp_path)
    run_ingest(ctx, corpus_path=FIXTURES / "corpus_zh", config=config)

    def responder(version: str, _system: str, _user: str, _schema: dict) -> dict:
        if version == "terms-v1":
            return {
                "concepts": [
                    {
                        "id": "SupplierRisk",
                        "name": "供应商风险",
                        "aliases": [],
                        "definition": "供应商相关风险。",
                        "risk": "medium",
                        "source_terms": ["供应商风险"],
                    }
                ],
                "dropped": [],
            }
        if version == "taxonomy-v1":
            return {"edges": [{"child": "SupplierRisk", "parent": None}]}
        if version == "relations-v2":
            return {"relations": []}
        if version == "closed-values-v2":
            return {"entries": []}
        if version == "iron-laws-v3":
            return {"constraints": []}
        if version == "workflows-v4":
            return {
                "workflows": [
                    {
                        "id": "wf_tool",
                        "name": "供应商核验工作流",
                        "triggers": ["供应商风险"],
                        "required_tools": ["supplier_check", "ghost_tool"],
                        "forbidden_tools": [],
                        "output_tags": [],
                        "review_level": "checkpoint",
                        "risk": "medium",
                        "concept_ids": ["SupplierRisk"],
                    }
                ]
            }
        raise AssertionError(f"unexpected version: {version}")

    report = PipelineRunner(ctx, ScriptedLLM(responder)).run(config, stages=ALL_STAGES, force=True, repair_rounds=1)
    assert report["valid"], report["errors"]

    from team_ontology.engine.provenance import load_annotated

    annotated = load_annotated(ctx.annotated_path(config.pack_id))
    workflow = annotated.pack.workflows[0]
    assert workflow.required_tools == ["supplier_check"]
    assert "ghost_tool" not in workflow.required_tools


def test_workflow_risk_falls_back_to_trigger_concepts(tmp_path):
    """concept_ids 为空时，从触发词回推概念聚合风险（review_level 不再恒为 none）。"""
    from team_ontology.engine.pipeline.stage_workflows import _concepts_from_triggers

    concepts = [
        {"concept": {"id": "Marketing", "name": "营销", "aliases": [], "risk": "medium"}},
        {"concept": {"id": "Campaign", "name": "活动", "aliases": [], "risk": "medium"}},
        {"concept": {"id": "Price", "name": "价格", "aliases": [], "risk": "medium"}},
    ]
    hits = _concepts_from_triggers(concepts, ["营销活动", "优惠券", "价格"])
    assert set(hits) == {"Marketing", "Campaign", "Price"}
    assert _concepts_from_triggers(concepts, ["无关词"]) == []


def test_terms_stage_strips_llm_extra_keys(tmp_path):
    """LLM 在概念里夹带 evidence 等额外键时，白名单清洗保证组装可用。"""
    from team_ontology.engine.pipeline.stage_terms import generate_concepts

    config = ProjectConfig(
        pack_id="strip-extra", pack_name="n", domain="d", languages=["zh"], thresholds=Thresholds(min_term_frequency=1)
    )
    ctx = ProjectContext.create(config, root=tmp_path / ".ontology-enhancer")
    ctx.write_jsonl(
        "chunks",
        [
            {
                "chunk_id": "c-0000",
                "doc": "d.md",
                "heading": "",
                "index": 0,
                "text": "客户是物流业务的服务对象。客户等级分为普通、银卡、金卡三种。",
            }
        ],
    )
    ctx.write_jsonl(
        "terms", [{"term": "客户", "weight": 2.0, "doc_count": 1}, {"term": "客户等级", "weight": 1.0, "doc_count": 1}]
    )

    class LLM:
        model = "m"

        def complete_json(self, **kw):
            return {
                "concepts": [
                    {
                        "id": "Customer",
                        "name": "客户",
                        "aliases": [],
                        "definition": "物流业务的服务对象。",
                        "risk": "medium",
                        "source_terms": ["客户"],
                        "evidence": ["c-0000"],  # 夹带键：应被清洗
                    }
                ],
                "dropped": ["客户等级"],
            }

    rows = generate_concepts(ctx, LLM(), config=config)
    assert "evidence" not in rows[0]["concept"]
    from team_ontology.engine.pipeline.assemble import assemble_pack

    annotated = assemble_pack(ctx, config=config, model="m")  # 不应抛 extra_forbidden
    assert annotated.pack.concepts[0].id == "Customer"
