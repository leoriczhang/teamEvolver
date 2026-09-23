from __future__ import annotations

import pytest

from team_skills.candidates.static_checks import validate_candidate_bundle
from team_skills.evolution.kernel.helpers import build_skill_md as evolution_render
from team_skills.library.editor import build_skill_md as editor_render
from team_skills.library.frontmatter import (
    render_skill_md,
    split_skill_md,
    validate_skill_md,
)
from team_skills.library.render import build_skill_md


def test_long_description_stays_on_one_physical_line() -> None:
    description = "包含冒号: 引号 ' \" # " + ("很长的说明 " * 200)
    rendered = render_skill_md(
        name="long-description",
        description=description,
        content="# Body",
    )

    header, body = split_skill_md(rendered, strict=True) or ("", "")
    description_lines = [
        line for line in header.splitlines() if line.startswith("description:")
    ]

    assert len(description_lines) == 1
    assert len(description_lines[0]) > 80
    assert body == "# Body"
    assert validate_skill_md(rendered)["frontmatter"]["description"] == " ".join(
        description.split()
    )


def test_embedded_frontmatter_is_removed_and_metadata_is_preserved() -> None:
    rendered = render_skill_md(
        name="canonical",
        description="new description",
        category="ops",
        content=(
            "---\n"
            "name: old\n"
            "description: old\n"
            "metadata:\n"
            "  owner: team-a\n"
            "---\n\n"
            "# Body\n"
        ),
        extra_frontmatter={"portable": True},
    )
    parsed = validate_skill_md(rendered, expected_name="canonical")

    assert parsed["body"] == "# Body"
    assert parsed["frontmatter"]["metadata"] == {"owner": "team-a"}
    assert parsed["frontmatter"]["portable"] is True
    assert rendered.count("\n---\n") == 1


def test_malformed_or_double_frontmatter_is_rejected() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        render_skill_md(
            name="broken",
            description="test",
            content="---\nname: nested\n",
        )
    with pytest.raises(ValueError, match="second"):
        render_skill_md(
            name="broken",
            description="test",
            content=(
                "---\nname: first\ndescription: first\n---\n"
                "---\nname: second\ndescription: second\n---\n"
            ),
        )


def test_all_rendering_entrypoints_use_the_same_format() -> None:
    skill = {
        "name": "same-renderer",
        "description": "same",
        "category": "general",
        "content": "# Body",
        "extra_frontmatter": {"supported_runtimes": ["deap"]},
    }

    canonical = build_skill_md(skill)

    assert evolution_render(skill) == canonical
    assert (
        editor_render(
            skill["name"],
            skill["description"],
            skill["category"],
            skill["content"],
            skill["extra_frontmatter"],
        )
        == canonical
    )


def test_static_validation_rejects_bad_skill_frontmatter() -> None:
    result = validate_candidate_bundle(
        {
            "name": "",
            "description": "missing name",
            "content": "# Body",
        }
    )

    assert result["passed"] is False
    assert result["checks"][0]["checker"] == "frontmatter_contract"
