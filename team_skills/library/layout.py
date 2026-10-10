"""Flat Skill directory layout helpers."""

from __future__ import annotations

import glob
import os

_ENTRYPOINT = "SKILL.md"


def skill_md_paths(skills_dir: str) -> list[str]:
    """Return sorted ``SKILL.md`` paths under a flat runtime cache."""
    return sorted(glob.glob(os.path.join(skills_dir, "*", _ENTRYPOINT)))


def skill_dir_for(skills_dir: str, skill_name: str, category: str = "general") -> str:
    """Resolve the on-disk directory for *skill_name*."""
    del category
    name = str(skill_name or "unknown").strip() or "unknown"
    return os.path.join(skills_dir, name)
