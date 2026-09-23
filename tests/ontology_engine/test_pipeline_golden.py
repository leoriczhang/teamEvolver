"""黄金语料端到端测试：ScriptedLLM（理想 LLM）+ 中文采购语料 → 完整 Domain Pack。

验收：概念覆盖率 100%、层级/关系/约束/工作流与预埋 ground-truth 一致、
零校验错误、元素证据完备、导出 JSON 干净且 parity 有效。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from team_ontology.engine.config import ProjectConfig, Thresholds
from team_ontology.engine.export.domain_pack import export_all
from team_ontology.engine.llm.client import ScriptedLLM
from team_ontology.engine.pipeline.context import ProjectContext
from team_ontology.engine.pipeline.ingest import run_ingest
from team_ontology.engine.pipeline.runner import ALL_STAGES, PipelineRunner
from team_ontology.engine.validation.extended import normalize_term
from team_ontology.engine.validation.hugagentos_parity import DomainPackValidator

FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

SEEDED_CONCEPTS = [
    {
        "id": "ProcurementRequest",
        "name": "采购申请",
        "aliases": ["采购单"],
        "definition": "包含采购标的、预算、申请部门和候选供应商的业务申请。",
        "risk": "low",
        "source_terms": ["采购申请", "采购单"],
    },
    {
        "id": "Supplier",
        "name": "供应商",
        "aliases": ["供货商"],
        "definition": "提供货物或服务的组织。",
        "risk": "low",
        "source_terms": ["供应商", "供货商"],
    },
    {
        "id": "SupplierRisk",
        "name": "供应商风险",
        "aliases": ["供应风险"],
        "definition": "可能影响供应商履约、合规或持续经营能力的风险。",
        "risk": "medium",
        "source_terms": ["供应商风险", "供应风险"],
    },
    {
        "id": "Risk",
        "name": "风险",
        "aliases": [],
        "definition": "不确定事件对目标造成影响的可能性。",
        "risk": "low",
        "source_terms": ["风险"],
    },
    {
        "id": "Contract",
        "name": "采购合同",
        "aliases": ["供货合同"],
        "definition": "采购部门与供应商签订的供货协议。",
        "risk": "low",
        "source_terms": ["采购合同", "供货合同"],
    },
    {
        "id": "QualificationDoc",
        "name": "资质文件",
        "aliases": [],
        "definition": "证明供应商主体资格的证照文件。",
        "risk": "low",
        "source_terms": ["资质文件"],
    },
    {
        "id": "RiskReport",
        "name": "风险评估报告",
        "aliases": ["风险摘要"],
        "definition": "记录供应商风险事实、影响与建议的报告。",
        "risk": "medium",
        "source_terms": ["风险评估报告", "风险摘要"],
    },
]

SEEDED_PARENTS = {"SupplierRisk": "Risk"}

SEEDED_RELATIONS = [
    ("ProcurementRequest", "包含", "SupplierRisk"),
    ("ProcurementRequest", "关联", "Supplier"),
    ("RiskReport", "记录", "SupplierRisk"),
    ("Contract", "关联", "Supplier"),
]

SEEDED_CLOSED_VALUES = {"SupplierRisk": ["低", "中", "高", "待核验"]}


def _build_llm() -> ScriptedLLM:
    def responder(prompt_version: str, _system: str, user: str, _schema: dict) -> dict:
        if prompt_version == "terms-v1":
            concepts = [
                {key: value for key, value in c.items() if key != "source_terms"} | {"source_terms": c["source_terms"]}
                for c in SEEDED_CONCEPTS
                if any(t in user for t in c["source_terms"]) or c["name"] in user
            ]
            covered = {t for c in concepts for t in c["source_terms"]}
            dropped = [t for t in re.findall(r"^- (.+)$", user, re.MULTILINE) if t not in covered]
            return {"concepts": concepts, "dropped": dropped}
        if prompt_version == "taxonomy-v1":
            edges = []
            for child, parent in SEEDED_PARENTS.items():
                if child in user:
                    edges.append({"child": child, "parent": parent if parent in user else None})
            for concept in SEEDED_CONCEPTS:
                if concept["id"] not in SEEDED_PARENTS and concept["id"] in user:
                    edges.append({"child": concept["id"], "parent": None})
            return {"edges": edges}
        if prompt_version == "relations-v2":
            relations = []
            for index, (subject, predicate, object_) in enumerate(SEEDED_RELATIONS):
                if subject in user and object_ in user:
                    relations.append(
                        {
                            "id": f"rel_{index}",
                            "subject": subject,
                            "predicate": predicate,
                            "object": object_,
                            "description": f"{subject} 与 {object_} 的关系。",
                            "min_cardinality": None,
                            "max_cardinality": None,
                            "forbidden": False,
                        }
                    )
            return {"relations": relations}
        if prompt_version == "closed-values-v2":
            entries = [
                {"concept_id": cid, "values": values} for cid, values in SEEDED_CLOSED_VALUES.items() if cid in user
            ]
            return {"entries": entries}
        if prompt_version == "iron-laws-v3":
            if "至少包含120个字符" not in user:
                return {"constraints": []}
            return {
                "constraints": [
                    {
                        "id": "procurement_summary_complete",
                        "name": "风险摘要必须完整",
                        "output_tag": "procurement_risk_summary",
                        "schema": {"type": "string", "minLength": 120},
                        "concept_id": "RiskReport",
                        "requires_citations": True,
                        "risk": "medium",
                        "message": "风险摘要过短，无法支持业务复核。",
                        "suggestion": "补充风险事实、影响、未知项和建议的核验动作。",
                    }
                ]
            }
        if prompt_version == "workflows-v4":
            return {
                "workflows": [
                    {
                        "id": "procurement_risk_review",
                        "name": "采购风险评审",
                        "triggers": ["供应商风险", "采购风险", "风险摘要"],
                        "required_tools": [],
                        "forbidden_tools": [],
                        "output_tags": ["procurement_risk_summary"],
                        "review_level": "checkpoint",
                        "risk": "medium",
                        "concept_ids": ["ProcurementRequest", "SupplierRisk", "RiskReport"],
                    }
                ]
            }
        raise AssertionError(f"unexpected prompt_version: {prompt_version}")

    return ScriptedLLM(responder, model="golden-scripted")


def _make_context(tmp_path: Path) -> tuple[ProjectContext, ProjectConfig]:
    config = ProjectConfig(
        pack_id="procurement-golden",
        pack_name="采购领域黄金包",
        domain="procurement",
        description="黄金语料端到端验证。",
        languages=["zh"],
        thresholds=Thresholds(
            min_term_frequency=1,
            term_candidates=120,
            terms_per_llm_batch=60,
            concepts_per_llm_batch=40,
            taxonomy_self_consistency=3,
        ),
    )
    ctx = ProjectContext.create(config, root=tmp_path / ".ontology-enhancer")
    return ctx, config


def run_golden(tmp_path: Path):
    ctx, config = _make_context(tmp_path)
    run_ingest(ctx, corpus_path=FIXTURES / "corpus_zh", config=config)
    llm = _build_llm()
    report = PipelineRunner(ctx, llm).run(config, stages=ALL_STAGES, force=True)
    return ctx, config, report


def test_golden_pipeline(tmp_path):
    ctx, config, report = run_golden(tmp_path)
    assert report["valid"], json.dumps(report["errors"], ensure_ascii=False)
    assert not report["errors"]

    from team_ontology.engine.provenance import load_annotated

    annotated = load_annotated(ctx.annotated_path(config.pack_id))
    pack = annotated.pack

    # 1) 概念覆盖率：预埋概念 100% 命中
    surfaces = {normalize_term(c.name) for c in pack.concepts} | {
        normalize_term(a) for c in pack.concepts for a in c.aliases
    }
    for seeded in SEEDED_CONCEPTS:
        keys = {normalize_term(seeded["name"]), *{normalize_term(a) for a in seeded["aliases"]}}
        assert keys & surfaces, f"预埋概念缺失：{seeded['name']}"

    # 2) 层级与预埋一致
    parent_by_id = {c.id: c.parent_id for c in pack.concepts}
    for child, parent in SEEDED_PARENTS.items():
        assert parent_by_id[child] == parent

    # 3) 关系全覆盖
    got = {(r.subject, r.predicate, r.object) for r in pack.relations}
    assert set(SEEDED_RELATIONS) <= got

    # 4) closed_values
    supplier_risk = next(c for c in pack.concepts if c.id == "SupplierRisk")
    assert supplier_risk.closed_values == SEEDED_CLOSED_VALUES["SupplierRisk"]

    # 5) 铁律约束：output target + minLength 120 + mode log + 证据
    iron = [c for c in pack.constraints if c.target.kind == "output"]
    assert iron, "缺少输出约束"
    law = iron[0]
    assert law.schema_ == {"type": "string", "minLength": 120}
    assert law.mode == "log"
    assert law.concept_id == "RiskReport"

    # 6) 工作流：触发词、输出标签、评审级别
    assert pack.workflows, "缺少工作流"
    workflow = pack.workflows[0]
    assert "供应商风险" in workflow.triggers
    assert workflow.output_tags == ["procurement_risk_summary"]
    assert workflow.review_level == "checkpoint"
    assert any(tag == "ontology:SupplierRisk" for t in workflow.asset_triggers for tag in t.tags_any)

    # 7) 元素证据完备（.parent 空父断言除外）
    for path, evidences in annotated.evidence.items():
        if path.endswith(".parent"):
            continue
        assert evidences, f"元素 {path} 缺少证据引用"

    # 8) 导出 JSON 干净 + parity 有效
    paths = export_all(annotated, ctx.dist_dir)
    exported = json.loads(paths["domain_pack"].read_text(encoding="utf-8"))
    _, parity = DomainPackValidator().validate(exported)
    assert parity.valid, parity.errors
    # 无内部字段泄漏
    assert all("evidence" not in c for c in exported["concepts"])
    assert paths["spec"].exists() and paths["provenance"].exists()


def test_golden_pipeline_deterministic(tmp_path):
    """ScriptedLLM 确定性：两次运行产物一致。"""
    from team_ontology.engine.provenance import load_annotated

    ctx_a, config_a, _ = run_golden(tmp_path / "a")
    ctx_b, config_b, _ = run_golden(tmp_path / "b")
    pack_a = load_annotated(ctx_a.annotated_path(config_a.pack_id)).clean_pack_dict()
    pack_b = load_annotated(ctx_b.annotated_path(config_b.pack_id)).clean_pack_dict()
    assert pack_a == pack_b
