"""Rendering helpers for ``SKILL.md`` files."""

from __future__ import annotations

from team_skills.library.frontmatter import render_skill_md


def build_skill_md(skill: dict) -> str:
    """Render a skill dictionary into a SKILL.md document."""
    return render_skill_md(
        name=skill.get("name", "unknown"),
        description=skill.get("description", ""),
        category=skill.get("category", "general"),
        content=skill.get("content", ""),
        extra_frontmatter=(
            skill.get("extra_frontmatter")
            if isinstance(skill.get("extra_frontmatter"), dict)
            else None
        ),
        metadata=(
            skill.get("metadata")
            if isinstance(skill.get("metadata"), dict)
            else None
        ),
    )
