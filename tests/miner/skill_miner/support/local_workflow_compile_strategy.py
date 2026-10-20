# ruff: noqa: F401
"""Fresh workflow contracts retain public Compile, strict source indexes and canonical authority."""

import ast
import copy
import json

import pytest

from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.bounded_compile_plan import INDEX_SCHEMA_V2, validate_index
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from tests.miner.skill_miner.support.compile_strategy import embedded_contract
