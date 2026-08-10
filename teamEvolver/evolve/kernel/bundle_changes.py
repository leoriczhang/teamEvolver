"""Deterministic selection and materialization of evolved bundle files."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable, Mapping

from ...skills.bundle import (
    SkillBundleError,
    attach_bundle_payload,
    candidate_skill_bundle,
    diff_skill_bundles,
    is_ignored_bundle_rel_path,
    normalize_bundle_rel_path,
)


class BundleChangeError(ValueError):
    """Raised when a model-proposed bundle change violates the contract."""


def _normalized_extensions(extensions: Iterable[str]) -> set[str]:
    return {
        f".{str(value or '').strip().lower().lstrip('.')}"
        for value in extensions
        if str(value or "").strip().lstrip(".")
    }


def _validate_editable_path(path: str, extensions: set[str]) -> str:
    try:
        rel_path = normalize_bundle_rel_path(path)
    except SkillBundleError as exc:
        raise BundleChangeError(str(exc)) from exc
    if rel_path == "SKILL.md":
        raise BundleChangeError("SKILL.md must be edited through skill content")
    if is_ignored_bundle_rel_path(rel_path):
        raise BundleChangeError(f"Ignored bundle path cannot be edited: {rel_path}")
    suffix = PurePosixPath(rel_path).suffix.lower()
    if suffix not in extensions:
        raise BundleChangeError(f"Bundle path is not editable: {rel_path}")
    return rel_path


def select_editable_files(
    bundle_files: Mapping[str, bytes],
    *,
    extensions: Iterable[str],
    max_file_bytes: int,
    max_prompt_bytes: int,
    priority_paths: Iterable[str] = (),
) -> dict[str, str]:
    """Select UTF-8 bundle files for the model under deterministic budgets."""
    allowed = _normalized_extensions(extensions)
    priority = {
        str(path or "").strip().replace("\\", "/").lstrip("./")
        for path in priority_paths
        if str(path or "").strip()
    }
    candidates: list[tuple[int, int, str, str]] = []
    for raw_path, data in bundle_files.items():
        try:
            rel_path = _validate_editable_path(raw_path, allowed)
        except BundleChangeError:
            continue
        if len(data) > max(1, int(max_file_bytes)):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        candidates.append(
            (0 if rel_path in priority else 1, len(data), rel_path, text)
        )

    selected: dict[str, str] = {}
    used = 0
    budget = max(1, int(max_prompt_bytes))
    for _priority, size, rel_path, text in sorted(candidates):
        if used + size > budget:
            continue
        selected[rel_path] = text
        used += size
    return selected


def materialize_bundle_changes(
    skill: Mapping[str, object],
    *,
    current_bundle: Mapping[str, bytes],
    file_changes: Iterable[Mapping[str, object]],
    editable_paths: Iterable[str],
    extensions: Iterable[str],
    max_file_bytes: int,
    allow_delete: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    """Apply validated model operations and return a canonical candidate."""
    allowed_extensions = _normalized_extensions(extensions)
    visible = {
        normalize_bundle_rel_path(path)
        for path in editable_paths
    }
    final_bundle = dict(current_bundle)
    audit_changes: list[dict[str, object]] = []
    seen: set[str] = set()

    for raw_change in file_changes:
        if not isinstance(raw_change, Mapping):
            raise BundleChangeError("Each file change must be an object")
        rel_path = _validate_editable_path(
            str(raw_change.get("path") or ""),
            allowed_extensions,
        )
        if rel_path in seen:
            raise BundleChangeError(f"Duplicate file change: {rel_path}")
        seen.add(rel_path)
        operation = str(raw_change.get("operation") or "").strip().lower()
        reason = str(raw_change.get("reason") or "").strip()
        if not reason:
            raise BundleChangeError(f"File change requires a reason: {rel_path}")

        if operation == "upsert":
            if rel_path in final_bundle and rel_path not in visible:
                raise BundleChangeError(
                    f"Existing file was not visible to the model: {rel_path}"
                )
            content = raw_change.get("content")
            if not isinstance(content, str):
                raise BundleChangeError(
                    f"Upsert requires UTF-8 text content: {rel_path}"
                )
            data = content.encode("utf-8")
            if len(data) > max(1, int(max_file_bytes)):
                raise BundleChangeError(f"Changed file exceeds size limit: {rel_path}")
            final_bundle[rel_path] = data
        elif operation == "delete":
            if not allow_delete:
                raise BundleChangeError("Bundle file deletion is disabled")
            if rel_path not in visible or rel_path not in final_bundle:
                raise BundleChangeError(
                    f"Delete target was not visible or does not exist: {rel_path}"
                )
            del final_bundle[rel_path]
        else:
            raise BundleChangeError(
                f"Unsupported file operation {operation!r}: {rel_path}"
            )
        audit_changes.append(
            {
                "path": rel_path,
                "operation": operation,
                "reason": reason,
            }
        )

    # The structured skill fields are authoritative for the entrypoint.
    seed = dict(skill)
    seed.pop("bundle", None)
    seed.pop("bundle_files", None)
    seed.pop("_editable_bundle_files", None)
    final_bundle.update(candidate_skill_bundle(seed))
    candidate = attach_bundle_payload(
        seed,
        final_bundle,
        file_changes=audit_changes,
    )
    return candidate, diff_skill_bundles(current_bundle, final_bundle)
