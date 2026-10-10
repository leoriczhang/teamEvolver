"""Bounded local text access for current Skill mining artifacts and round archives."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
_TEXT_SUFFIXES = {".md", ".markdown", ".json", ".jsonl", ".txt"}
_ARTIFACT_KINDS = {
    "SKILL.md": "skill",
    "EVALUATION.md": "evaluation",
    "BENCHMARK.md": "benchmark",
    "benchmark.json": "benchmark",
}


def _artifact_path(relative_path: str) -> tuple[list[str], list[str], str]:
    if not isinstance(relative_path, str):
        raise ValueError("产物路径必须是文本")
    raw = relative_path.strip().replace("\\", "/")
    parts = raw.split("/")
    if (not raw or "\x00" in raw or any(not part or part.startswith(".") for part in parts)
            or len(parts) < 5 or parts[0] != "mining_jobs"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", parts[1])):
        raise ValueError("产物路径不在允许的挖掘任务目录中")
    if parts[2] == "workspace":
        output = parts[3:]
    elif (len(parts) >= 7 and parts[2] == "rounds"
          and re.fullmatch(r"round-[1-5]", parts[3])
          and parts[4] in {"output", "reviewed_output"}):
        output = parts[5:]
    else:
        raise ValueError("任务路径不是可预览的挖掘产物")
    if output[0] not in {"compiled_skill", "semantic_reports"} or len(output) < 2:
        raise ValueError("任务路径不是可预览的挖掘产物")
    if output[0] == "compiled_skill" and len(output) < 3:
        raise ValueError("Skill 产物路径无效")
    if Path(parts[-1]).suffix.lower() not in _TEXT_SUFFIXES:
        raise ValueError("产物不是支持预览的文本文件")
    kind = _ARTIFACT_KINDS.get(parts[-1], "semantic" if output[0] == "semantic_reports" else "resource")
    return parts, output, kind


def artifact_job_id(relative_path: str) -> str:
    """Return the validated job ID so API callers can share the job's mutation lock."""
    parts, _, _ = _artifact_path(relative_path)
    return parts[1]


def _safe_open(name: str, flags: int, *, dir_fd: int | None = None, mode: int = 0o600) -> int:
    try:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("产物路径不能包含符号链接或无效目录") from exc
        raise


@contextmanager
def _directory(parent: Path | int, parts: list[str]) -> Iterator[int]:
    # Every component is opened relative to an already opened directory. A
    # symlink swap cannot redirect reads or atomic replacement outside the job.
    descriptor = os.dup(parent) if isinstance(parent, int) else _safe_open(
        str(parent), os.O_RDONLY | os.O_DIRECTORY
    )
    try:
        for part in parts:
            child = _safe_open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_bytes(directory: int, name: str, limit: int) -> bytes:
    descriptor = _safe_open(name, os.O_RDONLY | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("产物必须是普通文件")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("产物或任务元数据超过大小限制")
    return content


def _job_metadata(directory: int, job_id: str) -> dict:
    try:
        metadata = json.loads(_read_bytes(directory, "job.json", MAX_METADATA_BYTES).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法确认挖掘任务元数据") from exc
    if (not isinstance(metadata, dict) or metadata.get("job_id") != job_id
            or metadata.get("engine") != "ov_compile"):
        raise ValueError("挖掘任务元数据不匹配")
    return metadata


def _payload(parts: list[str], kind: str, content: bytes, *, edited: bool = False) -> dict:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("产物必须是有效的 UTF-8 文本") from exc
    if "\x00" in text:
        raise ValueError("产物包含二进制内容")
    return {
        "ok": True,
        "path": "/".join(parts),
        "name": parts[-1],
        "kind": kind,
        "content": text,
        "size_bytes": len(content),
        "truncated": False,
        "edited": edited,
    }


def read_artifact(project_root: Path | str, relative_path: str) -> dict:
    """Read one UTF-8 artifact, excluding input snapshots and control-plane files."""
    parts, _, kind = _artifact_path(relative_path)
    with _directory(Path(project_root).resolve(), parts[:2]) as job_directory:
        _job_metadata(job_directory, parts[1])
        with _directory(job_directory, parts[2:-1]) as artifact_directory:
            content = _read_bytes(artifact_directory, parts[-1], MAX_ARTIFACT_BYTES)
    return _payload(parts, kind, content)


def save_artifact(project_root: Path | str, relative_path: str, content: str) -> dict:
    """Atomically edit a completed current workspace; round archives stay immutable.

    API callers must hold their normal per-job mutation lock across this call
    so continuation, deletion and artifact edits cannot race each other.
    """
    parts, output, kind = _artifact_path(relative_path)
    if parts[2] != "workspace":
        raise ValueError("历史轮次产物为只读，请编辑本轮工作区")
    if parts[-1] not in _ARTIFACT_KINDS and not (
        output[0] == "semantic_reports" and Path(parts[-1]).suffix.lower() in {".md", ".markdown"}
    ):
        raise ValueError("该文件不是可编辑的挖掘产物")
    if not isinstance(content, str):
        raise ValueError("产物内容必须是文本")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("产物必须是有效的 UTF-8 文本") from exc
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("产物超过 2 MB，暂不支持在线编辑")
    result = _payload(parts, kind, encoded, edited=True)
    with _directory(Path(project_root).resolve(), parts[:2]) as job_directory:
        metadata = _job_metadata(job_directory, parts[1])
        if metadata.get("status") not in {"succeeded", "waiting_human"}:
            raise ValueError("只有已完成轮次或已完成任务的产物可以人工修改")
        with _directory(job_directory, parts[2:-1]) as artifact_directory:
            # Require an existing valid text artifact. In particular, do not
            # create arbitrary files or follow a symlink at the final component.
            _payload(parts, kind, _read_bytes(artifact_directory, parts[-1], MAX_ARTIFACT_BYTES))
            temporary = f".artifact-edit-{uuid.uuid4().hex}"
            descriptor = _safe_open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=artifact_directory
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, parts[-1], src_dir_fd=artifact_directory, dst_dir_fd=artifact_directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=artifact_directory)
                except FileNotFoundError:
                    pass
    return result
