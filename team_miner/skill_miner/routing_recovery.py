"""Recover the verified exact-unit/transport-routing mismatch at a completed local boundary."""

from __future__ import annotations

from pathlib import Path

ERROR = "Local source routing needs a unique accepted unit for each original range"
PROTOCOL = "te-exact-review-routing-recovery.v1"


def prepare_recovery(root: Path, state: dict, job: dict, *, apply: bool = False) -> dict:
    from .ov_staged_orchestration import _json, _read, _save, _sha
    from .round_reuse import _accepted_extract_batch
    from .source_unit_scope import accepted_source_classification

    if (job.get("error") != ERROR or state.get("round") != job["current_round"]
            or state.get("phase") != "evidence_review" or state.get("current") is not None
            or state.get("complete") or state.get("evidence_review_initialized")
            or state.get("source_unit_protocol") != "te-exact-source-units.v1"
            or state.get("source_unit_cursor") != state.get("source_unit_count")
            or job.get("ov_status") != "completed" or not job.get("finished_at")
            or state.get("attempted_tasks") != state.get("completed_tasks")):
        raise ValueError("任务不在已完成抽取、尚未提交原文复核的精确路由失败边界")
    records, _, _ = accepted_source_classification(root, state)
    for index, unit in enumerate(records):
        if _accepted_extract_batch(root, unit, index) is None:
            raise ValueError("精确路由恢复缺少已验收原始抽取来源")
    if (root / "evidence-review/manifest.json").exists():
        raise ValueError("不能覆盖已经冻结的原文复核范围")
    protected = {}
    for directory in ("children", "units", "chunks", "source-units", "catalog", "workflow-planning",
                      "method-decisions", "scopes", "mining-intent"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file():
                if path.is_symlink():
                    raise ValueError("路由恢复不能读取符号链接")
                protected[str(path.relative_to(root))] = _sha(path.read_text(encoding="utf-8"))
    for relative in ("plan.json", "compiler-base.md"):
        protected[relative] = _sha((root / relative).read_text(encoding="utf-8"))
    journal = {"protocol": PROTOCOL, "job_id": job["job_id"], "round": job["current_round"],
               "error": job["error"], "failed_at": job["finished_at"], "original_state_sha256": _sha(_json(state)),
               "accepted_unit_count": len(records), "source_unit_count": state["source_unit_count"],
               "protected_files": protected,
               "reason": "修复原文复核错误套用分片独占规则；保留全部已验收抽取与规划，从原文复核断点继续"}
    path = root / "recovery/exact-review-routing-v1.json"
    if path.exists() and _read(path) != journal:
        raise ValueError("精确路由恢复记录不属于本次失败或冻结输入已变化")
    if apply:
        _save(path, journal)
    return journal
