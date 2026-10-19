"""Public read model for Replay runs; callable from producer APIs."""
from __future__ import annotations

from .datasets.candidate_selection import dataset_groups
from .repository import ReplayRepository


async def present_run(conn, services, domain, record):
    task = await services.tasks.get(conn, task_id=record["run_id"])
    results = await ReplayRepository(services.tx).cases(conn, domain, record["run_id"])
    payload = record["input"]
    if "job" in payload:
        job = payload["job"]
        cases = job.get("replay_cases", [])
        summary = (record["result"] or {}).get("replay") or {}

        def succeeded(result):
            return bool(result.get("cases")) and all(
                case.get(branch, {}).get("completed") is True
                for case in result["cases"] for branch in ("baseline", "candidate")
            )

        return {
            "status": task["status"] if task else "failed", "run_id": record["run_id"],
            "job_id": record["run_id"], "status_url": f"/api/jobs/{record['run_id']}",
            "subject_id": record["subject_id"], "result": record["result"],
            "input_hash": record["input_hash"], "dataset_hash": payload["dataset_hash"],
            "total": len(cases), "completed": len(results) if results else (
                len(cases) if record["result"] is not None else 0
            ),
            "succeeded": sum(succeeded(result) for result in results.values()) if results else summary.get(
                "succeeded", 0,
            ),
            "failed": sum(not succeeded(result) for result in results.values()) if results else summary.get(
                "failed", 0,
            ),
            "cases": [{
                "case_id": case["case_id"], "query": case["query"],
                "status": ("completed" if succeeded(results[case["case_id"]]) else "failed")
                if case["case_id"] in results else task["status"] if task else "failed",
                "verdict": results.get(case["case_id"], {}).get("verdict"),
                "session_id": case.get("provenance", {}).get("session_id", ""),
                "dataset_id": case.get("provenance", {}).get("dataset_id", ""),
                "selection": case.get("provenance", {}).get("replay_selection", "current"),
            } for case in cases],
            "datasets": dataset_groups(cases), "sampling": job.get("replay_sampling", {}),
            "created_at": record["created_at"], "finished_at": record["finished_at"] or "",
        }
    spec = payload.get("spec")
    cases = spec["dataset"]["cases"] if spec else (
        payload["dataset"]["document"]["cases"] if payload["dataset"] else []
    )
    return {
        "status": task["status"] if task else "failed", "run_id": record["run_id"], "job_id": record["run_id"],
        "status_url": f"/api/jobs/{record['run_id']}",
        "subject_id": record["subject_id"], "dataset_id": record["dataset_id"],
        "created_at": record["created_at"], "finished_at": record["finished_at"] or "",
        "result": record["result"], "input_hash": record["input_hash"],
        "dataset_hash": payload["dataset"]["document_hash"] if payload["dataset"] else (
            spec.get("metadata", {}).get("dataset_hash", "") if spec else ""
        ),
        "total": len(cases), "completed": len(results),
        "succeeded": sum(result.get("completed") is True for result in results.values()),
        "failed": sum(result.get("ok") is False for result in results.values()), "skipped": 0,
        "error_code": task.get("error_code", "") if task else "",
        "cases": [{"case_id": case["case_id"], "query": case["query"],
                   "status": "completed" if case["case_id"] in results else task["status"] if task else "failed",
                   "success": results.get(case["case_id"], {}).get("completed"),
                   "session_id": case["provenance"].get("session_id", ""),
                   "trace_id": case["provenance"].get("trace_id", "")} for case in cases],
    }
