"""Replay deterministic selection after accepted identity merges, without repeating Native work."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .mining_intent import intent_from_job, method_selection_changes, select_candidates

ERROR = "Exact method membership must target a final candidate"
PROTOCOL = "te-alias-selection-recovery.v1"


def prepare_recovery(root: Path, state: dict, job: dict, *, apply: bool = False) -> dict:
    from .ov_staged_orchestration import _json, _read, _save, _sha
    from .round_reuse import _accepted_extract_batch
    from .source_unit_scope import accepted_source_classification

    path = root / "recovery/alias-selection-v1.json"
    if path.exists():
        existing = _read(path)
        replay = copy.deepcopy(existing["original_state"])
        replay["intent_selection_complete"] = False
        if state == replay:
            state = copy.deepcopy(existing["original_state"])
    if (job.get("error") != ERROR or state.get("round") != job["current_round"]
            or state.get("phase") != "skill_section" or state.get("current") is not None
            or state.get("complete") or not state.get("identity_review_complete")
            or not state.get("intent_selection_complete") or state.get("source_routing_complete")
            or state.get("source_unit_protocol") != "te-exact-source-units.v1"
            or state.get("source_unit_cursor") != state.get("source_unit_count")
            or any(state.get(key, 0) != 0 for key in ("candidate_index", "cursor", "case_index", "section_count"))
            or job.get("ov_status") != "completed" or not job.get("finished_at")
            or state.get("attempted_tasks") != state.get("completed_tasks")):
        raise ValueError("任务不在已完成归并和选择、尚未提交正文的别名归属失败边界")
    intent = intent_from_job(job)
    if not intent["active"] or intent != state.get("mining_intent"):
        raise ValueError("恢复必须保留原任务冻结的挖掘模式、方向和数量")
    original = _read(root / "mining-intent/original-plan.json")
    aliases = _read(root / "identity/aliases.json")
    assessments = {path.stem: _read(path) for path in (root / "mining-intent/assessments").glob("*.json")}
    refs = [_read(path)["uri"] for path in (root / "source-files").glob("*.json")]
    selected, audit = select_candidates(original, intent, assessments, aliases, refs)
    if selected != _read(root / "plan.json") or audit != _read(root / "mining-intent/selection.json"):
        raise ValueError("不能借恢复更换已选候选、任务边界或原选择结果")
    changes = method_selection_changes(root, original, audit, aliases)
    if not changes or not any(row["receipt"]["resolved_name"] !=
                              json.loads(row["receipt"]["original_text"])["name"] for row in changes):
        raise ValueError("没有可由原别名与排除审计证明的遗漏归属")
    records, _, _ = accepted_source_classification(root, state)
    for index, unit in enumerate(records):
        if _accepted_extract_batch(root, unit, index) is None:
            raise ValueError("别名归属恢复缺少原已验收抽取来源")
    protected = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / "state.json" and not path.is_relative_to(root / "recovery"):
            if path.is_symlink():
                raise ValueError("别名归属恢复不能读取符号链接")
            protected[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    journal = {"protocol": PROTOCOL, "job_id": job["job_id"], "round": job["current_round"],
               "error": job["error"], "failed_at": job["finished_at"], "original_state": copy.deepcopy(state),
               "original_state_sha256": _sha(_json(state)), "method_changes": changes,
               "protected_files": protected,
               "reason": "修复已合并成员未跟随存续候选的选择归属；原选择不变，仅重放确定性选择投影，不重跑模型"}
    path = root / "recovery/alias-selection-v1.json"
    if path.exists() and _read(path) != journal:
        raise ValueError("别名归属恢复记录不属于本次失败或冻结记录已变化")
    if apply:
        _save(path, journal)
        state["intent_selection_complete"] = False
        _save(root / "state.json", state)
    return journal
