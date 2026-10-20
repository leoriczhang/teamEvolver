# ruff: noqa: F401
"""An empty supplement form advances real rounds without inventing human approval or Evidence."""

import asyncio
import json

import pytest

from team_miner.skill_miner.ov_compile_mining import OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend
from tests.miner.skill_miner.support.host import DomainHost as SkillMinerBridgeMixin
from tests.miner.skill_miner.support.ov_compile_round_repository import complete_round, create_job, populate, question


@pytest.fixture
def repository(tmp_path):
    return OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))

def fresh_job(repository, *, rounds=2):
    job = create_job(repository, rounds=rounds)
    return repository.update_job(job["job_id"], empty_review_policy="te-empty-review-skip.v1")
