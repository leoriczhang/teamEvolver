# ruff: noqa: F401
"""A durable user stop wins over stale worker writes without hiding Native acceptance."""

import asyncio
import copy

import pytest

from team_miner.skill_miner.ov_compile_mining import SKILL_BODY, OvSkillMiningRepository
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.ov_staged_orchestration import Owner, drive, start
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs
from tests.miner.skill_miner.support.selection_window_recovery import InvalidSelectionFake, RecoveryOwner


def new_job(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path,
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))
    job = repository.create_job(
        name="stop-race", account="default", source_uri="viking://resources/source",
        source_snapshot_uri="viking://resources/snapshot", output_uri="viking://resources/output",
        document_count=1, max_rounds=2,
    )
    return repository, job
