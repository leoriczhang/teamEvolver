#!/usr/bin/env python3
"""Regression tests for form-friendly human knowledge supplementation."""

from __future__ import annotations

import tempfile
import json
import threading
import time
import unittest
from pathlib import Path

import human_checkpoints as hc


class SemanticGapQuestionTests(unittest.TestCase):
    def test_checkpoint_asks_for_missing_rules_not_unit_approval(self) -> None:
        report = """
## [结构化缺口清单]

| 缺口ID | 缺口描述 | 影响的单元/结构(规范名) | 严重度(高/中/低) | 需跨批次验证(是/否) | 补全来源线索 |
|---|---|---|---|---|---|
| GAP-01 | 版本切换的判定标准缺失：两版规则并存，未说明判定字段与过渡期 | U-01、U-05 | 高 | 否 | 规则变更通知 |
| GAP-02 | 货品库存差异率计算公式缺失：未给出货品口径和触发阈值 | U-08 | 低 | 否 | 库存合同条款 |
| GAP-03 | 咨询工单与投诉工单的选择规则缺失：何时使用哪一种未明确 | U-07 | 中 | 否 | 商家操作指南 |
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "report.md").write_text(report, encoding="utf-8")
            questions, total = hc.extract_gap_questions_from_semantic_reports(root)

        self.assertEqual(total, 3)
        self.assertEqual([item["qid"] for item in questions], ["gap-01", "gap-03", "gap-02"])
        all_prompts = "\n".join(item["question"] for item in questions)
        self.assertNotIn("是否准确并应保留", all_prompts)
        self.assertNotIn("是否应保留", all_prompts)
        self.assertIn("业务字段", questions[0]["question"])

        formula = next(item for item in questions if item["qid"] == "gap-02")
        self.assertEqual(formula["field_label"], "准确公式与阈值")
        self.assertIn("分子、分母", formula["question"])
        self.assertIn("单位", formula["placeholder"])

        selector = next(item for item in questions if item["qid"] == "gap-03")
        self.assertEqual(selector["field_label"], "选择条件与优先级")
        self.assertIn("分别在什么条件下选择哪一种", selector["question"])

    def test_answers_are_serialized_as_authoritative_compiler_context(self) -> None:
        questions = [{
            "qid": "gap-07",
            "dimension": "关键知识缺口 GAP-07 · U-08",
            "question": "请填写货品库存差异率的准确公式和阈值？",
        }]
        context = hc.format_qa_context(
            "【使用者对关键知识缺口的补充】",
            questions,
            {"gap-07": "差异率=(盘亏-盘盈)/期末库存；季度统计；阈值0.03%"},
        )
        self.assertIn("关键知识缺口 GAP-07", context)
        self.assertIn("阈值0.03%", context)
        self.assertIn("权威领域知识", context)

    def test_checkpoint_persists_auditable_history_after_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = hc.FileCheckpointClient(root, enabled=True)
            result: dict[str, object] = {}

            def ask() -> None:
                result["value"] = client.ask(
                    "after_semantic", 2, "第 2 轮补证", "请补充", [{
                        "qid": "gap-02", "question": "请填写阈值？",
                    }],
                )

            thread = threading.Thread(target=ask)
            thread.start()
            pending_path = root / "pending.json"
            for _ in range(30):
                if pending_path.exists():
                    break
                time.sleep(0.05)
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            (root / "answer.json").write_text(json.dumps({
                "question_id": pending["id"], "answers": {"gap-02": "48 小时"},
            }, ensure_ascii=False), encoding="utf-8")
            thread.join(timeout=3)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result["value"], ({"gap-02": "48 小时"}, False))
            history = json.loads(next((root / "history").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(history["round"], 2)
            self.assertEqual(history["answers"], {"gap-02": "48 小时"})


if __name__ == "__main__":
    unittest.main()
