"""Explicit recovery reuses accepted findings and archives each delivery independently."""

from ..task_io import digest
from .input import sha
from .orchestration import update
from .partition import window_text
from .state import Journal


async def resume(engine, job, body):
    if body and set(body) != {"request_id"}:
        raise ValueError("resume supports request_id only; frozen budgets cannot be changed")
    request = str(body.get("request_id") or "")
    if request and job.get("dialogue_resume_request") == digest(request):
        return job
    if job["status"] not in {"interrupted", "stopped", "failed"}:
        raise ValueError("dialogue task is not resumable")
    journal = Journal(engine, job)
    state = await journal.get("summary")
    if state and state["stage"] == "finished":
        if not request:
            raise ValueError("partial delivery resume requires a stable request_id")
        epoch = job.get("dialogue_recovery_epoch", 0) + 1
        new_job = {**job, "dialogue_recovery_epoch": epoch}
        target = Journal(engine, new_job)
        missing = 0
        for number in range(1, state["extract_count"] + 1):
            unit = await journal.get(f"extract/{number}")
            missing += len(unit["files"]) - len(unit["accepted"])
            unit["projected"] = False
            if unit["status"] != "accepted":
                unit.update(attempt=0, status="pending")
                for field in ("request", "key", "task_id", "cancel_sent", "submitted_at", "staged_inputs",
                              "error", "errors", "definitive_failure"):
                    unit.pop(field, None)
            await target.save(**{f"extract/{number}": unit})
        for number in range(1, state["merge_count"] + 1):
            unit = await journal.get(f"merge/{number}")
            if unit["status"] == "accepted":
                await target.save(**{"reusable_merge/" + sha(window_text(unit["window"])): unit})
        summary = {**state, "stage": "extract", "cursor": 1, "merge_count": 0, "processed": 0,
                   "pending": state["pending"] - missing, "usable": 0, "isolated": 0, "warnings": 0,
                   "dimensions": 0, "blocked_units": 0, "merge_planning_cursor": 1, "merge_buffer": None,
                   "page_count": 0}
        summary.pop("submission_rejection", None)
        summary.pop("submission_error", None)
        await target.save(summary=summary)
        root = job["dialogue_output_root"].split("/deliveries/")[0]
        return await update(engine, job, status="preparing", dialogue_recovery_epoch=epoch,
                            dialogue_output_root=root + f"/deliveries/recovery-{epoch}",
                            dialogue_resume_request=digest(request), ov_task_id="", compile_request=None,
                            ov_cancel_reason="", remote_terminal_unconfirmed=False, materialized=False,
                            error="", finished_at="")
    # Active/unknown remote units retain their original key and task ID.
    return await update(engine, job, status="preparing", ov_cancel_reason="", ov_cancel_sent_at="",
                        remote_terminal_unconfirmed=False, error="", finished_at="")
