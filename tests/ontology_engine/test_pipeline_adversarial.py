"""对抗测试：LLM 输出语义错误/恶意结构时，确定性门禁必须保证产物可导入。

真实场景中 response_format=json_schema 已约束响应形状，残余风险是语义错误
（悬空引用/环/冲突/未知工具/超长字段）。本测试用 RawLLM 绕过形状校验，
验证流水线的确定性清洗 + parity 校验 + 修复循环能全部拦截。
"""

from __future__ import annotations

import json
from pathlib import Path

from team_ontology.engine.config import ProjectConfig, Thresholds
from team_ontology.engine.llm.client import LLMError
from team_ontology.engine.pipeline.context import ProjectContext
from team_ontology.engine.pipeline.ingest import run_ingest
from team_ontology.engine.pipeline.runner import ALL_STAGES, PipelineRunner
from team_ontology.engine.validation.hugagentos_parity import DomainPackValidator

FIXTURES = Path(__file__).parent / "fixtures"

# 与黄金测试相同的语料，但概念响应由 RawLLM 恶意构造


class RawLLM:
    """不做任何响应形状校验的 LLM 替身：模拟最恶劣的模型输出。"""

    model = "raw-adversarial"

    def __init__(self, handler):
        self._handler = handler

    def complete_json(self, *, system, user, json_schema, prompt_version, max_retries=3):
        return self._handler(prompt_version, user)


def _evil_responder(version: str, user: str) -> dict:
    """语义恶意响应：结构形状合法（过 schema），内容处处踩线。"""
    if version == "terms-v1":
        return {
            "concepts": [
                {
                    "id": "1bad-id!",  # 非法 id 字符 + 数字开头
                    "name": "",
                    "aliases": ["采购申请", "采购申请"],  # 重复别名
                    "definition": "",
                    "risk": "critical",  # 非法风险
                    "source_terms": ["采购申请"],
                },
                {
                    "id": "SupplierRisk",
                    "name": "供应商风险" * 50,  # 超长（>255）
                    "aliases": ["供应商风险"],  # 与名称相同的别名
                    "definition": "风险定义",
                    "risk": "medium",
                    "source_terms": ["供应商风险"],
                },
            ],
            "dropped": [],
        }
    if version == "taxonomy-v1":
        return {
            "edges": [
                {"child": "Concept", "parent": "SupplierRisk"},
                {"child": "SupplierRisk", "parent": "Concept"},  # 环
                {"child": "Ghost", "parent": "Concept"},  # 未知概念
            ]
        }
    if version == "relations-v2":
        return {
            "relations": [
                {
                    "id": "rel_1",
                    "subject": "Ghost",  # 悬空端点
                    "predicate": "包含",
                    "object": "SupplierRisk",
                    "description": "",
                    "min_cardinality": None,
                    "max_cardinality": None,
                    "forbidden": False,
                },
                {
                    "id": "rel_2",
                    "subject": "Concept",
                    "predicate": "包含" * 80,  # 超长谓词（>128）
                    "object": "SupplierRisk",
                    "description": "",
                    "min_cardinality": 9,  # min > max
                    "max_cardinality": 2,
                    "forbidden": False,
                },
                {
                    "id": "rel_2",  # 重复 id
                    "subject": "Concept",
                    "predicate": "包含",
                    "object": "SupplierRisk",
                    "description": "",
                    "min_cardinality": None,
                    "max_cardinality": None,
                    "forbidden": False,
                },
            ]
        }
    if version == "closed-values-v2":
        return {
            "entries": [
                {"concept_id": "SupplierRisk", "values": ["低", "低", "高"]},  # 重复取值
            ]
        }
    if version == "iron-laws-v3":
        return {
            "constraints": [
                {
                    "id": "bad_law",
                    "name": "x",
                    "output_tag": "",
                    "schema": {"type": "not_a_type"},  # 非法 JSON Schema
                    "concept_id": "Ghost",  # 悬空概念
                    "requires_citations": False,
                    "risk": "low",
                    "message": "m",
                    "suggestion": "",
                },
                {
                    "id": "law2",
                    "name": "x",
                    "output_tag": "summary_tag",
                    "schema": {"type": "string"},
                    "concept_id": "SupplierRisk",
                    "requires_citations": False,
                    "risk": "low",
                    "message": "超长" * 100,
                    "suggestion": "",
                },
            ]
        }
    if version == "workflows-v4":
        return {
            "workflows": [
                {
                    "id": "wf_bad",
                    "name": "x",
                    "triggers": [""],  # 空触发词 → 整体丢弃
                    "required_tools": ["ghost_tool"],  # 未知工具
                    "forbidden_tools": [],
                    "output_tags": [],
                    "review_level": "none",
                    "risk": "low",
                    "concept_ids": [],
                },
                {
                    "id": "wf_ok",
                    "name": "供应商风险工作流",
                    "triggers": ["供应商风险"],
                    "required_tools": [],
                    "forbidden_tools": [],
                    "output_tags": ["summary_tag"],
                    "review_level": "checkpoint",
                    "risk": "medium",
                    "concept_ids": ["SupplierRisk"],
                },
            ]
        }
    raise AssertionError(f"unexpected version: {version}")


def _make_context(tmp_path: Path) -> tuple[ProjectContext, ProjectConfig]:
    config = ProjectConfig(
        pack_id="adversarial",
        pack_name="对抗包",
        domain="procurement",
        languages=["zh"],
        thresholds=Thresholds(min_term_frequency=1, term_candidates=120),
    )
    return ProjectContext.create(config, root=tmp_path / ".ontology-enhancer"), config


def test_adversarial_llm_cannot_corrupt_pack(tmp_path):
    ctx, config = _make_context(tmp_path)
    run_ingest(ctx, corpus_path=FIXTURES / "corpus_zh", config=config)
    report = PipelineRunner(ctx, RawLLM(_evil_responder)).run(config, stages=ALL_STAGES, force=True, repair_rounds=2)
    assert report["valid"], json.dumps(report["errors"], ensure_ascii=False)

    from team_ontology.engine.provenance import load_annotated

    annotated = load_annotated(ctx.annotated_path(config.pack_id))
    pack = annotated.pack

    # 概念全部合法
    for concept in pack.concepts:
        assert concept.id[0].isalpha(), concept.id
        assert concept.name, "名称不得为空"
        assert concept.definition, "定义不得为空"
        assert concept.risk in {"low", "medium", "high"}
        assert len(set(concept.aliases)) == len(concept.aliases)

    # 无环：沿 parent 链走到头
    parent = {c.id: c.parent_id for c in pack.concepts}
    for cid in parent:
        seen = set()
        current = cid
        while current:
            assert current not in seen, "parent 环未被拦截"
            seen.add(current)
            current = parent.get(current)

    # 关系端点全部存在、谓词合法、基数合法、id 唯一
    ids = [r.id for r in pack.relations]
    assert len(ids) == len(set(ids))
    for relation in pack.relations:
        assert relation.subject in parent and relation.object in parent
        assert 1 <= len(relation.predicate) <= 128
        if relation.min_cardinality is not None and relation.max_cardinality is not None:
            assert relation.min_cardinality <= relation.max_cardinality

    # 约束合法且引用存在
    for constraint in pack.constraints:
        if constraint.concept_id:
            assert constraint.concept_id in parent
        assert constraint.target.output_tag or constraint.target.tool
        assert len(constraint.message) <= 2000

    # 工作流：空触发词工作流被丢弃，未知工具被剔除，标签引用存在
    for workflow in pack.workflows:
        assert workflow.triggers
        for trigger in workflow.asset_triggers:
            for tag in trigger.tags_any:
                assert tag.startswith("ontology:")
        assert workflow.id != "wf_bad"

    # 导出后 parity 依然有效
    _, parity = DomainPackValidator().validate(annotated.clean_pack_dict())
    assert parity.valid, parity.errors


def test_repair_loop_reruns_failed_stage(tmp_path):
    """直接注入坏工件（绕过阶段防御）→ parity 错误 → 修复轮重跑 constraints 并收敛。"""
    ctx, config = _make_context(tmp_path)
    run_ingest(ctx, corpus_path=FIXTURES / "corpus_zh", config=config)

    # 注入一条 name 超长（>255）的约束工件，阶段自身的截断防御无法兜底
    ctx.write_jsonl(
        "concepts",
        [
            {
                "concept": {
                    "id": "MinimalConcept",
                    "name": "最小概念",
                    "aliases": [],
                    "definition": "占位概念。",
                    "risk": "low",
                },
                "source_terms": [],
                "evidence": [],
            }
        ],
    )
    ctx.write_jsonl(
        "iron_law_constraints",
        [
            {
                "constraint": {
                    "id": "bad_law",
                    "name": "长" * 300,
                    "target": {"kind": "output", "output_tag": "t"},
                    "schema": {"type": "string"},
                    "concept_id": None,
                    "requires_citations": False,
                    "prerequisite_tools": [],
                    "mode": "log",
                    "risk": "low",
                    "message": "m",
                    "suggestion": "",
                    "enabled": True,
                },
                "evidence": [],
            }
        ],
    )

    calls = {"iron": 0}

    def responder(version: str, user: str) -> dict:
        if version == "closed-values-v2":
            return {"entries": []}
        if version == "iron-laws-v3":
            calls["iron"] += 1
            return {"constraints": []}  # 修复：清空坏约束
        if version == "workflows-v4":
            return {"workflows": []}
        raise AssertionError(f"unexpected version: {version}")

    report = PipelineRunner(ctx, RawLLM(responder)).run(config, stages=("constraints",), force=False, repair_rounds=2)
    assert report["valid"], json.dumps(report["errors"], ensure_ascii=False)
    assert calls["iron"] >= 1, "修复轮应重跑 constraints 阶段"
    runs = ctx.read_jsonl("runs")
    assert sum(1 for r in runs if r["stage"] == "constraints") >= 1
    from team_ontology.engine.provenance import load_annotated

    annotated = load_annotated(ctx.annotated_path(config.pack_id))
    assert annotated.pack.constraints == []


def test_llm_error_surfaces_to_caller(tmp_path):
    """LLM 反复失败时异常向上抛出而不是产出坏包。"""
    ctx, config = _make_context(tmp_path)
    run_ingest(ctx, corpus_path=FIXTURES / "corpus_zh", config=config)

    def failing(_version: str, _user: str) -> dict:
        raise LLMError("upstream down")

    runner = PipelineRunner(ctx, RawLLM(failing))
    try:
        runner.run(config, stages=ALL_STAGES, force=True)
    except LLMError:
        pass
    else:
        raise AssertionError("LLM 失败未向上抛出")
