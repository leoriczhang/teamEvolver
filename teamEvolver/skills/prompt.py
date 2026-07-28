"""Skill injection prompt construction.

Builds the ``<available_skills>`` XML catalog and the ``## Skills (mandatory)``
instruction block injected into the model's system prompt. The full and compact
catalogs share one builder (``include_description`` toggles the token-saving
compact form).
"""

from __future__ import annotations

from typing import Any, Callable

_PREAMBLE_HEAD = "\n\nThe following skills provide specialized instructions for specific tasks."
_PREAMBLE_PATH = (
    "When a skill file references a relative path, resolve it against the skill "
    "directory (parent of SKILL.md / dirname of the path) and use that absolute "
    "path in tool commands."
)


def escape_xml(text: str) -> str:
    """Escape XML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_skills_catalog(
    skills: list[dict[str, Any]],
    location_for: Callable[[dict[str, Any]], str],
    *,
    include_description: bool,
) -> str:
    """Build the ``<available_skills>`` XML catalog.

    When *include_description* is true each entry carries ``<name>``,
    ``<description>`` and ``<location>`` (the full form); otherwise
    ``<description>`` is omitted to save tokens (the compact form).
    """
    if not skills:
        return ""
    match_hint = "description" if include_description else "name"
    lines = [
        _PREAMBLE_HEAD,
        f"Load a skill only when the task matches its {match_hint}.",
        _PREAMBLE_PATH,
        "",
        "<available_skills>",
    ]
    for skill in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape_xml(skill.get('name', ''))}</name>")
        if include_description:
            lines.append(f"    <description>{escape_xml(skill.get('description', ''))}</description>")
        lines.append(f"    <location>{escape_xml(location_for(skill))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def build_skills_section(skills_prompt: str, read_tool_name: str = "read") -> str:
    """Wrap a catalog string with the ``## Skills (mandatory)`` instruction block.

    The ``skill_view`` tool takes a skill *name*; every other read tool reads the
    ``SKILL.md`` at its ``<location>`` — the instructions differ accordingly.
    """
    trimmed = skills_prompt.strip()
    if not trimmed:
        return ""

    rate_limit_note = (
        "- When a skill drives external API writes, assume rate limits: prefer fewer "
        "larger writes, avoid tight one-item loops, serialize bursts when possible, "
        "and respect 429/Retry-After."
    )

    if read_tool_name == "skill_view":
        return "\n".join(
            [
                "## Skills (mandatory)",
                "Before replying: scan <available_skills> <description> entries.",
                "- If exactly one skill clearly applies: call `skill_view` with its <name>, then follow it.",
                "- If multiple could apply: choose the most specific one, then call `skill_view` for that skill only.",
                "- If none clearly apply: do not load any skill.",
                "Constraints: never load more than one skill up front; only load after selecting.",
                rate_limit_note,
                trimmed,
                "",
            ]
        )
    return "\n".join(
        [
            "## Skills (mandatory)",
            "Before replying: scan <available_skills> <description> entries.",
            f"- If exactly one skill clearly applies: read its SKILL.md at "
            f"<location> with `{read_tool_name}`, then follow it.",
            "- If multiple could apply: choose the most specific one, then read/follow it.",
            "- If none clearly apply: do not read any SKILL.md.",
            "Constraints: never read more than one skill up front; only read after selecting.",
            rate_limit_note,
            trimmed,
            "",
        ]
    )


def build_injection_prompt(
    skills: list[dict[str, Any]],
    location_for: Callable[[dict[str, Any]], str],
    *,
    max_chars: int = 30_000,
    read_tool_name: str = "read",
) -> str:
    """Catalog *skills* and wrap them with the mandatory-skills instructions.

    Uses the full catalog (name + description + location) when it fits within
    *max_chars*, else falls back to the compact catalog (name + location).
    Returns an empty string when *skills* is empty.
    """
    if not skills:
        return ""
    full = format_skills_catalog(skills, location_for, include_description=True)
    if len(full) <= max_chars:
        catalog = full
    else:
        catalog = format_skills_catalog(skills, location_for, include_description=False)
    return build_skills_section(catalog, read_tool_name)
