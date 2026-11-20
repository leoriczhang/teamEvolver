"""Compose a pull's exact members with business checkpoints and public task states."""
from __future__ import annotations

from collections import defaultdict

from .pull_runs import PullRunRepository

TERMINAL = {"succeeded", "failed", "cancelled"}
_ORDER = {"session.normalize": 0, "session.split": 1, "session.analyze": 2, "session.archive": 3}
_STATES = ("archived", "skipped", "failed", "cancelled", "pending")


def _node_status(node, task):
    # A committed business archive remains authoritative if the worker crashed
    # before confirming its attempt. A failed stage alone may already be retrying.
    if node["archive_status"] == "succeeded":
        return "skipped" if node["ingestion_status"] == "skipped" else "archived"
    if task and task["status"] in {"failed", "cancelled"}:
        return task["status"]
    return "expanded" if node["children"] and node["split_status"] == "succeeded" else "pending"


def _member(member, nodes, tasks, *, truncated=False):
    if member["acceptance"] in {"error", "empty"}:
        return {**member, "ingestion_id": member["ingestion_id"] or "", "status": member["acceptance"],
                "session_ids": [], "descendants": [], "descendants_truncated": False}, []
    leaves = []
    projected = []
    for node in nodes:
        task = tasks.get(node["node"])
        state = _node_status(node, task)
        projected.append({"ingestion_id": node["node"], "status": state,
                          "error_code": (task or {}).get("error_code", ""), "session_ids": node["session_ids"]})
        if not node["children"]:
            leaves.append(state)
    states = {node["status"] for node in projected}
    if "failed" in states:
        state = "failed"
    elif "cancelled" in states:
        state = "cancelled"
    elif truncated or not leaves or "pending" in states:
        state = "pending"
    else:
        state = "skipped" if all(state == "skipped" for state in leaves) else "archived"
    errors = [node["error_code"] for node in projected if node["status"] in {"failed", "cancelled"}]
    return {**member, "status": state, "error_code": next(iter(errors), ""),
            "session_ids": sorted({sid for node in projected for sid in node["session_ids"]}),
            "descendants": projected[:200], "descendants_truncated": truncated or len(projected) > 200}, leaves


async def progress(services, conn, *, job, run_id, limit=50, offset=0):
    runs = PullRunRepository(services.tx)
    tracked = await runs.exists(conn, run_id)
    counts = await runs.counts(conn, run_id) if tracked else dict.fromkeys(
        ("fetched", "enqueued", "duplicate", "error", "empty"), 0)
    summary = {"total": counts["fetched"], **{k: counts[k] for k in ("enqueued", "duplicate", "error", "empty")},
               **dict.fromkeys(_STATES, 0), "leaf": {"total": 0, **dict.fromkeys(_STATES, 0)}}
    public_job = {"job_id": job["task_id"], **{k: job[k] for k in ("kind", "status", "attempt", "error_code")}}
    result = {"job": public_job, "tracking_available": tracked, "counts": counts, "summary": summary,
              "members": [], "limit": limit, "offset": offset, "has_more": False,
              "pull_complete": job["status"] in TERMINAL, "analysis_complete": False,
              "complete": False, "status": "incomplete", "summary_truncated": False}
    if not tracked:
        result["reason"] = "pull_membership_not_recorded"
        if job["status"] in {"failed", "cancelled"}:
            result["status"] = job["status"]
        return result

    # Iterate keyset pages. Keep only the requested response page in memory;
    # global counters include every member, regardless of response pagination.
    after, position = "", 0
    while True:
        members = await runs.page(conn, run_id, after=after)
        if not members:
            break
        nodes, truncated = await runs.nodes(conn, [m["ingestion_id"] for m in members if m["ingestion_id"]])
        result["summary_truncated"] |= truncated
        by_root = defaultdict(list)
        for node in nodes:
            by_root[node["root"]].append(node)
        task_states = {}
        ids = list(dict.fromkeys(node["node"] for node in nodes if node["archive_status"] != "succeeded"))
        for start in range(0, len(ids), 1000):
            tasks = await services.tasks.get_for_subjects(conn, subject_ids=ids[start:start + 1000])
            for task in tasks:
                previous = task_states.get(task["subject_id"])
                if previous is None or _ORDER[task["kind"]] > _ORDER[previous["kind"]]:
                    task_states[task["subject_id"]] = task
        for member in members:
            item, leaves = _member(member, by_root[member["ingestion_id"]], task_states, truncated=truncated)
            state = item["status"]
            if state in _STATES:
                summary[state] += 1
            summary["leaf"]["total"] += len(leaves)
            for leaf in leaves:
                summary["leaf"][leaf] += 1
            if offset <= position < offset + limit:
                result["members"].append(item)
            position += 1
        after = members[-1]["external_id"]
    result["has_more"] = position > offset + limit
    result["analysis_complete"] = result["pull_complete"] and not (
        summary["pending"] or summary["leaf"]["pending"] or result["summary_truncated"])
    result["complete"] = result["pull_complete"] and result["analysis_complete"]
    if job["status"] == "failed" or summary["failed"] or summary["error"]:
        result["status"] = "failed"
    elif job["status"] == "cancelled" or summary["cancelled"]:
        result["status"] = "cancelled"
    elif result["summary_truncated"]:
        result["status"], result["reason"] = "incomplete", "split_tree_limit_exceeded"
    else:
        result["status"] = "completed" if result["complete"] else "running"
    return result
