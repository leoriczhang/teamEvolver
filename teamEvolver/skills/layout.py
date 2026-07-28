"""Skill directory layout helpers.

Single source of truth for hermes-root detection, category-based directory
resolution, and ``SKILL.md`` discovery. Both the skill manager (local loading)
and the skill hub (sync) build on these so the two never diverge again.

Two on-disk layouts are supported:

* **flat** (default) — ``<skills_dir>/<name>/SKILL.md``
* **hermes root** (``~/.hermes/skills``) — category-nested,
  ``<skills_dir>/<category>/<name>/SKILL.md``
"""

from __future__ import annotations

import glob
import os

_HERMES_SKILLS_REL = os.path.join(".hermes", "skills")
_ENTRYPOINT = "SKILL.md"


def is_hermes_skill_root(skills_dir: str) -> bool:
    """True when *skills_dir* is the shared ``~/.hermes/skills`` root.

    The hermes root nests each skill under a category directory; every other
    root keeps skills flat one level below ``skills_dir``.
    """
    return os.path.realpath(skills_dir) == os.path.realpath(
        os.path.join(os.path.expanduser("~"), _HERMES_SKILLS_REL)
    )


def skill_md_paths(skills_dir: str) -> list[str]:
    """Return sorted ``SKILL.md`` paths under *skills_dir*.

    Recursive glob for the hermes (category-nested) root, single-level glob
    otherwise.
    """
    if is_hermes_skill_root(skills_dir):
        return sorted(glob.glob(os.path.join(skills_dir, "**", _ENTRYPOINT), recursive=True))
    return sorted(glob.glob(os.path.join(skills_dir, "*", _ENTRYPOINT)))


def skill_dir_for(skills_dir: str, skill_name: str, category: str = "general") -> str:
    """Resolve the on-disk directory for *skill_name* given its *category*.

    Under the hermes root a non-``general`` category adds a category level;
    otherwise skills stay flat.
    """
    name = str(skill_name or "unknown").strip() or "unknown"
    cat = str(category or "general").strip() or "general"
    if is_hermes_skill_root(skills_dir) and cat != "general":
        return os.path.join(skills_dir, cat, name)
    return os.path.join(skills_dir, name)


def category_from_skill_path(skills_dir: str, skill_md_path: str) -> str:
    """Infer a skill's category from its ``SKILL.md`` path under the hermes root.

    Returns ``"general"`` for flat roots or when no category level is present.
    """
    if not is_hermes_skill_root(skills_dir):
        return "general"
    rel = os.path.relpath(skill_md_path, skills_dir)
    parts = rel.split(os.sep)
    if len(parts) >= 3:
        return str(parts[0] or "general")
    return "general"
