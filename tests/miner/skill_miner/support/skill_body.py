# ruff: noqa: F401
"""A method keeps one integrated body and an exact, recoverable source-batch journal."""

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import render_stage_batch_compiler
from team_miner.skill_miner.ov_artifact_batch_orchestration import _verify
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.skill_body import PROTOCOL, RULE, iter_integrated_skill, previous_body
from tests.miner.skill_miner.support.artifact_batches import ArtifactOwner
from tests.miner.skill_miner.support.compile_strategy import embedded_contract
from tests.miner.skill_miner.support.ov_staged_orchestration import drive, start


def section(identity, content):
    return {"schema_version": planning.SECTION_SCHEMA, "input_ids": [identity],
            "title": "完整面谈方法", "content": content}
