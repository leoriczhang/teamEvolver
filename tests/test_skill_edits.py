"""Tests for search/replace skill editing (evolve.kernel.skill_edits)."""

from __future__ import annotations

import json

from teamEvolver.evolve.kernel.skill_edits import (
    apply_skill_edits,
    format_edit_failures,
)


def test_replace_applies_once() -> None:
    result = apply_skill_edits(
        "alpha\nbeta\ngamma",
        [{"operation": "replace", "old_string": "beta", "new_string": "BETA"}],
    )
    assert result.ok
    assert result.content == "alpha\nBETA\ngamma"
    assert result.applied == 1
    assert result.failures == []


def test_replace_not_found_fails_verbatim() -> None:
    result = apply_skill_edits(
        "alpha\nbeta\n",
        [{"operation": "replace", "old_string": "delta", "new_string": "x"}],
    )
    assert not result.ok
    assert result.content == "alpha\nbeta\n"  # untouched
    assert "未在当前正文中找到" in result.failures[0].reason


def test_replace_ambiguous_match_fails() -> None:
    result = apply_skill_edits(
        "ab ab",
        [{"operation": "replace", "old_string": "ab", "new_string": "x"}],
    )
    assert not result.ok
    assert "匹配了 2 次" in result.failures[0].reason


def test_replace_empty_new_string_deletes() -> None:
    result = apply_skill_edits(
        "keep\nremove me\nkeep2",
        [{"operation": "replace", "old_string": "remove me\n", "new_string": ""}],
    )
    assert result.ok
    assert result.content == "keep\nkeep2"


def test_insert_after_inserts_immediately_after_anchor() -> None:
    result = apply_skill_edits(
        "## 标题A\n内容\n",
        [
            {
                "operation": "insert_after",
                "anchor": "## 标题A\n",
                "new_string": "\n新段落\n",
            }
        ],
    )
    assert result.ok
    assert result.content == "## 标题A\n\n新段落\n内容\n"


def test_insert_after_requires_unique_anchor() -> None:
    result = apply_skill_edits(
        "x x",
        [{"operation": "insert_after", "anchor": "x", "new_string": "y"}],
    )
    assert not result.ok
    assert "匹配了 2 次" in result.failures[0].reason


def test_sequential_edits_see_prior_results() -> None:
    result = apply_skill_edits(
        "one two three",
        [
            {"operation": "replace", "old_string": "one", "new_string": "ONE"},
            {"operation": "replace", "old_string": "ONE two", "new_string": "ONE TWO"},
        ],
    )
    assert result.ok
    assert result.content == "ONE TWO three"
    assert result.applied == 2


def test_all_or_nothing_on_failure() -> None:
    """A failing edit rejects the whole batch — no partial application."""
    result = apply_skill_edits(
        "alpha\nbeta\n",
        [
            {"operation": "replace", "old_string": "alpha", "new_string": "ALPHA"},
            {"operation": "replace", "old_string": "missing", "new_string": "x"},
        ],
    )
    assert not result.ok
    assert result.content == "alpha\nbeta\n"
    assert result.applied == 0
    assert len(result.failures) == 1
    assert result.failures[0].index == 1


def test_invalid_edit_shapes_fail() -> None:
    result = apply_skill_edits(
        "body",
        [
            "not-a-dict",
            {"operation": "unknown_op", "old_string": "b", "new_string": "x"},
            {"operation": "replace", "new_string": "x"},  # no old_string
            {"operation": "replace", "old_string": "b", "new_string": 123},
            {"operation": "insert_after", "anchor": "b", "new_string": ""},
        ],
    )
    assert not result.ok
    assert len(result.failures) == 5
    assert all(result.content == "body" for _ in [1])


def test_empty_edits_rejected() -> None:
    result = apply_skill_edits("body", [])
    assert not result.ok
    result = apply_skill_edits("body", "not-a-list")  # type: ignore[arg-type]
    assert not result.ok


def test_escaped_json_anchor_roundtrip() -> None:
    """The llm-wiki regression: anchors with \\" escapes must match byte-exact.

    A hallucinated (unescaped) anchor fails; the verbatim one applies.
    """
    content = (
        "```json\n"
        '{\n  "args": "{\\"operation\\":\\"grep\\"}",\n'
        "```\n"
    )
    # Hypothetical edit that wants to rewrite the args example line.
    target = '"args": "{\\"operation\\":\\"grep\\"}"'
    assert content.count(target) == 1

    hallucinated = apply_skill_edits(
        content,
        [
            {
                "operation": "replace",
                # LLM "normalized" the escapes away — must NOT match.
                "old_string": '"args": "{"operation":"grep"}"',
                "new_string": "x",
            }
        ],
    )
    assert not hallucinated.ok
    assert "未在当前正文中找到" in hallucinated.failures[0].reason

    verbatim = apply_skill_edits(
        content,
        [
            {
                "operation": "replace",
                "old_string": target,
                "new_string": '"args": "{\\"operation\\":\\"read\\"}"',
            }
        ],
    )
    assert verbatim.ok
    assert '\\"read\\"' in verbatim.content
    # Fences and escapes elsewhere untouched.
    assert verbatim.content.startswith("```json\n")
    assert verbatim.content.endswith("```\n")


def test_format_edit_failures_renders_list() -> None:
    result = apply_skill_edits("ab", [{"operation": "replace", "old_string": "zz", "new_string": "y"}])
    rendered = format_edit_failures(result.failures)
    assert "- #1 (replace):" in rendered
    assert "未在当前正文中找到" in rendered


def test_multiline_anchor_with_context_is_unique() -> None:
    """Disambiguating an ambiguous match by extending the anchor with context."""
    content = "step: A\nmark\nstep: B\nmark\n"
    ambiguous = apply_skill_edits(
        content, [{"operation": "replace", "old_string": "mark", "new_string": "X"}]
    )
    assert not ambiguous.ok

    resolved = apply_skill_edits(
        content,
        [{"operation": "replace", "old_string": "step: B\nmark", "new_string": "step: B\nMARK"}],
    )
    assert resolved.ok
    assert resolved.content == "step: A\nmark\nstep: B\nMARK\n"


def test_edit_json_serialization_roundtrip() -> None:
    """An edit list travels inside the LLM JSON envelope; escape levels nest."""
    edits = [
        {
            "operation": "replace",
            "old_string": 'grep "\\"patterns\\""',
            "new_string": "x",
        }
    ]
    envelope = json.dumps({"skill": {"edits": edits}}, ensure_ascii=False)
    parsed = json.loads(envelope)
    result = apply_skill_edits('pre grep "\\"patterns\\"" post', parsed["skill"]["edits"])
    assert result.ok
    assert result.content == "pre x post"
