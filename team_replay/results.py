"""Describe observed Replay execution independently of its acceptance verdict."""

from __future__ import annotations

from typing import Any, Mapping

from .metrics import metric_number

_FAILED = {"failed", "error"}
_SKIPPED = {"skipped", "unsupported", "blocked", "incompatible"}
_NOT_RUN = {"not_run", "not_found", "pending", "queued", "running", "cancelled", "canceled"}
_FINISHED = {"", "evaluated", "completed", "ok", "succeeded", "success", "checklist_incomplete"}


def _branch_observed(side: Mapping[str, Any]) -> bool:
    """A status/ok flag is metadata; responses, traces and observed zeroes are evidence."""
    if any(isinstance(side.get(key), str) and side[key].strip()
           for key in ("response", "final_response", "response_text")):
        return True
    if any(metric_number(side.get(key)) is not None
           for key in ("interaction_turns", "tool_call_count", "total_tokens")):
        return True
    return any(
        isinstance(message, Mapping) and (
            isinstance(message.get("content"), str) and message["content"].strip()
            or isinstance(message.get("tool_calls"), list) and message["tool_calls"]
        )
        for message in side.get("messages") or []
    ) if isinstance(side.get("messages"), list) else False


def _branch_execution(side: Any) -> str:
    if not isinstance(side, Mapping) or not side:
        return "not_run"
    status = str(side.get("status") or "").strip().lower()
    if status in _FAILED:
        return "failed"
    if status in _SKIPPED:
        return "skipped"
    if status in _NOT_RUN:
        return "not_run"
    if side.get("ok") is False or side.get("error") or side.get("error_code"):
        return "failed"
    if status not in _FINISHED or not _branch_observed(side):
        return "not_run"
    return "completed"


def replay_execution_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """A returned evaluation object alone is never proof that Replay ran."""
    raw_status = str(summary.get("status") or "").strip().lower()
    cases = summary.get("cases") if isinstance(summary.get("cases"), list) else []
    states = [
        {branch: _branch_execution(case.get(branch) if isinstance(case, Mapping) else None)
         for branch in ("baseline", "candidate")}
        for case in cases
    ]
    paired_count = sum(all(status == "completed" for status in pair.values()) for pair in states)
    branch_states = {status for pair in states for status in pair.values()}
    reason = str(summary.get("reason") or summary.get("error") or "")
    static = summary.get("static_validation")
    static_failed = isinstance(static, Mapping) and static.get("passed") is False
    if raw_status in _FAILED or "failed" in branch_states or static_failed:
        status = "failed"
    elif raw_status in _SKIPPED or "skipped" in branch_states:
        status = "skipped"
    elif raw_status in _NOT_RUN or "not_run" in branch_states:
        status = "not_run"
    elif raw_status in {"", "evaluated", "completed"} and paired_count:
        status = "completed"
    else:
        status = "not_run"
    if status != "completed" and not reason:
        failures = []
        for case, pair in zip(cases, states):
            for branch, state in pair.items():
                if state == "completed":
                    continue
                side = case.get(branch) if isinstance(case, Mapping) else None
                detail = (
                    side.get("error") or side.get("reason") or side.get("error_code") or side.get("status")
                    if isinstance(side, Mapping) else None
                )
                failures.append(f"{branch}: {detail or '未获得 Agent 执行观测'}")
        reason = "; ".join(dict.fromkeys(failures)) or (
            "True Replay 未完成，尚未获得可验证的 Baseline/Candidate 双分支结果"
        )
    return {"status": status, "reason": reason, "case_count": paired_count}
