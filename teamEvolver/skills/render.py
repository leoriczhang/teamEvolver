"""Rendering helpers for ``SKILL.md`` files."""

from __future__ import annotations


def build_skill_md(skill: dict) -> str:
    """Render a skill dictionary into a SKILL.md document."""
    name = skill.get("name", "unknown")
    description = skill.get("description", "")
    category = skill.get("category", "general")
    content = skill.get("content", "")

    needs_quoting = any(c in str(description) for c in ":{}[],\"'#&*!|>%@`\n")
    if needs_quoting:
        escaped = str(description).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        desc_line = f'description: "{escaped}"'
    else:
        desc_line = f"description: {description}"

    fm_lines = [f"name: {name}", desc_line, f"category: {category}"]
    extra_fm = skill.get("extra_frontmatter")
    if isinstance(extra_fm, dict):
        import yaml

        for key, value in extra_fm.items():
            if key not in ("name", "description", "category"):
                fm_lines.append(f"{key}: {yaml.dump(value, default_flow_style=True).strip()}")

    return "---\n" + "\n".join(fm_lines) + "\n---\n\n" + str(content) + "\n"
