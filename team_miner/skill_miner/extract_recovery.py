"""Revalidate the identified structural-only extraction failure without another model retry."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .source_unit_extraction import STRUCTURAL_COVERAGE_PROTOCOL, bind_source_unit_index
from .source_units import PROTOCOL as SOURCE_UNIT_PROTOCOL

ERROR_PREFIX = "Extraction repair limit reached: "
_COVERAGE_ERROR = "Every assigned source must remain classified or explicitly deferred"
_JOURNAL = "recovery/extract-structural-coverage-v1.json"


def _plan(root: Path, state: dict, job: dict) -> dict:
    from .ov_extract_batch_orchestration import MAX_EXTRACT_ATTEMPTS, _verified
    from .ov_staged_orchestration import _json, _read, _sha
    from .round_reuse import _accepted_extract_batch

    current = state.get("current") or {}
    if (state.get("round") != job["current_round"] or state.get("phase") != "extract"
            or state.get("source_unit_protocol") != SOURCE_UNIT_PROTOCOL or state.get("complete")
            or current.get("stage") != "extract_batch" or not current.get("task_id")
            or current["task_id"] != job.get("ov_task_id") or job.get("ov_status") != "completed"
            or not job.get("finished_at") or not str(job.get("error", "")).startswith(ERROR_PREFIX)):
        raise ValueError("任务不在已确认的来源结构覆盖失败边界")
    _, _, rows, texts, ledger = _verified(root, current)
    pending = [slot for slot in ledger["slots"] if slot["status"] == "pending"]
    if (not pending or job["error"] != ERROR_PREFIX + ", ".join(str(slot["context"]["unit_index"]) for slot in pending)
            or any(slot["format_attempts"] != MAX_EXTRACT_ATTEMPTS
                   or not slot["error"].startswith(_COVERAGE_ERROR) for slot in pending)):
        raise ValueError("只能重新验收已确认的结构覆盖失败，不增加模型重试次数")
    # Each accepted sibling stays byte-identical, including earlier file Maps in this batch.
    siblings = {}
    for slot in ledger["slots"]:
        if slot["status"] != "valid":
            continue
        index = slot["context"]["unit_index"]
        unit = _read(root / "units/0" / f"{index:08d}.json")
        origin = _accepted_extract_batch(root, unit, index)
        if origin is None:
            raise ValueError("已验收兄弟结果缺少冻结来源记录")
        for field in ("accepted_origin", "result", "packet_file", "request_file", "compiler_file", "native_metadata"):
            record = origin["files"][field]
            siblings[record["path"]] = record["sha256"]
        siblings[f"units/0/{index:08d}.json"] = _sha(_json(unit) + "\n")
    entries = []
    next_ledger = deepcopy(ledger)
    for row, text in zip(rows, texts, strict=True):
        context = row["context"]
        slot = ledger["slots"][context["slot"]]
        outcome_path = "extract-batches/outcomes/" + current["child"] + "-" + row["output_path"] + ".json"
        outcome = _read(root / outcome_path)
        if outcome["context"] != context:
            raise ValueError("来源抽取原始验收记录改变了冻结分配")
        if slot["status"] == "valid":
            continue
        result_file = "children/" + current["child"] + "/" + row["output_path"] + ".raw"
        result_path = root / result_file
        if (result_path.is_symlink() or outcome["valid"] or not outcome["error"].startswith(_COVERAGE_ERROR)
                or outcome["result_file"] != result_file or context["format_attempts"] != MAX_EXTRACT_ATTEMPTS - 1):
            raise ValueError("来源抽取失败缺少最后一次完整原始结果")
        raw = result_path.read_text(encoding="utf-8")
        if _sha(raw) != outcome["result_sha256"] or not raw.strip() or len(raw) > 48000:
            raise ValueError("来源抽取原始结果 SHA 或完整长度发生变化")
        identity = current["child"] + "::" + row["output_path"]
        unit = bind_source_unit_index(root, json.loads(raw), json.loads(text), context, identity)
        if not unit.get("structural_coverage"):
            raise ValueError("失败不是已识别的纯结构遗漏，必须独立诊断")
        unit_path = f"units/0/{context['unit_index']:08d}.json"
        origin = {
            "unit_id": unit["id"], "unit_index": context["unit_index"], "unit_path": unit_path,
            "identity": identity, "context": context, "child": current["child"],
            "packet_file": row["local_path"], "packet_sha256": row["sha256"],
            **{key: current[key] for key in ("request_file", "request_sha256", "compiler_file",
                                            "compiler_sha256", "compiler_source_sha256")},
            "result_file": result_file, "result_sha256": outcome["result_sha256"],
            "unit_sha256": _sha(_json(unit) + "\n"), "recovery_protocol": STRUCTURAL_COVERAGE_PROTOCOL,
            "original_outcome_file": outcome_path, "original_outcome_sha256": _sha(_json(outcome) + "\n"),
        }
        entries.append({"unit": unit, "origin": origin})
        next_ledger["slots"][context["slot"]].update(status="valid", error="", unit_id=unit["id"],
                                                    recovery_protocol=STRUCTURAL_COVERAGE_PROTOCOL)
    if len(entries) != len(pending):
        raise ValueError("结构恢复必须覆盖全部失败分配，不能跳过真实遗漏")
    return {"job_id": job["job_id"], "round": job["current_round"], "error": job["error"],
            "failed_at": job["finished_at"], "protocol": STRUCTURAL_COVERAGE_PROTOCOL,
            "original_state": state, "original_ledger": ledger, "next_ledger": next_ledger,
            "entries": entries, "siblings": siblings,
            "reason": "修复纯结构标题覆盖误判，按冻结原文和 SHA 重新验收原始输出；未新增模型重试或业务事实"}


def prepare_recovery(root: Path, state: dict, job: dict, *, apply: bool = False) -> dict:
    from .ov_staged_orchestration import _read, _save, _sha

    path = root / _JOURNAL
    if path.exists():
        if path.is_symlink():
            raise ValueError("结构恢复记录不能是符号链接")
        journal = _read(path)
        if any(journal[key] != job[field] for key, field in (
            ("job_id", "job_id"), ("round", "current_round"), ("error", "error"), ("failed_at", "finished_at")
        )):
            raise ValueError("结构恢复记录不属于本次失败")
        original = journal["original_state"]
        current_ledger = _read(root / original["current"]["ledger_file"])
        # Restart-safe transaction replay never grants a fresh repair allowance.
        if current_ledger not in (journal["original_ledger"], journal["next_ledger"]):
            raise ValueError("结构恢复事务的当前来源账目发生变化")
        for relative, digest in journal["siblings"].items():
            if _sha((root / relative).read_text(encoding="utf-8")) != digest:
                raise ValueError("已验收兄弟结果发生变化")
        for entry in journal["entries"]:
            origin = entry["origin"]
            # Verify frozen request/packet/compiler again, even after a partial commit.
            from .ov_extract_batch_orchestration import _verified

            _, _, rows, texts, _ = _verified(root, original["current"])
            row, text = next((row, text) for row, text in zip(rows, texts, strict=True)
                             if row["context"] == origin["context"])
            raw = (root / origin["result_file"]).read_text(encoding="utf-8")
            if (_sha(raw) != origin["result_sha256"]
                    or _sha((root / origin["original_outcome_file"]).read_text(encoding="utf-8"))
                    != origin["original_outcome_sha256"]):
                raise ValueError("结构恢复引用的原始结果或验收记录发生变化")
            projected = bind_source_unit_index(
                root, json.loads(raw), json.loads(text), row["context"], origin["identity"],
            )
            if projected != entry["unit"]:
                raise ValueError("结构恢复投影不再匹配完整原始输出")
    else:
        journal = _plan(root, state, job)
        if apply:
            _save(path, journal)
    if not apply:
        return journal
    for entry in journal["entries"]:
        origin = entry["origin"]
        for destination, value in ((root / origin["unit_path"], entry["unit"]),
                                   (root / "extract-batches/accepted" / (origin["unit_id"] + ".json"), origin)):
            if destination.exists() and _read(destination) != value:
                raise ValueError("结构恢复不能覆盖不同的已验收结果")
            if not destination.exists():
                _save(destination, value)
    original = journal["original_state"]
    _save(root / original["current"]["ledger_file"], journal["next_ledger"])
    _save(root / "state.json", {**original, "current": None,
                               "completed_tasks": original["completed_tasks"] + 1})
    return journal
