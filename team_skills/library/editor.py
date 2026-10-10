"""Filesystem helpers for temporary Skill authoring workspaces.

These helpers do not own published Skill state. Service routes stage edits in a
temporary directory, commit the resulting bundle through ``SkillHub``, and may
then materialize it into a rebuildable runtime cache.

All operations build on the shared :mod:`team_skills.library.bundle`,
:mod:`team_skills.library.frontmatter` and :mod:`team_skills.library.layout` helpers
so temporary and materialized bundle layouts match durable storage.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

from team_skills.library import frontmatter, layout

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

    Matches the basename of every discovered ``SKILL.md`` parent directory.
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
    return frontmatter.render_skill_md(
        name=name,
        description=description,
        category=category,
        content=body,
        extra_frontmatter=dict(extra_frontmatter or {}),
    )


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
