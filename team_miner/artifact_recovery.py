"""One audited protocol transition for the identified integrated-body capacity failure."""

from __future__ import annotations

from pathlib import Path

from .skill_body import PROTOCOL as INTEGRATED_PROTOCOL
from .skill_body import previous_body
from .skill_body_insert import PROTOCOL

ERROR_PREFIX = "Artifact repair limit reached: "
CAPACITY_ERROR = "Independent artifact must be complete and within 12K characters"


def prepare_recovery(root: Path, state: dict, job: dict, *, apply: bool = False) -> dict:
    from .ov_artifact_batch_orchestration import _verify
    from .ov_staged_orchestration import _read, _save

    journal_path = root / "recovery/body-insert-v1.json"
    if journal_path.exists():
        journal = _read(journal_path)
        if (journal["job_id"] != job["job_id"] or journal["round"] != job["current_round"]
                or journal["error"] != job["error"] or journal["failed_at"] != job["finished_at"]):
            raise ValueError("容量恢复记录不属于本次失败")
    else:
        current = state.get("current") or {}
        if (state.get("round") != job["current_round"] or state.get("phase") != "skill_section"
                or state.get("skill_body_protocol") != INTEGRATED_PROTOCOL or state.get("body_insert_protocol")
                or state.get("complete") or current.get("stage") != "artifact_batch"
                or current.get("artifact_stage") != "skill_section" or not current.get("task_id")
                or current["task_id"] != job.get("ov_task_id") or job.get("ov_status") != "completed"):
            raise ValueError("任务不在已确认的完整正文容量失败边界")
        _, _, rows, _ = _verify(root, current)
        names = job["error"][len(ERROR_PREFIX):].split(", ")
        ledgers = {}
        for name in names:
            row = next((item for item in rows if item["context"]["skill_name"] == name), None)
            if row is None:
                raise ValueError("容量失败候选缺少原始冻结分配")
            context = row["context"]
            outcome = _read(root / "artifact-batches/outcomes" /
                            (current["child"] + "-" + row["output_path"] + ".json"))
            ledger = _read(root / "artifact-batches/candidates" / (name + ".json"))
            if (outcome["context"] != context or outcome["valid"] or outcome["error"] != CAPACITY_ERROR
                    or ledger.get("error") != CAPACITY_ERROR or ledger.get("format_attempts") != 3
                    or ledger["cursor"] != context["before"] or ledger["section_count"] != context["section_index"]
                    or not previous_body(root, name, ledger["section_count"])["content"]):
                raise ValueError("只能恢复已确认的整篇正文超限，其他校验失败需独立诊断")
            attempts = {}
            for path in (root / "artifact-batches/outcomes").glob(
                "*-" + name + f"-section-{context['section_index']:08d}.json.json"
            ):
                prior = _read(path)
                assignment = prior.get("context", {})
                if {k: v for k, v in assignment.items() if k != "format_attempts"} != {
                    k: v for k, v in context.items() if k != "format_attempts"
                }:
                    continue
                attempts[assignment.get("format_attempts")] = prior
            if (set(attempts) != {0, 1, 2}
                    or any(value["valid"] or value["error"] != CAPACITY_ERROR for value in attempts.values())):
                raise ValueError("三次原始修复未全部确认是同一完整正文容量问题")
            ledgers[name] = ledger
        # Every sibling must already have a durable result; no unpolled Compile is discarded.
        for row in rows:
            outcome = _read(root / "artifact-batches/outcomes" /
                            (current["child"] + "-" + row["output_path"] + ".json"))
            if outcome["context"] != row["context"]:
                raise ValueError("已完成兄弟结果的冻结分配发生变化")
        journal = {"job_id": job["job_id"], "round": job["current_round"], "error": job["error"],
                   "failed_at": job["finished_at"], "protocol": PROTOCOL, "original_state": state,
                   "failed_ledgers": ledgers, "reason": "整篇正文超限已修复，保留旧正文并仅生成有界新增内容"}
        if apply:
            _save(journal_path, journal)
    if not apply:
        return journal
    # Replaying after a process exit repeats the same projection, never creates another allowance.
    for name, ledger in journal["failed_ledgers"].items():
        _save(root / "artifact-batches/candidates" / (name + ".json"),
              {**ledger, "format_attempts": 0, "error": "", "capacity_recovery_protocol": PROTOCOL})
    next_state = dict(journal["original_state"])
    next_state.update(current=None, body_insert_protocol=PROTOCOL,
                      completed_tasks=next_state.get("completed_tasks", 0) + 1)
    _save(root / "state.json", next_state)
    return journal
