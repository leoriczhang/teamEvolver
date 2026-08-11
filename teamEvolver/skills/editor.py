"""Local skill library editing: create, update, delete, and bundle files.

Pure on-disk operations over the configured ``skills_dir`` used by the skill
management UI. Cloud sync is layered on top by the service admin routes; this
module never touches object storage so it stays trivially testable.

All operations build on the shared :mod:`teamEvolver.skills.bundle`,
:mod:`teamEvolver.skills.frontmatter` and :mod:`teamEvolver.skills.layout` helpers
so the on-disk representation never diverges from what the manager loads and the
hub syncs.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import yaml

from . import frontmatter, layout
from .bundle import (
    SkillBundleError,
    coerce_skill_bundle,
    is_ignored_bundle_rel_path,
    list_skill_bundle_paths,
    normalize_bundle_rel_path,
    write_skill_bundle,
)

# Skill folder names double as object-store keys and shell paths, so keep them
# to a conservative, traversal-safe charset.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_ENTRYPOINT = "SKILL.md"


class SkillEditorError(ValueError):
    """Raised when a skill edit request is malformed or unsafe."""


# ---------------------------------------------------------------------------- #
# Name / lookup helpers                                                         #
# ---------------------------------------------------------------------------- #


def validate_skill_name(name: str) -> str:
    """Return a normalized skill name or raise on an unsafe value."""
    value = str(name or "").strip()
    if not value:
        raise SkillEditorError("Skill name must not be empty")
    if not _SKILL_NAME_RE.match(value):
        raise SkillEditorError(
            f"Invalid skill name {name!r}: use letters, digits, '.', '-' or '_' only"
        )
    return value


def find_skill_dir(skills_dir: str, name: str) -> Optional[str]:
    """Return the on-disk directory of *name*, or ``None`` when absent.

    Handles both the flat and hermes (category-nested) layouts by matching the
    basename of every discovered ``SKILL.md`` parent directory.
    """
    target = validate_skill_name(name)
    for path in layout.skill_md_paths(skills_dir):
        skill_dir = os.path.dirname(path)
        if os.path.basename(skill_dir) == target:
            return skill_dir
    return None


# ---------------------------------------------------------------------------- #
# SKILL.md construction                                                         #
# ---------------------------------------------------------------------------- #


def build_skill_md(
    name: str,
    description: str,
    category: str,
    body: str,
    extra_frontmatter: Optional[Mapping[str, Any]] = None,
) -> str:
    """Compose a ``SKILL.md`` document from its parts.

    Extra frontmatter fields (everything outside name/description/category) are
    preserved verbatim so round-tripping an edited skill never drops metadata.
    """
    fm: dict[str, Any] = {"name": name, "description": description}
    cat = str(category or "").strip()
    if cat and cat != "general":
        fm["category"] = cat
    for key, value in (extra_frontmatter or {}).items():
        if key in {"name", "description", "category"}:
            continue
        fm[key] = value

    fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm_text}\n---\n\n{str(body or '').strip()}\n"


# ---------------------------------------------------------------------------- #
# Introspection                                                                 #
# ---------------------------------------------------------------------------- #


def _skill_summary(skills_dir: str, skill_md_path: str) -> dict[str, Any]:
    skill_dir = os.path.dirname(skill_md_path)
    parsed = frontmatter.parse_skill_md(skill_md_path) or {}
    try:
        mtime = os.path.getmtime(skill_md_path)
        updated_at = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    except OSError:
        updated_at = ""
    bundle_paths = list_skill_bundle_paths(skill_dir)
    return {
        "name": os.path.basename(skill_dir),
        "description": parsed.get("description", ""),
        "category": parsed.get("category", "general"),
        "files": bundle_paths,
        "file_count": len(bundle_paths),
        "updated_at": updated_at,
    }


def list_skills(skills_dir: str) -> list[dict[str, Any]]:
    """Return summary metadata for every local skill, sorted by name."""
    summaries = [
        _skill_summary(skills_dir, path) for path in layout.skill_md_paths(skills_dir)
    ]
    return sorted(summaries, key=lambda s: s["name"].lower())


def get_skill(skills_dir: str, name: str) -> dict[str, Any]:
    """Return the full editable payload for a single skill."""
    skill_dir = find_skill_dir(skills_dir, name)
    if not skill_dir:
        raise SkillEditorError(f"Skill not found: {name}")
    skill_md_path = os.path.join(skill_dir, _ENTRYPOINT)
    parsed = frontmatter.parse_skill_md(skill_md_path) or {}
    summary = _skill_summary(skills_dir, skill_md_path)
    return {
        **summary,
        "body": parsed.get("content", ""),
        "extra_frontmatter": parsed.get("_extra_frontmatter", {}),
        "skill_md": open(skill_md_path, encoding="utf-8").read(),
    }


# ---------------------------------------------------------------------------- #
# Mutations                                                                     #
# ---------------------------------------------------------------------------- #


def save_skill(
    skills_dir: str,
    name: str,
    description: str,
    category: str,
    body: str,
    *,
    skill_md: str = "",
) -> dict[str, Any]:
    """Create or overwrite a skill's ``SKILL.md``.

    Existing bundle files (``scripts/``, ``references/`` …) are left untouched.
    When *skill_md* is provided it is written verbatim (advanced/raw edit);
    otherwise the document is composed from the structured fields, preserving
    any extra frontmatter already on disk.
    """
    name = validate_skill_name(name)
    existing_dir = find_skill_dir(skills_dir, name)
    created = existing_dir is None

    if skill_md.strip():
        content = skill_md if skill_md.endswith("\n") else skill_md + "\n"
    else:
        if not str(description or "").strip():
            raise SkillEditorError("Skill description must not be empty")
        extra: dict[str, Any] = {}
        if existing_dir:
            parsed = frontmatter.parse_skill_md(os.path.join(existing_dir, _ENTRYPOINT))
            extra = (parsed or {}).get("_extra_frontmatter", {}) or {}
        content = build_skill_md(name, description, category, body, extra)

    target_dir = existing_dir or layout.skill_dir_for(skills_dir, name, category)
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, _ENTRYPOINT), "w", encoding="utf-8") as f:
        f.write(content)

    return {"name": name, "created": created, "dir": target_dir}


def delete_skill(skills_dir: str, name: str) -> dict[str, Any]:
    """Remove a skill directory from disk."""
    skill_dir = find_skill_dir(skills_dir, name)
    if not skill_dir:
        raise SkillEditorError(f"Skill not found: {name}")
    shutil.rmtree(skill_dir)
    return {"name": validate_skill_name(name), "deleted": True}


def add_bundle_files(
    skills_dir: str,
    name: str,
    files: Mapping[str, bytes],
) -> dict[str, Any]:
    """Add or replace bundle files under an existing skill.

    ``SKILL.md`` cannot be written through this path — use :func:`save_skill`.
    """
    skill_dir = find_skill_dir(skills_dir, name)
    if not skill_dir:
        raise SkillEditorError(f"Skill not found: {name}")

    written: list[str] = []
    for raw_rel, data in files.items():
        rel = normalize_bundle_rel_path(raw_rel)
        if rel == _ENTRYPOINT:
            raise SkillEditorError("Use the editor to change SKILL.md, not file upload")
        if is_ignored_bundle_rel_path(rel):
            continue
        dest = os.path.join(skill_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(bytes(data))
        written.append(rel)
    return {"name": validate_skill_name(name), "written": sorted(written)}


def delete_bundle_file(skills_dir: str, name: str, rel_path: str) -> dict[str, Any]:
    """Delete one bundle file from a skill (never ``SKILL.md``)."""
    skill_dir = find_skill_dir(skills_dir, name)
    if not skill_dir:
        raise SkillEditorError(f"Skill not found: {name}")
    rel = normalize_bundle_rel_path(rel_path)
    if rel == _ENTRYPOINT:
        raise SkillEditorError("SKILL.md cannot be deleted; delete the whole skill instead")
    dest = os.path.join(skill_dir, rel)
    if not os.path.isfile(dest):
        raise SkillEditorError(f"File not found: {rel}")
    os.remove(dest)
    return {"name": validate_skill_name(name), "removed": rel}


# ---------------------------------------------------------------------------- #
# Zip import                                                                    #
# ---------------------------------------------------------------------------- #


def bundle_from_zip(zip_bytes: bytes) -> dict[str, bytes]:
    """Extract a skill bundle from zip *bytes*, stripping a single wrapper dir.

    Returns ``{rel_path: data}`` with ignored/unsafe entries dropped. The caller
    is responsible for verifying a ``SKILL.md`` entrypoint is present.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise SkillEditorError(f"Not a valid zip file: {e}") from e

    raw: dict[str, bytes] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        raw[info.filename.replace("\\", "/")] = zf.read(info)

    if _ENTRYPOINT not in raw:
        top_dirs = {p.split("/", 1)[0] for p in raw if "/" in p}
        if len(top_dirs) == 1:
            top = next(iter(top_dirs)) + "/"
            raw = {p[len(top):]: b for p, b in raw.items() if p.startswith(top)}

    bundle: dict[str, bytes] = {}
    for path, data in raw.items():
        try:
            rel = normalize_bundle_rel_path(path)
        except SkillBundleError:
            continue
        if is_ignored_bundle_rel_path(rel):
            continue
        bundle[rel] = data
    return bundle


def import_zip(
    skills_dir: str,
    zip_bytes: bytes,
    *,
    name_override: str = "",
) -> dict[str, Any]:
    """Import a zipped skill package into the local library.

    The skill name is taken from the uploaded ``SKILL.md`` frontmatter, falling
    back to *name_override*. Any existing skill of the same name is replaced.
    """
    bundle = bundle_from_zip(zip_bytes)
    if _ENTRYPOINT not in bundle:
        raise SkillEditorError("Zip package must contain a SKILL.md at its root")

    parsed = frontmatter._load_frontmatter_from_raw(  # noqa: SLF001 - shared parser
        bundle[_ENTRYPOINT].decode("utf-8", errors="replace")
    )
    fm_name = str((parsed or {}).get("name", "")).strip()
    name = validate_skill_name(name_override or fm_name)
    if not (parsed or {}).get("description"):
        raise SkillEditorError("Uploaded SKILL.md must define name and description")

    category = frontmatter.resolve_category(parsed or {}) or "general"
    existing_dir = find_skill_dir(skills_dir, name)
    created = existing_dir is None
    target_dir = existing_dir or layout.skill_dir_for(skills_dir, name, category)
    write_skill_bundle(target_dir, coerce_skill_bundle(bundle), clean=True)

    return {
        "name": name,
        "created": created,
        "dir": target_dir,
        "files": sorted(coerce_skill_bundle(bundle).keys()),
    }
