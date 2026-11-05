# ruff: noqa: F401
"""Invalid model bookkeeping retries smaller frozen assignments without accepting partial results."""

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner.direct_workflow_planning import SELECTION_RECOVERY_PROTOCOL
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake, DirectOwner
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.local_scope_pipeline import LocalScopeOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class InvalidSelectionFake(DirectFake):
    def __init__(self, *, failure="missing", threshold=4):
        super().__init__()
        self.failure, self.threshold = failure, threshold
        self.valid_assignments = []

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/plan-" not in request["from"][0]:
            return result
        packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
        assigned = [row["item_id"] for row in packet["derived_controls"]["items"]]
        if len(assigned) <= self.threshold:
            self.valid_assignments.append(assigned)
            return result
        uri = request["to"] + "/selection.json"
        value = json.loads(self.contents[uri])
        origins = value["skills"][0]["derived_from"]
        if self.failure == "missing":
            origins.pop()
        elif self.failure == "duplicate":
            origins.append(origins[0])
        elif self.failure == "oversized":
            value["padding"] = "Z" * 13000
        else:
            self.contents[uri] = "{incomplete JSON"
            return result
        self.contents[uri] = json.dumps(value)
        return result

class RecoveryOwner(LocalScopeOwner):
    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"],
                                   staged_selection_recovery_protocol=SELECTION_RECOVERY_PROTOCOL), entries
