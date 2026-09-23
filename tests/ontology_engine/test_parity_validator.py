"""parity 校验器测试：与 HugAgentOS DomainPackValidator 行为一致。

夹具 ``pack_valid_quickstart.json`` 取自 HugAgentOS 官方
``document/zh-CN/getting-started/domain-ontology-quickstart.md`` 的示例。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from team_ontology.engine.schemas import OntologyPackDocument
from team_ontology.engine.validation.hugagentos_parity import (
    DomainPackValidator,
    tool_input_schema,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_pack() -> dict:
    return json.loads((FIXTURES / "pack_valid_quickstart.json").read_text(encoding="utf-8"))


def load_tool(name: str) -> dict:
    return json.loads((FIXTURES / "tools" / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 有效输入
# ---------------------------------------------------------------------------


def test_quickstart_pack_valid_without_tools():
    document, report = DomainPackValidator().validate(load_pack())
    assert report.as_dict() == {
        "valid": True,
        "errors": [],
        "warnings": [],
        "infos": [],
    }
    assert document is not None
    assert document.pack_id == "procurement_risk"
    assert document.schema_version == "1.0"


def test_pack_with_tool_parameter_constraint_valid():
    pack = load_pack()
    pack["constraints"].append(
        {
            "id": "supplier_name_required",
            "name": "供应商名称必填",
            "target": {"kind": "tool_parameter", "tool": "supplier_check", "parameter": "supplier_name"},
            "schema": {"type": "string", "minLength": 2},
            "mode": "log",
            "risk": "low",
            "message": "必须提供供应商名称。",
            "suggestion": "补充供应商名称后重试。",
            "enabled": True,
        }
    )
    document, report = DomainPackValidator().validate(
        pack, tool_schemas={"supplier_check": load_tool("supplier_check.json")}
    )
    assert report.valid, report.errors
    assert document is not None


def test_allow_unresolved_tools_downgrades_to_warning():
    pack = load_pack()
    pack["constraints"].append(
        {
            "id": "unknown_tool_constraint",
            "name": "引用未知工具",
            "target": {"kind": "tool", "tool": "ghost_tool"},
            "schema": {"type": "string"},
            "mode": "log",
            "risk": "low",
            "message": "x",
            "suggestion": "",
            "enabled": True,
        }
    )
    pack["config"]["allow_unresolved_tools"] = True
    _, report = DomainPackValidator().validate(pack)
    assert report.valid
    assert not report.errors
    assert any("ghost_tool" in w.message for w in report.warnings)


# ---------------------------------------------------------------------------
# 无效输入 — pydantic 结构层
# ---------------------------------------------------------------------------


def _expect_error(payload: dict, fragment: str, path_prefix: str | None = None):
    _, report = DomainPackValidator().validate(payload)
    assert not report.valid
    matching = [e for e in report.errors if fragment in e.message]
    assert matching, f"未找到包含 {fragment!r} 的错误，实际：{[e.message for e in report.errors]}"
    if path_prefix is not None:
        assert any(e.path.startswith(path_prefix) for e in matching)
    return report


def test_dangling_parent_reference():
    pack = load_pack()
    pack["concepts"][1]["parent_id"] = "Ghost"
    _expect_error(pack, "unknown parent Ghost")


def test_parent_cycle_rejected():
    pack = load_pack()
    pack["concepts"][0]["parent_id"] = "SupplierRisk"
    pack["concepts"][1]["parent_id"] = "ProcurementRequest"
    _expect_error(pack, "cycle", "pack")


def test_relation_unknown_endpoint():
    pack = load_pack()
    pack["relations"][0]["object"] = "Ghost"
    _expect_error(pack, "unknown concept Ghost")


def test_constraint_unknown_concept():
    pack = load_pack()
    pack["constraints"][0]["concept_id"] = "Ghost"
    _expect_error(pack, "unknown concept Ghost")


def test_duplicate_concept_ids():
    pack = load_pack()
    pack["concepts"].append(copy.deepcopy(pack["concepts"][0]))
    _expect_error(pack, "duplicate concept ids")


def test_duplicate_relation_ids():
    pack = load_pack()
    pack["relations"].append(copy.deepcopy(pack["relations"][0]))
    _expect_error(pack, "duplicate relation ids")


def test_invalid_id_pattern():
    pack = load_pack()
    pack["concepts"][0]["id"] = "1bad-id!"
    _expect_error(pack, "pattern")


def test_invalid_pack_id_pattern():
    pack = load_pack()
    pack["pack_id"] = "BadPackId"
    _expect_error(pack, "pattern")


def test_cardinality_min_exceeds_max():
    pack = load_pack()
    pack["relations"][0]["min_cardinality"] = 5
    pack["relations"][0]["max_cardinality"] = 2
    _expect_error(pack, "min_cardinality cannot exceed max_cardinality")


def test_extra_field_forbidden():
    pack = load_pack()
    pack["concepts"][0]["extra_junk"] = "x"
    _expect_error(pack, "Extra inputs")


def test_workflow_requires_activation():
    pack = load_pack()
    pack["workflows"][0]["triggers"] = []
    pack["workflows"][0]["asset_triggers"] = []
    _expect_error(pack, "requires text triggers or asset_triggers")


def test_target_kind_requires_tool():
    pack = load_pack()
    pack["constraints"][0]["target"] = {"kind": "tool"}
    _expect_error(pack, "tool target requires tool")


def test_output_target_requires_tag():
    pack = load_pack()
    pack["constraints"][0]["target"] = {"kind": "output"}
    _expect_error(pack, "output target requires output_tag")


# ---------------------------------------------------------------------------
# 无效输入 — JSON Schema 与工具引用层
# ---------------------------------------------------------------------------


def test_invalid_json_schema_rejected():
    pack = load_pack()
    pack["constraints"][0]["schema"] = {"type": "not_a_type"}
    _expect_error(pack, "", "constraints.0.schema")


def test_unknown_tool_reference_is_error():
    pack = load_pack()
    pack["workflows"][0]["required_tools"] = ["ghost_tool"]
    _expect_error(pack, "unknown tool reference: ghost_tool")


def test_unknown_parameter_rejected():
    pack = load_pack()
    pack["constraints"].append(
        {
            "id": "bad_param",
            "name": "参数不存在",
            "target": {
                "kind": "tool_parameter",
                "tool": "supplier_check",
                "parameter": "no_such_param",
            },
            "schema": {"type": "string"},
            "mode": "log",
            "risk": "low",
            "message": "x",
            "suggestion": "",
            "enabled": True,
        }
    )
    _, report = DomainPackValidator().validate(pack, tool_schemas={"supplier_check": load_tool("supplier_check.json")})
    assert not report.valid
    assert any("no_such_param" in e.message for e in report.errors)


def test_required_forbidden_overlap():
    pack = load_pack()
    pack["workflows"][0]["required_tools"] = ["t1"]
    pack["workflows"][0]["forbidden_tools"] = ["t1"]
    _expect_error(pack, "cannot be both required and forbidden")


def test_asset_trigger_unknown_tool():
    pack = load_pack()
    pack["workflows"][0]["asset_triggers"] = [{"kind": "tool", "ids": ["ghost_tool"]}]
    _expect_error(pack, "unknown tool reference: ghost_tool")


# ---------------------------------------------------------------------------
# 工具 schema 形态兼容
# ---------------------------------------------------------------------------


def test_tool_input_schema_compatibility():
    assert tool_input_schema(load_tool("supplier_check.json"))["type"] == "object"
    assert tool_input_schema({"function": {"parameters": {"type": "object"}}})["type"] == "object"
    assert tool_input_schema({"function": {"inputSchema": {"type": "object"}}})["type"] == "object"
    assert tool_input_schema({"parameters": {"type": "object"}})["type"] == "object"
    assert tool_input_schema({}) == {}


# ---------------------------------------------------------------------------
# 导出往返：模型 -> dict -> 再解析，且 dict 不含任何内部字段
# ---------------------------------------------------------------------------


def test_model_roundtrip_preserves_pack():
    document = OntologyPackDocument.model_validate(load_pack())
    again = OntologyPackDocument.model_validate(document.model_dump(by_alias=True))
    assert again == document


def test_model_dump_has_no_internal_fields():
    document = OntologyPackDocument.model_validate(load_pack())
    dumped = document.model_dump(by_alias=True)
    assert "schema_" not in dumped
    assert dumped["constraints"][0]["schema"] == {"type": "string", "minLength": 120}
