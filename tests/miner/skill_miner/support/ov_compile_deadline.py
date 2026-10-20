# ruff: noqa: F401
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from team_miner.skill_miner.ov_compile_mining import OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend
from tests.miner.skill_miner.support.host import DomainHost as SkillMinerBridgeMixin


def job_repo(tmp_path):
    r = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))
    j = r.create_job(
        name="deadline test",
        account="default",
        source_uri="viking://resources/test",
        source_snapshot_uri="viking://resources/test-snapshot",
        output_uri="viking://resources/test-output",
        document_count=3,
        max_rounds=3,
        runtime_timeout_seconds=60,
    )
    j = r.update_job(
        j["job_id"],
        status="running",
        ov_task_id="fake",
        started_at=(datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(),
    )
    return r, j
