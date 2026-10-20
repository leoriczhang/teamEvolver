# ruff: noqa: F401
"""Real host transport, with synthetic independent public Compile responses only."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.direct_workflow_planning import FrozenPlanningIntegrityError
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import STAGED_PROTOCOL, _json, _package, _read, _save, _sha
from team_miner.skill_miner.ov_workflow_batch_transport import WORKFLOW_BATCH_PROTOCOL, _verified
from team_miner.skill_miner.ov_workflow_batch_transport import _instruction as workflow_instruction
from team_miner.skill_miner.workflow_metadata import (
    OUTPUT_SCHEMA as METADATA_SCHEMA,
)
from team_miner.skill_miner.workflow_metadata import (
    build_workflow_metadata,
    validate_workflow_metadata_manifest,
    validate_workflow_metadata_output,
)
from team_miner.skill_miner.workflow_partition import (
    CAPACITY_SCHEMA,
    OUTPUT_SCHEMA,
    build_workflow_partition,
    restore_workflow_partition,
    validate_workflow_partition_manifest,
    validate_workflow_partition_output,
)
from team_miner.skill_miner.workflow_partition_compiler import (
    render_workflow_metadata_compiler,
    render_workflow_partition_compiler,
)
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchFake
from tests.miner.skill_miner.support.ov_staged_orchestration import Owner

POLICY = {"version": 1, "language": "zh-CN", "basis": "frozen_original_source"}

class WorkflowFake(BatchFake):
    def __init__(self):
        super().__init__()
        self.workflow_batches = []
        self.failures = {}
        self.post_status = 0
        self.workflow_lost_ack = False
        self.foreign_output = False

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if (method != "POST" or path != "/api/v1/compile"
                or "/stages/workflow-batch-" not in request["from"][0]):
            return await super().request(account, method, path, **kwargs)
        if self.post_status:
            raise HTTPException(self.post_status, "Definite workflow request rejection")
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert request["from"][0] in self.directories
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_workflow_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(text) for uri, text in sorted(self.contents.items()) if uri.startswith(prefix)]
            assert 1 <= len(packets) <= 8
            self.workflow_batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
            for packet in packets:
                controls = packet["derived_controls"]
                errors = self.failures.get(packet["output_path"], [])
                failure = errors.pop(0) if errors else ""
                if failure == "missing":
                    self.tasks[task_id]["meta"] = {"pipeline": {"errors": ["Independent Map output missing"]}}
                    continue
                if packet["stage"] == "workflow_partition":
                    value = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                             "rows": [[identity, identity] for identity in controls["assigned_ids"]], "reasons": {}}
                    if failure == "capacity":
                        value = {"schema_version": CAPACITY_SCHEMA,
                                 "assignment_sha256": controls["assignment_sha256"],
                                 "input_ids": controls["assigned_ids"], "reason": "独立方法需更小窗口"}
                    if failure == "invalid":
                        value["rows"] = [["m9999", "m9999"]]
                else:
                    value = {"schema_version": METADATA_SCHEMA,
                             "assignment_sha256": controls["assignment_sha256"],
                             "candidate": {"name": "refund-review", "task": "根据政策复核退款申请",
                                           "trigger": "收到退款申请", "inputs": ["退款申请", "原始政策"],
                                           "outputs": ["复核结论"], "boundary": "仅适用所给政策",
                                           "rationale": "流程完整"}}
                    if failure == "invalid":
                        value["candidate"]["name"] = "Bad Name"
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
            if self.foreign_output:
                self.contents[request["to"] + "/foreign.json"] = "{}"
        if self.workflow_lost_ack:
            self.workflow_lost_ack = False
            raise httpx.ReadTimeout("Accepted Native receipt was lost")
        return {"task_id": task_id}

class WorkflowOwner(Owner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = WorkflowFake()
        self.accepted_slots = []
        self.finished = []
        self.validation_calls = 0

    def _workflow_compiler(self, root, state, stage):
        renderer = (render_workflow_partition_compiler if stage == "workflow_partition"
                    else render_workflow_metadata_compiler)
        return renderer(SKILL_BODY)

    async def _workflow_validate_slot(self, root, state, slot):
        self.validation_calls += 1
        validator = (validate_workflow_partition_manifest if slot["packet"]["stage"] == "workflow_partition"
                     else validate_workflow_metadata_manifest)
        try:
            validator(slot["context"]["manifest"], controls=slot["packet"]["derived_controls"])
        except ValueError as exc:
            raise FrozenPlanningIntegrityError(str(exc)) from exc

    async def _workflow_accept_slot(self, job, root, state, slot, value):
        validator = (validate_workflow_partition_output if slot["packet"]["stage"] == "workflow_partition"
                     else validate_workflow_metadata_output)
        checked = validator(value, slot["context"]["manifest"])
        self.accepted_slots.append(slot["packet"]["output_path"])
        return checked

    async def _workflow_batch_finished(self, job, root, state, ledger):
        assert all(slot["status"] == "valid" for slot in ledger["slots"])
        self.finished.append(copy.deepcopy(ledger))
        state.update(current=None, complete=True)
        return await self._staged_save(job, root, state, status="waiting_human", ov_task_id="")

def record(index):
    ref = "scope-original-" + str(index)
    item = {"item_id": "method-original-" + str(index), "kind": "method", "task": "根据原始政策复核退款",
            "boundary": "仅适用已提交政策", "source_refs": [ref],
            "source_files": ["viking://resources/policy-" + str(index) + ".md"]}
    return {"item": item, "bindings": {ref: [index]}, "ordinals": [index], "unit_id": "unit-" + str(index),
            "leaf_items": [item["item_id"]]}

def slot(index, stage):
    partition = build_workflow_partition([record(index)])
    if stage == "workflow_partition":
        built = partition
    else:
        controls = partition["controls"]
        output = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                  "rows": [[identity, identity] for identity in controls["assigned_ids"]], "reasons": {}}
        group = restore_workflow_partition(output, partition["manifest"])["independent"][0]
        metadata_controls, metadata_manifest = build_workflow_metadata(group)
        built = {"controls": metadata_controls, "manifest": metadata_manifest}
    packet = {"schema_version": "te-staged-compile.packet.v1", "stage": stage,
              "output_path": f"workflow-output-{index:03d}.json", "evidence": [],
              "derived_controls": built["controls"], "output_language_policy": POLICY}
    return {"packet": packet, "context": {"manifest": built["manifest"], "original_index": index},
            "status": "pending", "format_attempts": 0, "error": ""}

async def begin(owner, stage="workflow_partition", count=8, mutate=None):
    job, _ = owner.new()
    job = owner.repo.update_job(job["job_id"], staged_execution_protocol=admission.OPTIMIZED_PROTOCOL)
    root = owner._staged_root(job)
    state = {"protocol": STAGED_PROTOCOL, "round": 1, "phase": stage, "language_policy": POLICY,
             "execution_protocol": admission.OPTIMIZED_PROTOCOL,
             "completed_tasks": 0, "attempted_tasks": 0, "estimated_tasks": 1}
    ledger = {"protocol": WORKFLOW_BATCH_PROTOCOL, "stage": stage, "before": 0, "after": count,
              "slots": [slot(index, stage) for index in range(count)]}
    if mutate:
        mutate(ledger)
    job = await owner._submit_workflow_batch(job, root, state, "workflow-tests/ledger.json", ledger)
    return job, root, _read(root / "state.json")

async def finish(owner, job):
    for _ in range(5):
        if job["status"] in {"waiting_human", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Workflow batch did not terminate within its finite test assignments")
