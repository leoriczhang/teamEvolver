"""扩展校验器测试：别名冲突、closed_values、资产标签、触发词碰撞、覆盖率。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from team_ontology.engine.schemas import OntologyPackDocument
from team_ontology.engine.validation.extended import ExtendedValidator, normalize_term

FIXTURES = Path(__file__).parent / "fixtures"


def load_document() -> OntologyPackDocument:
    payload = json.loads((FIXTURES / "pack_valid_quickstart.json").read_text(encoding="utf-8"))
    return OntologyPackDocument.model_validate(payload)


def test_normalize_term():
    assert normalize_term("Supplier Risk") == normalize_term("supplierrisk")
    assert normalize_term("供应商风险") == "供应商风险"
    assert normalize_term(" 采购 风险 ") == "采购风险"


def test_alias_conflict_detected():
    document = load_document()
    document.concepts[0].aliases = ["供应商风险"]  # 与 SupplierRisk.name 冲突
    report = ExtendedValidator().validate(document)
    assert report.valid
    assert any("词面冲突" in w.message for w in report.warnings)


def test_closed_values_duplicate():
    document = load_document()
    document.concepts[1].closed_values = ["低", "中", "低"]
    report = ExtendedValidator().validate(document)
    assert report.valid
    assert any("受控取值重复" in w.message for w in report.warnings)


def test_closed_values_blank_is_error():
    document = load_document()
    document.concepts[1].closed_values = [" "]
    report = ExtendedValidator().validate(document)
    assert not report.valid
    assert any("空白受控取值" in e.message for e in report.errors)


def test_asset_tag_unknown_concept_is_error():
    document = load_document()
    document.workflows[0].asset_triggers[0].tags_any = ["ontology:Ghost"]
    report = ExtendedValidator().validate(document)
    assert not report.valid
    assert any("未定义的概念" in e.message for e in report.errors)


def test_asset_tag_without_prefix_warns():
    document = load_document()
    document.workflows[0].asset_triggers[0].tags_any = ["plain_tag"]
    report = ExtendedValidator().validate(document)
    assert report.valid
    assert any("不会作为受控本体标签生效" in w.message for w in report.warnings)


def test_trigger_collision_across_workflows():
    document = load_document()
    extra = copy.deepcopy(document.workflows[0])
    extra.id = "wf2"
    extra.name = "另一个工作流"
    extra.triggers = ["供应商风险"]
    extra.asset_triggers = []
    document.workflows.append(extra)
    report = ExtendedValidator().validate(document)
    assert any("被多个工作流使用" in w.message for w in report.warnings)


def test_trigger_substring_warns():
    document = load_document()
    document.workflows[0].triggers = ["供应商风险", "供应商"]
    report = ExtendedValidator().validate(document)
    assert any("互为子串" in w.message for w in report.warnings)


def test_orphan_concept_info():
    document = load_document()
    document.concepts[1].parent_id = "ProcurementRequest"  # 仍被关系和标签引用
    report = ExtendedValidator().validate(document)
    # 清理引用后 ProcurementRequest 变孤立
    document.relations = []
    document.workflows = []
    document.constraints = []
    report = ExtendedValidator().validate(document)
    assert any("未被任何关系/约束/资产标签引用" in i.message for i in report.infos)


def test_coverage_report():
    document = load_document()
    stats = {"供应商风险": 10.0, "采购申请": 8.0, "未覆盖术语甲": 5.0, "未覆盖术语乙": 3.0}
    report = ExtendedValidator(term_stats=stats).validate(document)
    coverage_infos = [i.message for i in report.infos if i.path == "coverage"]
    assert any("覆盖率 50.0%" in m for m in coverage_infos)
    assert any("未覆盖术语：'未覆盖术语甲'" in m for m in coverage_infos)
