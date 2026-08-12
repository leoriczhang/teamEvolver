from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path


SKILLMINER_ROOT = Path(__file__).resolve().parents[1] / "teamEvolver" / "skillminer"
if str(SKILLMINER_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLMINER_ROOT))

import human_checkpoints as hc  # noqa: E402


def test_semantic_checkpoint_uses_structured_gaps_not_unit_approval(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text(
        """## [结构化缺口清单]
| 缺口ID | 缺口描述 | 影响的单元/结构 | 严重度 | 需跨批次验证 | 补全来源线索 |
|---|---|---|---|---|---|
| GAP-01 | 库存差异率计算公式缺失：未说明统计口径和触发阈值 | U-08 | 高 | 否 | 库存合同 |
""",
        encoding="utf-8",
    )
    questions, total = hc.extract_gap_questions_from_semantic_reports(tmp_path)

    assert total == 1
    assert questions[0]["qid"] == "gap-01"
    assert questions[0]["field_label"] == "准确公式与阈值"
    assert "分子、分母" in questions[0]["question"]
    assert "是否准确并应保留" not in questions[0]["question"]


def test_file_checkpoint_client_blocks_until_matching_form_answer(tmp_path: Path) -> None:
    client = hc.FileCheckpointClient(tmp_path, enabled=True)
    result: dict[str, object] = {}

    def ask() -> None:
        answers, stopped = client.ask(
            "on_gap_low_confidence",
            1,
            "补充指标",
            "逐条填写",
            [{
                "qid": "g1",
                "question": "请问，升级时效指标是多少？",
                "field_label": "指标值（请包含单位与适用条件）",
                "answer_type": "short_text",
            }],
        )
        result["answers"] = answers
        result["stopped"] = stopped

    thread = threading.Thread(target=ask)
    thread.start()
    pending_path = tmp_path / "pending.json"
    deadline = time.monotonic() + 3
    while not pending_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    (tmp_path / "answer.json").write_text(
        json.dumps({
            "question_id": pending["id"],
            "answers": {"g1": "48 小时；普通订单"},
            "stop": False,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result == {
        "answers": {"g1": "48 小时；普通订单"},
        "stopped": False,
    }
    assert not pending_path.exists()
    assert not (tmp_path / "answer.json").exists()
