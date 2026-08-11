"""Helpers for reading, hashing, and writing multi-file skill bundles."""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

_BUNDLE_ENTRYPOINT = "SKILL.md"
_IGNORED_NAMES = {".DS_Store"}
_IGNORED_DIR_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class SkillBundleError(ValueError):
    """Raised when a bundle is malformed or a bundle path is unsafe."""


def _coerce_bytes(data: bytes | bytearray | str) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    raise TypeError(f"Unsupported bundle payload type: {type(data).__name__}")


def normalize_bundle_rel_path(rel_path: str) -> str:
    value = str(rel_path or "").strip().replace("\\", "/")
    if not value:
        raise SkillBundleError("Bundle path must not be empty")
    parts = PurePosixPath(value).parts
    if not parts:
        raise SkillBundleError("Bundle path must not be empty")
    if any(part in {"", ".", ".."} for part in parts):
        raise SkillBundleError(f"Unsafe bundle path: {rel_path!r}")
    return "/".join(parts)


def is_ignored_bundle_rel_path(rel_path: str) -> bool:
    parts = PurePosixPath(normalize_bundle_rel_path(rel_path)).parts
    if any(part in _IGNORED_DIR_NAMES for part in parts[:-1]):
        return True
    leaf = parts[-1]
    if leaf in _IGNORED_NAMES:
        return True
    return any(leaf.endswith(suffix) for suffix in _IGNORED_SUFFIXES)


def read_skill_bundle(skill_dir: str | os.PathLike[str]) -> dict[str, bytes]:
    root = Path(skill_dir)
    if not root.is_dir():
        return {}

    bundle: dict[str, bytes] = {}
    for rel_path in list_skill_bundle_paths(root):
        path = root / Path(rel_path)
        bundle[rel_path] = path.read_bytes()
    return bundle


def list_skill_bundle_paths(skill_dir: str | os.PathLike[str]) -> list[str]:
    root = Path(skill_dir)
    if not root.is_dir():
        return []

    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root).as_posix()
        if is_ignored_bundle_rel_path(rel_path):
            continue
        paths.append(rel_path)
    return paths


def coerce_skill_bundle(bundle_files: Mapping[str, bytes | bytearray | str]) -> dict[str, bytes]:
    bundle: dict[str, bytes] = {}
    for raw_rel_path, raw_data in bundle_files.items():
        rel_path = normalize_bundle_rel_path(raw_rel_path)
        if is_ignored_bundle_rel_path(rel_path):
            continue
        bundle[rel_path] = _coerce_bytes(raw_data)
    return bundle


def bundle_file_records(bundle_files: Mapping[str, bytes | bytearray | str]) -> list[dict[str, int | str]]:
    records: list[dict[str, int | str]] = []
    for rel_path, raw_data in sorted(coerce_skill_bundle(bundle_files).items()):
        data = _coerce_bytes(raw_data)
        records.append(
            {
                "path": rel_path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return records


def bundle_tree_sha256(bundle_files: Mapping[str, bytes | bytearray | str]) -> str:
    digest = hashlib.sha256()
    for record in bundle_file_records(bundle_files):
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_skill_bundle_with_meta(
    skill_dir: str | os.PathLike[str],
) -> tuple[dict[str, bytes], list[dict[str, int | str]], str]:
    bundle = read_skill_bundle(skill_dir)
    records = bundle_file_records(bundle)
    tree_sha = bundle_tree_sha256(bundle)
    return bundle, records, tree_sha


def bundle_entrypoint_bytes(
    bundle_files: Mapping[str, bytes | bytearray | str],
    entrypoint: str = _BUNDLE_ENTRYPOINT,
) -> bytes:
    bundle = coerce_skill_bundle(bundle_files)
    key = normalize_bundle_rel_path(entrypoint)
    if key not in bundle:
        raise SkillBundleError(f"Skill bundle is missing required entrypoint {key}")
    return bundle[key]


def bundle_entrypoint_text(
    bundle_files: Mapping[str, bytes | bytearray | str],
    entrypoint: str = _BUNDLE_ENTRYPOINT,
) -> str:
    return bundle_entrypoint_bytes(bundle_files, entrypoint).decode("utf-8")


def write_skill_bundle(
    skill_dir: str | os.PathLike[str],
    bundle_files: Mapping[str, bytes | bytearray | str],
    *,
    clean: bool = False,
) -> None:
    root = Path(skill_dir)
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    for rel_path, data in sorted(coerce_skill_bundle(bundle_files).items()):
        path = root / Path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def bundle_has_only_entrypoint(
    bundle_files: Mapping[str, bytes | bytearray | str],
    entrypoint: str = _BUNDLE_ENTRYPOINT,
) -> bool:
    bundle = coerce_skill_bundle(bundle_files)
    return set(bundle.keys()) == {normalize_bundle_rel_path(entrypoint)}


def bundle_paths(bundle_files: Mapping[str, bytes | bytearray | str] | Iterable[str]) -> list[str]:
    if isinstance(bundle_files, Mapping):
        paths = bundle_files.keys()
    else:
        paths = bundle_files
    out: list[str] = []
    for rel_path in paths:
        clean = normalize_bundle_rel_path(str(rel_path))
        if is_ignored_bundle_rel_path(clean):
            continue
        out.append(clean)
    return sorted(set(out))


def encode_bundle_payload(
    bundle_files: Mapping[str, bytes | bytearray | str],
) -> dict[str, object]:
    """Encode a complete bundle as a JSON-safe, self-verifying payload."""
    bundle = coerce_skill_bundle(bundle_files)
    files: list[dict[str, object]] = []
    for rel_path, data in sorted(bundle.items()):
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        files.append(
            {
                "path": rel_path,
                "encoding": encoding,
                "content": content,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return {
        "format": "bundle_v1",
        "entrypoint": _BUNDLE_ENTRYPOINT,
        "tree_sha256": bundle_tree_sha256(bundle),
        "files": files,
    }


def decode_bundle_payload(payload: Mapping[str, object]) -> dict[str, bytes]:
    """Decode and verify a ``bundle_v1`` payload."""
    if str(payload.get("format") or "") != "bundle_v1":
        raise SkillBundleError("Unsupported bundle payload format")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise SkillBundleError("Bundle payload files must be a list")

    bundle: dict[str, bytes] = {}
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise SkillBundleError("Bundle file entry must be an object")
        rel_path = normalize_bundle_rel_path(str(item.get("path") or ""))
        if rel_path in bundle:
            raise SkillBundleError(f"Duplicate bundle path: {rel_path}")
        encoding = str(item.get("encoding") or "utf-8").lower()
        content = item.get("content")
        if not isinstance(content, str):
            raise SkillBundleError(f"Bundle content must be text: {rel_path}")
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SkillBundleError(
                    f"Invalid base64 bundle content: {rel_path}"
                ) from exc
        else:
            raise SkillBundleError(
                f"Unsupported bundle encoding {encoding!r}: {rel_path}"
            )
        declared_size = item.get("size")
        if declared_size is not None and int(declared_size) != len(data):
            raise SkillBundleError(f"Bundle size mismatch: {rel_path}")
        declared_sha = str(item.get("sha256") or "")
        actual_sha = hashlib.sha256(data).hexdigest()
        if declared_sha and declared_sha != actual_sha:
            raise SkillBundleError(f"Bundle hash mismatch: {rel_path}")
        bundle[rel_path] = data

    declared_tree = str(payload.get("tree_sha256") or "")
    actual_tree = bundle_tree_sha256(bundle)
    if declared_tree and declared_tree != actual_tree:
        raise SkillBundleError("Bundle tree hash mismatch")
    return bundle


def candidate_skill_bundle(skill: Mapping[str, object]) -> dict[str, bytes]:
    """Return a candidate's full bundle, accepting canonical and legacy jobs."""
    raw_bundle = skill.get("bundle")
    if isinstance(raw_bundle, Mapping):
        bundle = decode_bundle_payload(raw_bundle)
    else:
        legacy = skill.get("bundle_files")
        bundle = (
            coerce_skill_bundle(legacy)
            if isinstance(legacy, Mapping)
            else {}
        )

    from .render import build_skill_md

    bundle[_BUNDLE_ENTRYPOINT] = build_skill_md(dict(skill)).encode("utf-8")
    return coerce_skill_bundle(bundle)


def attach_bundle_payload(
    skill: Mapping[str, object],
    bundle_files: Mapping[str, bytes | bytearray | str],
    *,
    file_changes: Iterable[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Return a copy of *skill* carrying a canonical complete bundle."""
    payload = dict(skill)
    payload.pop("bundle_files", None)
    bundle = coerce_skill_bundle(bundle_files)
    from .render import build_skill_md

    bundle[_BUNDLE_ENTRYPOINT] = build_skill_md(payload).encode("utf-8")
    payload["bundle"] = encode_bundle_payload(bundle)
    if file_changes is not None:
        payload["file_changes"] = [dict(item) for item in file_changes]
    return payload


def diff_skill_bundles(
    before: Mapping[str, bytes | bytearray | str],
    after: Mapping[str, bytes | bytearray | str],
) -> dict[str, object]:
    """Build a deterministic file-level diff without exposing binary content."""
    old = coerce_skill_bundle(before)
    new = coerce_skill_bundle(after)
    files: list[dict[str, object]] = []
    for rel_path in sorted(set(old) | set(new)):
        old_data = old.get(rel_path)
        new_data = new.get(rel_path)
        if old_data is None:
            status = "added"
        elif new_data is None:
            status = "deleted"
        elif old_data == new_data:
            status = "unchanged"
        else:
            status = "modified"
        record: dict[str, object] = {
            "path": rel_path,
            "status": status,
            "old_sha256": hashlib.sha256(old_data).hexdigest() if old_data is not None else "",
            "new_sha256": hashlib.sha256(new_data).hexdigest() if new_data is not None else "",
            "old_size": len(old_data) if old_data is not None else 0,
            "new_size": len(new_data) if new_data is not None else 0,
        }
        try:
            old_text = old_data.decode("utf-8").splitlines() if old_data is not None else []
            new_text = new_data.decode("utf-8").splitlines() if new_data is not None else []
        except UnicodeDecodeError:
            record["is_text"] = False
        else:
            record["is_text"] = True
            if status != "unchanged":
                record["diff"] = "\n".join(
                    difflib.unified_diff(
                        old_text,
                        new_text,
                        fromfile=f"current/{rel_path}",
                        tofile=f"candidate/{rel_path}",
                        lineterm="",
                    )
                )
        files.append(record)
    return {
        "before_tree_sha256": bundle_tree_sha256(old),
        "after_tree_sha256": bundle_tree_sha256(new),
        "changed_count": sum(
            1 for item in files if item["status"] != "unchanged"
        ),
        "files": files,
    }
