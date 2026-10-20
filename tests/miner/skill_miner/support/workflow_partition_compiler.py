# ruff: noqa: F401
"""Contract regression checks for the pure grouping/metadata compiler split."""

from __future__ import annotations

import ast
import copy
import json

import pytest

from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.workflow_partition_compiler import (
    METADATA_SCHEMA,
    PARTITION_CAPACITY_SCHEMA,
    PARTITION_SCHEMA,
    render_workflow_metadata_compiler,
    render_workflow_partition_compiler,
)


def _contract(body: str) -> dict:
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])

def _instructions(renderer, base=SKILL_BODY):
    return _contract(renderer(base))["synthesize"]["instructions"]
