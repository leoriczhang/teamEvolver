# ruff: noqa: F401
"""Case batches preserve the existing OV file-Map contract and frozen single-case compilers."""

import ast
import hashlib
import json

import pytest

from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY


def embedded_contract(body):
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
