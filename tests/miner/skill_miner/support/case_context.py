# ruff: noqa: F401
"""Case narratives stay within source scope while review requirements and frozen requests survive."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.case_context import CASE_NARRATIVE_PROTOCOL, case_history_controls
from team_miner.skill_miner.compile_strategy import render_case_batch_compiler
from team_miner.skill_miner.ov_case_batch_orchestration import _bounded_case_revision
from team_miner.skill_miner.ov_case_group_orchestration import render_case_group_compiler
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchOwner, to_batch
