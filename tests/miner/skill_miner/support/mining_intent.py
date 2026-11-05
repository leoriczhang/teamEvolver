# ruff: noqa: F401
"""User scope is frozen and enforced without count-driven fabrication or extra Compile calls."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner.mining_intent import normalize_intent, select_candidates
from team_miner.skill_miner.workflow_metadata import (
    WorkflowMetadataManifestError,
    WorkflowMetadataOutputError,
    build_workflow_metadata,
    validate_workflow_metadata_manifest,
    validate_workflow_metadata_output,
)
from tests.miner.skill_miner.support.workflow_metadata import _candidate, _group


def assessment(priority=50, *, matches=True, complete=True):
    return {"matches_direction": matches, "complete_for_goal": complete, "priority": priority,
            "reason": "来源支持指定范围内的完整独立方法"}

def plan(names):
    return {"schema_version": "skill-miner.plan.v1", "skills": [
        {"name": name, "task": "完整方法", "source_refs": [f"viking://source/{name}.md"]} for name in names
    ], "deferred": []}
