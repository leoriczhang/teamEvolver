"""Source ambiguity and authored ordering stay inside the versioned Skill script."""
import importlib.util

import pytest

from tests.miner.skill_miner.support import COMPILER_ROOT
from tests.miner.skill_miner.support.knowledge_skill_preparation import Workspace, table

SCRIPT = COMPILER_ROOT / "compiler_skills/knowledge-point-v8/scripts/prepare_point.py"
SPEC = importlib.util.spec_from_file_location("source_integrity_v8", SCRIPT)
PREP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREP)


@pytest.fixture
def documents(tmp_path):
    workspace = Workspace(tmp_path)
    files = {}
    for name, ids in workspace.ids_by_file.items():
        parts = PREP.sources(tmp_path, ids)
        text = PREP.join_ranges(list(parts.items()))
        files[name] = {"text": text, "uri": next(iter(parts.values()))["uri"],
                       "sha256": PREP.digest(text), "range_ids": ids}
    return files


def test_duplicate_table_ids_and_headers_cannot_silently_overwrite_members():
    with pytest.raises(ValueError, match="Duplicate original table record ID"):
        PREP.rows(table(["id", "name"], [["1", "甲"], ["1", "乙"]]), "original.md")
    with pytest.raises(ValueError, match="Duplicate original table column"):
        PREP.rows(table(["id", "name", "name"], [["1", "甲", "乙"]]), "original.md")


def test_ambiguous_names_cannot_silently_change_multi_point_membership(documents):
    documents["知识点.md"]["text"] = documents["知识点.md"]["text"].replace("| 2 | 甲乙 |", "| 2 | 甲 |")
    with pytest.raises(ValueError, match="Ambiguous knowledge-point titles"):
        PREP.select_point(documents, "模拟教官系统提示词.md", "1")


def test_explicit_numeric_task_order_overrides_export_row_order_without_changing_fields(documents):
    documents["对练任务.md"]["text"] = table(
        ["id", "任务标题", "任务描述（AI）", "任务话术提醒（AI）", "任务排序", "所属知识点"],
        [["11", "第二任务", "原文第二", "提醒", "2", "甲"],
         ["12", "第一任务", "原文第一", "提醒", "1", "甲"]],
    )
    config = PREP.select_point(documents, "模拟教官系统提示词.md", "1")
    assert [t["id"] for t in config["tasks"]] == ["12", "11"]
    assert config["tasks"][0]["source_fields"]["任务排序"] == "1"
    assert not config["source_issues"]


@pytest.mark.parametrize("order", ["", "NaN", "Infinity", "同上", "1"])
def test_ambiguous_task_order_is_reported_without_inventing_priority(documents, order):
    documents["对练任务.md"]["text"] = table(
        ["id", "任务标题", "任务描述（AI）", "任务话术提醒（AI）", "任务排序", "所属知识点"],
        [["11", "任务甲", "原文甲", "提醒", order, "甲"], ["12", "任务乙", "原文乙", "提醒", "1", "甲"]],
    )
    config = PREP.select_point(documents, "模拟教官系统提示词.md", "1")
    assert [t["id"] for t in config["tasks"]] == ["11", "12"]
    assert [i["kind"] for i in config["source_issues"]] == ["ambiguous_task_order"]


def test_malformed_optional_persona_is_preserved_and_exposed_as_an_explicit_gap(documents):
    text = table(["id", "客户key", "客户姓名"], [["1", "coach", "姓名|额外列"]])
    documents["对练用户.md"]["text"] = text
    config = PREP.select_point(documents, "模拟教官系统提示词.md", "1")
    assert config["coach_source_rows"][0]["fields"] is None
    assert config["coach_source_rows"][0]["raw_record"] in text
    issue = config["source_issues"][0]
    assert issue["kind"] == "ambiguous_table_row" and issue["source"]["record_id"] == "1"
    assert [t["id"] for t in config["tasks"]] == ["11"]
