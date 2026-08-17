"""Recover uploaded input files from archived Agent session snapshots."""

from __future__ import annotations

import base64
import hashlib
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_BYTES = 80 * 1024 * 1024
_MAX_FILES = 100


def _safe_material_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").lstrip("./")
    parts = PurePosixPath(raw).parts
    if (
        not raw
        or raw.startswith("/")
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return ""
    return "/".join(parts)


def _embedded_materials(
    session: Mapping[str, Any],
) -> Iterable[tuple[str, bytes]]:
    for item in session.get("source_materials") or []:
        if not isinstance(item, Mapping):
            continue
        rel_path = _safe_material_path(item.get("path"))
        if not rel_path:
            continue
        try:
            data = base64.b64decode(
                str(item.get("content_b64") or ""),
                validate=True,
            )
        except (ValueError, TypeError):
            continue
        yield rel_path, data


def _trusted_snapshot_path(session: Mapping[str, Any]) -> Path | None:
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), Mapping)
        else {}
    )
    raw = str(runtime_context.get("sandbox_snapshot_path") or "").strip()
    if not raw:
        return None
    archive = Path(raw).expanduser()
    session_id = str(session.get("session_id") or "").strip()
    parts = set(archive.parts)
    if (
        "session_snapshots" not in parts
        or not session_id
        or session_id not in parts
        or not archive.name.endswith(".tar.gz")
        or not archive.is_file()
    ):
        return None
    return archive


def _snapshot_materials(
    session: Mapping[str, Any],
) -> Iterable[tuple[str, bytes]]:
    archive = _trusted_snapshot_path(session)
    if archive is None:
        return
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                rel_path = _safe_material_path(member.name)
                if not rel_path or not rel_path.startswith("uploads/"):
                    continue
                if member.size < 0 or member.size > _MAX_FILE_BYTES:
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                data = source.read(_MAX_FILE_BYTES + 1)
                if len(data) > _MAX_FILE_BYTES:
                    continue
                yield rel_path, data
    except (OSError, tarfile.TarError):
        return


def collect_session_materials(
    sessions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collect deduplicated real uploads from source sessions.

    Embedded ``source_materials`` support cross-host Agents, while
    ``sandbox_snapshot_path`` recovers same-host AgentsHub uploads without
    inflating the ingest payload.
    """
    output: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    used_paths: set[str] = set()
    total_bytes = 0
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        session_id = str(session.get("session_id") or "").strip()
        candidates = list(_embedded_materials(session))
        if not candidates:
            candidates = list(_snapshot_materials(session))
        for raw_path, data in candidates:
            if (
                not data
                or len(data) > _MAX_FILE_BYTES
                or total_bytes + len(data) > _MAX_TOTAL_BYTES
                or len(output) >= _MAX_FILES
            ):
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                continue
            rel_path = _safe_material_path(raw_path)
            if not rel_path:
                continue
            if rel_path in used_paths:
                rel_path = _safe_material_path(
                    f"source_sessions/{session_id}/{rel_path}"
                )
            if not rel_path:
                continue
            seen_hashes.add(digest)
            used_paths.add(rel_path)
            total_bytes += len(data)
            output.append(
                {
                    "path": rel_path,
                    "size": len(data),
                    "sha256": digest,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "source_session_id": session_id,
                }
            )
    return output
