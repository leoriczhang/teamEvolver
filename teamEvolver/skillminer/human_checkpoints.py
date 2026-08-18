"""Concrete, form-friendly questions and file IPC for human mining checkpoints."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

_SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2, "": 3}
_NUMERIC_HINTS = (
    "指标", "阈值", "金额", "比例", "费率", "时长", "时效", "天数", "小时",
    "次数", "数量", "上限", "下限", "区间", "分数", "权重", "百分比", "量化",
    "公式", "差异率", "准确数值", "倍数", "工作日", "自然日", "分钟",
)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _plain_text(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ：:；;")


def _question_from_gap(body: str) -> dict[str, str]:
    """Turn an artifact's diagnostic wording into one answerable question."""
    context = _plain_text(body)
    explicit = re.findall(r"[^。；;！？!?]*[？?]", context)
    if explicit:
        prompt = _plain_text(explicit[0]).rstrip("?？")
        if "请问" in prompt:
            prompt = prompt[prompt.index("请问"):]
        prompt += "？"
        if not prompt.startswith(("请问", "请确认", "请说明", "请填写")):
            prompt = f"请问，{prompt}"
    else:
        subject = re.split(r"[：:—–]|(?:证据不足)|(?:待补充)", context, maxsplit=1)[0]
        subject = re.sub(
            r"(?:仍然|目前|当前)?(?:缺失|未明确|不明确|不完整|未覆盖|不清晰|未知|待确认|存在冲突)$",
            "",
            subject,
        )
        subject = _plain_text(subject).replace('"', "").replace("“", "").replace("”", "")
        subject = subject[:90] or "该知识缺口"

        # These branches deliberately ask for missing operational knowledge, not
        # whether a mined semantic unit should be kept.  The wording also tells
        # the UI what kind of value the user needs to enter.
        if "公式" in context or "差异率" in context:
            prompt = (
                f"请填写“{subject}”的准确计算公式：分子、分母、统计口径、统计周期"
                "以及触发阈值分别是什么？"
            )
            field_label = "准确公式与阈值"
            placeholder = "例如：（盘亏金额－盘盈金额）÷ 期末库存金额 × 100%；单位：%；季度统计；阈值 0.03%"
            answer_type = "long_text"
        elif "组合" in context and any(word in context for word in ("状态", "场景", "条件")):
            prompt = (
                f"请补全“{subject}”：每种状态组合分别满足什么条件、得到什么处理或赔付结论？"
                "涉及比例、金额或时限时请填写准确数值。"
            )
            field_label = "各状态组合与对应结论"
            placeholder = "请按“条件组合 → 处理/赔付结论”逐项填写，并写明准确比例、金额或时限"
            answer_type = "long_text"
        elif "优先级" in context or "较重" in context:
            prompt = (
                f"请明确“{subject}”：各类型如何排序或比较，出现并列时采用什么处理规则？"
            )
            field_label = "优先级与并列规则"
            placeholder = "请填写完整排序、比较指标和并列时的处理方式"
            answer_type = "long_text"
        elif "版本" in context or "适用边界" in context or "冲突处理" in context:
            prompt = (
                f"请明确“{subject}”：应读取哪个业务字段、以什么时间点或条件选择规则版本，"
                "过渡期或规则冲突时如何处理？"
            )
            field_label = "适用边界与冲突规则"
            placeholder = "请填写判定字段、切换时间点/条件、过渡期规则和冲突时的优先级"
            answer_type = "long_text"
        elif "定义" in context or "适用范围" in context:
            prompt = f"请给出“{subject}”：包含哪些情形、不包含哪些情形，有哪些例外？"
            field_label = "完整定义与例外"
            placeholder = "请分别填写包含范围、排除范围和例外条件"
            answer_type = "long_text"
        elif any(word in context for word in ("流程", "机制", "步骤")):
            prompt = (
                f"请填写“{subject}”的标准流程：由谁处理、需要哪些证据、处理时限是多少，"
                "不同结论分别进入哪一步？"
            )
            field_label = "流程、角色与时限"
            placeholder = "请按步骤填写责任角色、所需证据、准确时限和结果分支"
            answer_type = "long_text"
        elif "选择规则" in context or "何时应选" in context:
            prompt = f"请明确“{subject}”：分别在什么条件下选择哪一种，多个条件同时满足时如何决定？"
            field_label = "选择条件与优先级"
            placeholder = "请分别填写每种选择的触发条件，以及条件冲突时的优先级"
            answer_type = "long_text"
        elif "处理规则" in context or "处理方式" in context:
            prompt = (
                f"请补全“{subject}”：需要读取哪些状态或条件，各种条件组合分别如何处理？"
                "涉及数值时请给出准确数字和单位。"
            )
            field_label = "条件组合与处理结论"
            placeholder = "请按“状态/条件 → 处理结论”逐项填写，并注明例外"
            answer_type = "long_text"
        elif any(word in context for word in ("判定", "标准", "条件", "边界")):
            prompt = (
                f"请明确“{subject}”：成立与不成立分别需要满足哪些条件和证据？"
            )
            field_label = "判定条件与证据"
            placeholder = "请分别填写成立/不成立的必要条件、证据要求及例外"
            answer_type = "long_text"
        elif any(hint in subject or hint in context for hint in _NUMERIC_HINTS):
            prompt = f"请填写“{subject}”的准确数值，并注明单位、统计口径、适用条件和例外。"
            field_label = "准确数值与单位"
            placeholder = "例如：48 小时；从首次受理时起算；仅适用于普通订单"
            answer_type = "short_text"
        else:
            prompt = f"请补全“{subject}”的明确业务规则：适用条件、处理结论和例外分别是什么？"
            field_label = "完整业务规则"
            placeholder = "请填写适用条件、明确结论和例外；如涉及数值请给出准确数字与单位"
            answer_type = "long_text"

    numeric = any(hint in prompt or hint in context for hint in _NUMERIC_HINTS)
    return {
        "question": prompt,
        "context": context,
        "field_label": locals().get(
            "field_label", "指标值（请包含单位与适用条件）" if numeric else "规则说明"
        ),
        "placeholder": locals().get(
            "placeholder",
            "例如：48 小时；适用于已完成揽收且非大促订单"
            if numeric else "请填写明确结论、适用条件和例外；如有依据可一并注明",
        ),
        "answer_type": locals().get("answer_type", "short_text" if numeric else "long_text"),
    }


def _markdown_cells(line: str) -> list[str]:
    """Split one simple Markdown table row while tolerating escaped pipes."""
    if not line.lstrip().startswith("|"):
        return []
    return [
        _plain_text(cell.replace(r"\|", "|"))
        for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def _semantic_gap_question(
    gap_id: str,
    description: str,
    affected: str = "",
    severity: str = "",
    cross_validate: str = "",
    source: str = "",
) -> dict[str, Any]:
    normalized = _question_from_gap(description)
    context_parts = [f"系统发现：{_plain_text(description)}"]
    if affected:
        context_parts.append(f"影响：{_plain_text(affected)}")
    if source:
        context_parts.append(f"建议核对：{_plain_text(source)}")
    if cross_validate == "是":
        context_parts.append("需要跨批次验证")
    normalized["context"] = "；".join(context_parts)
    return {
        "qid": _plain_text(gap_id).lower() or f"gap-{uuid.uuid4().hex[:8]}",
        "dimension": f"关键知识缺口 {_plain_text(gap_id)}" + (
            f" · {_plain_text(affected)}" if affected else ""
        ),
        "severity": severity if severity in _SEVERITY_ORDER else "",
        "source": _plain_text(source),
        "required": False,
        **normalized,
    }


def extract_gap_questions_from_semantic_reports(
    semantic_reports_dir: Path | str,
    *,
    limit: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    """Read structured semantic gaps and turn them into concrete supplement fields.

    Semantic discovery already diagnoses missing rules in its ``结构化缺口清单``.
    This function consumes those diagnoses directly, so the checkpoint asks for
    missing thresholds, formulas, decision rules and exceptions instead of asking
    the user to approve or reject candidate semantic units.
    """
    root = Path(semantic_reports_dir)
    paths = sorted(root.glob("*.md")) if root.is_dir() else []
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()

    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        report_questions: list[dict[str, Any]] = []
        for raw in text.splitlines():
            cells = _markdown_cells(raw)
            if len(cells) < 3 or not re.fullmatch(r"GAP[-_ ]?\d+", cells[0], re.IGNORECASE):
                continue
            gap_id = cells[0].upper().replace("_", "-").replace(" ", "-")
            description = cells[1]
            affected = cells[2] if len(cells) > 2 else ""
            severity = cells[3] if len(cells) > 3 else ""
            cross_validate = cells[4] if len(cells) > 4 else ""
            source = cells[5] if len(cells) > 5 else ""
            report_questions.append(_semantic_gap_question(
                gap_id, description, affected, severity, cross_validate, source
            ))

        # Older reports may have only ``### 缺口 N：标题`` sections.  Use
        # those as a fallback, but prefer the richer structured table above.
        if not report_questions:
            sections = re.finditer(
                r"^###\s*缺口\s*(\d+)\s*[：:]\s*([^\n]+)\n(.*?)(?=^###\s*缺口|^##\s|\Z)",
                text,
                flags=re.MULTILINE | re.DOTALL,
            )
            for match in sections:
                body = _plain_text(match.group(3))
                severity_match = re.search(r"严重度\s*[：:]\s*(高|中|低)", body)
                report_questions.append(_semantic_gap_question(
                    f"GAP-{int(match.group(1)):02d}",
                    f"{_plain_text(match.group(2))}：{body[:280]}",
                    severity=severity_match.group(1) if severity_match else "",
                ))

        for question in report_questions:
            dedupe_key = re.sub(r"\W+", "", question["question"])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            questions.append(question)

    questions.sort(key=lambda item: _SEVERITY_ORDER.get(item["severity"], 3))
    return questions[:max(0, limit)], len(questions)


def extract_gap_questions_from_skill(skill_md_path: Path | str | None) -> list[dict[str, Any]]:
    """Extract gaps from SKILL.md as concrete, single-answer form items."""
    if not skill_md_path or not Path(skill_md_path).is_file():
        return []
    text = Path(skill_md_path).read_text(encoding="utf-8", errors="ignore")
    questions: list[dict[str, Any]] = []
    dimension = ""
    seen: set[str] = set()
    dim_re = re.compile(r"^#{2,4}\s*(维度[一二三四五六七八九十百]+[：:][^\n]+)")
    gap_re = re.compile(r"[-*]\s*\**\s*(高|中|低)?严重度?\s*(缺口|冲突|存疑)")
    table_gap_re = re.compile(
        r"^\|\s*(GAP-\d+)[：:]\s*([^|]+)\|\s*([^|]+)\|\s*(高|中|低)\s*\|\s*([^|]+)\|"
    )
    for raw in text.splitlines():
        line = raw.strip()
        match = dim_re.match(line)
        if match:
            dimension = _plain_text(match.group(1))
            continue
        table_match = table_gap_re.match(line)
        if table_match:
            gap_id, topic, affected_dimension, severity, evidence_needed = table_match.groups()
            context = f"{_plain_text(topic)}；需要补充的依据：{_plain_text(evidence_needed)}"
            normalized = _question_from_gap(topic)
            normalized["context"] = context
            dedupe_key = normalized["question"][:80]
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            questions.append({
                "qid": gap_id.lower(),
                "dimension": _plain_text(affected_dimension),
                "severity": severity,
                "source": "",
                "required": False,
                **normalized,
            })
            continue
        if not (gap_re.search(line) or "冲突未解决" in line or "存疑" in line):
            continue
        if any(summary in line for summary in ("缺口统计", "严重度缺口集中", "高严重度缺口集中")):
            continue
        severity_match = re.search(r"(高|中|低)严重度", line)
        severity = severity_match.group(1) if severity_match else ""
        body = re.sub(r"^[-*]\s*", "", line)
        body = re.sub(
            r"\**\s*(高|中|低)?严重度?(缺口|冲突未解决|冲突|存疑)\**\s*[：:]?\s*",
            "",
            body,
        )
        source_match = re.search(r"[（(]来源[：:]([^）)]*)[）)]\s*$", body)
        source = _plain_text(source_match.group(1)) if source_match else ""
        body = re.sub(r"[（(]来源[：:][^）)]*[）)]\s*$", "", body).strip()
        if len(_plain_text(body)) < 6:
            continue
        normalized = _question_from_gap(body)
        dedupe_key = normalized["question"][:80]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        questions.append({
            "qid": f"g{len(questions) + 1}",
            "dimension": dimension,
            "severity": severity,
            "source": source,
            "required": False,
            **normalized,
        })
    questions.sort(key=lambda item: _SEVERITY_ORDER.get(item["severity"], 3))
    return questions


def format_qa_context(header: str, questions: list[dict[str, Any]], answers: dict[str, Any]) -> str:
    """Serialize form answers as authoritative evidence for the next model step."""
    lines = [f"\n{header}"]
    answered = 0
    for question in questions:
        answer = str(answers.get(question["qid"]) or "").strip()
        if not answer:
            continue
        answered += 1
        dimension = f"（{question['dimension']}）" if question.get("dimension") else ""
        lines.append(f"  {answered}. 问{dimension}：{question['question']}")
        lines.append(f"     使用者答：{answer}")
    if not answered:
        return ""
    lines.append("  请把上述使用者答案作为权威领域知识，写入对应维度并消解相应缺口。\n")
    return "\n".join(lines)


class FileCheckpointClient:
    """Block a worker process while the web service collects a form response."""

    def __init__(self, checkpoint_dir: Path | str | None, *, enabled: bool = False) -> None:
        self.root = Path(checkpoint_dir).resolve() if checkpoint_dir else None
        self.enabled = bool(enabled and self.root)
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def ask(
        self,
        checkpoint: str,
        round_idx: int,
        title: str,
        intro: str,
        questions: list[dict[str, Any]],
        *,
        allow_stop: bool = False,
    ) -> tuple[dict[str, str], bool]:
        if not self.enabled or not questions:
            return {}, False
        checkpoint_id = f"{checkpoint}-r{round_idx}-{uuid.uuid4().hex[:10]}"
        pending_path = self.root / "pending.json"
        answer_path = self.root / "answer.json"
        answer_path.unlink(missing_ok=True)
        payload = {
            "id": checkpoint_id,
            "checkpoint": checkpoint,
            "round": round_idx,
            "title": title,
            "intro": intro,
            "questions": questions,
            "allow_stop": allow_stop,
            "created_at": time.time(),
        }
        _write_json_atomic(pending_path, payload)
        print(f"HUMAN_CHECKPOINT_WAITING::{checkpoint_id}", flush=True)
        try:
            while True:
                if answer_path.is_file():
                    try:
                        answer = json.loads(answer_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        time.sleep(0.2)
                        continue
                    if isinstance(answer, dict) and answer.get("question_id") == checkpoint_id:
                        break
                time.sleep(0.2)
        finally:
            pending_path.unlink(missing_ok=True)
        answers = {
            str(key): str(value).strip()
            for key, value in (answer.get("answers") or {}).items()
            if str(value).strip()
        }
        # ``pending.json`` and ``answer.json`` are intentionally transient IPC
        # files. Persist an immutable copy before clearing them so a multi-round
        # task can show every knowledge supplementation form and its answer in
        # the task detail after the worker has resumed.
        history_record = {
            **payload,
            "answers": answers,
            "stop": bool(answer.get("stop")),
            "submitted_at": answer.get("submitted_at") or time.time(),
        }
        _write_json_atomic(self.root / "history" / f"{checkpoint_id}.json", history_record)
        answer_path.unlink(missing_ok=True)
        print(f"HUMAN_CHECKPOINT_ANSWERED::{checkpoint_id}", flush=True)
        return answers, bool(answer.get("stop"))


__all__ = [
    "FileCheckpointClient",
    "extract_gap_questions_from_semantic_reports",
    "extract_gap_questions_from_skill",
    "format_qa_context",
]
