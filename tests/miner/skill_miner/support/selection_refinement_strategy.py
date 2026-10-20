# ruff: noqa: F401
"""Future R2 metadata corrections retain identity through the original OV file Map."""

import ast
import hashlib
import json

import pytest

from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE, output_language_instruction
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY


def embedded_contract(body):
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])

def instructions(body):
    return embedded_contract(body)["synthesize"]["instructions"]

def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()
