# ruff: noqa: F401
"""Independent artifact batches preserve public Compile and custom business policy."""

import ast
import json

import pytest

from team_miner.skill_miner.compile_strategy import (
    MULTI_COMPILE_PLAN,
    STAGE_RULES,
    mining_policy_block,
    render_stage_batch_compiler,
    stage_compiler_body,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
