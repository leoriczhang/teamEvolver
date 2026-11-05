# ruff: noqa: F401
"""Artifact language follows original prose, not code or previous generated output."""

from __future__ import annotations

import pytest

from team_miner.skill_miner import output_language
from team_miner.skill_miner.output_language import (
    output_language_instruction,
    resolve_output_language_policy,
    validate_artifact_headings,
)


def source(tmp_path, content, name="policy.md", kind="source"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return {"kind": kind, "relative_path": f"original/{name}", "local_path": str(path)}
