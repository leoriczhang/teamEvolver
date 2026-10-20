# ruff: noqa: F401
"""Fresh local routing remains lossless and reusable through the full two-round host pipeline."""

from __future__ import annotations

import asyncio

from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from tests.miner.skill_miner.support.direct_workflow_planning import DirectOwner
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class LocalScopeOwner(DirectOwner):
    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"], staged_source_scope_protocol=SOURCE_SCOPE_PROTOCOL_V3), entries
