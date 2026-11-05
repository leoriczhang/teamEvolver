# ruff: noqa: F401
"""Regressions for the compiler contract that OV receives, without model or OV calls."""

import ast
import hashlib
import json
from copy import deepcopy

import pytest

from team_miner.skill_miner import benchmark_format
from team_miner.skill_miner.compile_strategy import (
    COMPILE_CONTRACT,
    COMPILE_PLAN,
    DEFAULT_MINING_POLICY,
    MINING_POLICY_SCHEMA,
    MULTI_COMPILE_PLAN,
    STAGE_RULES,
    TE_STAGED_COMPILE,
    compiler_business_policy,
    compiler_execution_guidance,
    mining_policy_block,
    multi_compile_contract,
    multi_compiler_execution_guidance,
    render_single_stage_compiler,
    stage_compiler_body,
)
from team_miner.skill_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY, OvSkillMiningRepository
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from team_miner.skill_miner.settings import compiler_prompt
from team_skills.library.frontmatter import parse_skill_md_text
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_document


def embedded_contract(body):
    """Read the JSON actually embedded in a child Skill or frozen legacy Skill."""
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
