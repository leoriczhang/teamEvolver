# ruff: noqa: F401
"""Generated role-name omissions are source-bound projections, never rewrites of Native evidence."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.case_corrections import corrected_case, omit_foreign_case_scope, omit_unsupported_query_name
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _sha
from tests.miner.skill_miner.fixtures import build_document


def fixture(root):
    chunk = planning.source_chunks("客户别名zhanglei，未命名的规划师询问客户已准备的费用。", "viking://original/dialogue.md")[0]
    chunk["ordinal"] = 0
    _save(root / "chunks/00000000.json", chunk)
    _save(root / "chunk-ids" / (chunk["id"] + ".json"), {"ordinal": 0})
    value = build_document("collect-finance", [{"query": "你是规划师张磊，请收集客户张先生的财务信息。"}])["cases"][0]
    value["case_id"] = "collect-finance-case-01"
    value["provenance"]["session_ids"] = [chunk["source_uri"]]
    _save(root / "cases/collect-finance/00.json", value)
    context = {"case_id": value["case_id"], "input_ids": [chunk["id"]], "source_refs": [chunk["source_uri"]]}
    _save(root / "case-batches/collect-finance/outcomes/accepted-00.json",
          {"status": "valid", "context": context, "case": value})
    return value, chunk

def correct(root, value):
    return omit_unsupported_query_name(
        root, "collect-finance", 0, expected_case_sha256=_sha(_json(value)),
        start=value["query"].index("张磊"), text="张磊",
        explanation="已核对当前原文：客户别名不能变成未命名规划师的姓名，保持角色称谓。",
    )

def scope_fixture(root):
    value, primary = fixture(root)
    secondary = planning.source_chunks("另一个客户家庭的排序：子女教育第一，医疗第二。", "viking://original/other.md")[0]
    secondary["ordinal"] = 1
    _save(root / "chunks/00000001.json", secondary)
    _save(root / "chunk-ids" / (secondary["id"] + ".json"), {"ordinal": 1})
    value["query"] = "请与退休客户沟通。\n1. 分析医疗费用。\n2. 子女教育第一，医疗第二。\n3. 评估已有保额。"
    value["checks"] += [{"id": "foreign-order", "kind": "output", "text": "子女教育第一，医疗第二。"}]
    value["checks"][-2]["text"] += "随后排序。"
    value["provenance"]["session_ids"].append(secondary["source_uri"])
    _save(root / "cases/collect-finance/00.json", value)
    context = {"case_id": value["case_id"], "input_ids": [primary["id"], secondary["id"]],
               "method_input_ids": [primary["id"]], "context_input_ids": [secondary["id"]],
               "source_refs": value["provenance"]["session_ids"]}
    _save(root / "case-batches/collect-finance/outcomes/accepted-00.json",
          {"status": "valid", "context": context, "case": value})
    text = "2. 子女教育第一，医疗第二。\n"
    trajectory = value["checks"][-2]
    kwargs = {
        "expected_case_sha256": _sha(_json(value)),
        "foreign_context_claim": {"input_id": secondary["id"], "quote": "子女教育第一，医疗第二。"},
        "query_omissions": [{"start": value["query"].index(text), "text": text}],
        "omitted_checks": [value["checks"][-1]],
        "check_text_omissions": [{"id": trajectory["id"], "omissions": [
            {"start": trajectory["text"].index("随后排序。"), "text": "随后排序。"},
        ]}],
        "explanation": "人工对照两个原始情境：排序来自另一个客户，不能套到退休客户；仅删除越界内容。",
    }
    return value, primary, kwargs
