# ruff: noqa: F401
"""Fresh grouping and metadata traverse real TE orchestration with inert model results."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.workflow_metadata import OUTPUT_SCHEMA as METADATA_SCHEMA
from team_miner.skill_miner.workflow_partition import OUTPUT_SCHEMA as PARTITION_SCHEMA
from team_miner.skill_miner.workflow_partition import PROTOCOL
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.local_scope_pipeline import LocalScopeOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class WorkflowFake(DirectFake):
    """Only Native results are synthetic; frozen packets, scopes and acceptance are real."""

    def __init__(self):
        super().__init__()
        self.workflow_batches = []

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if (method != "POST" or path != "/api/v1/compile"
                or "/stages/workflow-batch-" not in request["from"][0]):
            return await super().request(account, method, path, **kwargs)
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert len(request["from"]) == 1 and request["from"][0] in self.directories
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
            return {"task_id": task_id}
        task_id = "cmp_workflow_" + str(len(self.accepted))
        self.accepted[key] = (task_id, copy.deepcopy(request))
        prefix = request["from"][0] + "/"
        packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents) if uri.startswith(prefix)]
        assert 1 <= len(packets) <= 8
        assert len({packet["output_path"] for packet in packets}) == len(packets)
        self.workflow_batches.append(copy.deepcopy(packets))
        for packet in packets:
            controls = packet["derived_controls"]
            if packet["stage"] == "workflow_partition":
                ids = controls["assigned_ids"]
                value = {
                    "schema_version": PARTITION_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                    "rows": [[identity, ids[-1]] for identity in ids],
                    "reasons": {ids[-1]: "同一完整复核流程包含所列条件与步骤"} if len(ids) > 1 else {},
                }
            else:
                assert packet["stage"] == "workflow_metadata"
                revision = json.loads(packet["revision_context"] or "{}")
                revised = bool(revision.get("feedback"))
                value = {
                    "schema_version": METADATA_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                    "candidate": {"name": "refund-review", "task": "退款复核",
                                  "trigger": "收到用户退款申请", "inputs": ["原始申请和政策条件"],
                                  "outputs": ["复核结论和未解决条件"],
                                  "boundary": "只按提供的原文；保留人工核实步骤" if revised else "只按提供的原文",
                                  "rationale": "原文支持完整独立复核方法"},
                }
            self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
        self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
        return {"task_id": task_id}

class WorkflowOwner(LocalScopeOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = WorkflowFake()

    def new(self, count=5):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"], staged_workflow_partition_protocol=PROTOCOL), entries

async def _first_round(owner):
    job, entries = owner.new(5)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    result = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert result["status"] == "waiting_human", result.get("error")
    return result

def _dataset(owner, job):
    return _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")
