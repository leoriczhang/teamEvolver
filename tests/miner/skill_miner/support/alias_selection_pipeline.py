# ruff: noqa: F401
"""Merged members follow their survivor through count/direction selection and exact writer routing."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import mining_intent
from team_miner.skill_miner.intent_recovery import ERROR, PROTOCOL, prepare_recovery
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY, utc_now
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.fresh_exact_pipeline import FreshOwner, run
from tests.miner.skill_miner.support.source_unit_extraction import POLICY


def owner_with_excluded_merged_members(root, mode):
    owner = FreshOwner(root, rounds=2 if mode == "single" else 1, mode=mode,
                       direction="完整客户沟通对练方法", target_count=1 if mode == "target" else None)
    owner.ov.partition_groups = 4
    owner.ov.identity_pair_groups = True
    owner.ov.unmatched_second_pair = mode == "auto"
    return owner
