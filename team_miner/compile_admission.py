"""Durable local admission for Native Compile requests; reservations never expire by time."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

OPTIMIZED_PROTOCOL = "te-mining-optimized.v1"
MAX_NATIVE_COMPILES = 2
TERMINAL_NATIVE_STATES = {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}


@contextmanager
def _locked(jobs_root: Path):
    jobs_root.mkdir(parents=True, exist_ok=True)
    lock = jobs_root / ".compile-admission.lock"
    if lock.is_symlink():
        raise ValueError("Compile admission lock cannot be a symlink")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield jobs_root / ".compile-admission.json"
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("Compile admission journal cannot be a symlink")
    if not path.exists():
        return {"version": 1, "reservations": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("reservations"), dict):
        raise ValueError("Invalid Compile admission journal")
    return value


def _save(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".admission-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def acquire(repository, job: dict, current: dict) -> bool:
    """Reserve before POST, counting other locally observed mining Compile tasks too."""
    with _locked(repository.jobs_root) as path:
        value = _read(path)
        reservations = value["reservations"]
        key = current["key"]
        if key in reservations:
            row = reservations[key]
            if row["job_id"] != job["job_id"] or row["round"] != int(job["current_round"]):
                raise ValueError("Compile reservation belongs to another job")
            return True
        live = {"key:" + identity for identity in reservations}
        covered_tasks = {row.get("task_id") for row in reservations.values() if row.get("task_id")}
        covered_jobs = {row["job_id"] for row in reservations.values()}
        for other in repository.list_jobs():
            task_id = other.get("ov_task_id")
            if (
                other.get("engine") == "ov_compile"
                and task_id
                and (other.get("status") in {"queued", "running", "preparing", "stopping"}
                     or other.get("remote_terminal_unconfirmed"))
                and other["job_id"] not in covered_jobs
                and task_id not in covered_tasks
            ):
                live.add("task:" + str(task_id))
        if len(live) >= MAX_NATIVE_COMPILES:
            return False
        reservations[key] = {
            "job_id": job["job_id"],
            "round": int(job["current_round"]),
            "account": str(job.get("account") or ""),
            "task_id": "",
        }
        _save(path, value)
        return True


def accepted(jobs_root: Path, key: str, task_id: str) -> None:
    with _locked(jobs_root) as path:
        value = _read(path)
        row = value["reservations"].get(key)
        if row:
            if row.get("task_id") and row["task_id"] != task_id:
                raise ValueError("Idempotent Compile reservation changed task identity")
            row["task_id"] = task_id
            _save(path, value)


def release(jobs_root: Path, *, task_id: str = "", key: str = "") -> None:
    """Caller must have observed a terminal task, or established that no POST occurred."""
    if not task_id and not key:
        return
    with _locked(jobs_root) as path:
        value = _read(path)
        remove = [
            identity
            for identity, row in value["reservations"].items()
            if identity == key or (task_id and row.get("task_id") == task_id)
        ]
        for identity in remove:
            value["reservations"].pop(identity)
        if remove:
            _save(path, value)


def reservations(jobs_root: Path) -> list[dict]:
    with _locked(jobs_root) as path:
        return [{"key": key, **row} for key, row in _read(path)["reservations"].items()]
