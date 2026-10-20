# ruff: noqa: F401
"""Durable grouping-to-metadata projections keep every original method's scope."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.direct_workflow_planning import FrozenPlanningIntegrityError
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _sha, _write
from team_miner.skill_miner.ov_workflow_planning import OvWorkflowPlanningMixin, _assignment, _feedback
from team_miner.skill_miner.workflow_partition import CAPACITY_SCHEMA, OUTPUT_SCHEMA
from team_miner.skill_miner.workflow_partition_compiler import METADATA_STAGE, PARTITION_STAGE
from tests.miner.skill_miner.support.workflow_metadata import _candidate, _record


class Host(OvWorkflowPlanningMixin):
    def __init__(self):
        self.submissions = []

    async def _ensure_aux_compiler(self, root, state, stage, body):
        state.setdefault("aux_compilers", {})[stage] = _sha(body)
        _write(root / "aux-compilers" / (stage + ".md"), body)

    async def _staged_save(self, job, root, state, **changes):
        _save(root / "state.json", state)
        return {**job, **changes}

    async def _submit_workflow_batch(self, job, root, state, path, ledger):
        self.submissions.append(copy.deepcopy(ledger))
        return job

    async def _staged_selection_complete(self, job, root, state):
        state["phase"] = "skill_section"
        return job

async def _entered(tmp_path, count=4, *, mining_intent=None):
    host = Host()
    records = [_record(i) for i in range(count)]
    for i, record in enumerate(records):
        _save(tmp_path / "catalog/method" / f"{i:08d}.json", record)
        for uri in record["item"]["source_files"]:
            _save(tmp_path / "source-files" / (_sha(uri) + ".json"), {"ordinals": record["ordinals"]})
    state = {"language_policy": {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"},
             "round": 1, "base_sha256": _sha(SKILL_BODY),
             "catalog_counts": {"method": count}, "selection_cursor": 0, "workflow_partition_protocol":
                 "te-workflow-partition.v2"}
    job = {"job_id": "workflow-test", "current_round": 1}
    if mining_intent is not None:
        state["mining_intent"] = mining_intent
        job["mining_intent"] = mining_intent
    _write(tmp_path / "compiler-base.md", SKILL_BODY)
    await host._workflow_enter(job, tmp_path, state)
    return host, job, state, records

def _partition_value(slot, *, group=True, capacity=False):
    controls = slot["packet"]["derived_controls"]
    ids = controls["assigned_ids"]
    base = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"]}
    if capacity:
        return {**base, "schema_version": CAPACITY_SCHEMA, "input_ids": ids, "reason": "完整说明需要较小输入范围"}
    grouped = group and len(ids) > 1
    return {**base, "rows": [[identity, ids[-1] if grouped else identity] for identity in ids],
            "reasons": {ids[-1]: "完整流程包含所列步骤"} if grouped else {}}

async def _accept(host, job, root, state, ledger, values):
    for slot, value in zip(ledger["slots"], values, strict=True):
        slot["acceptance"] = await host._workflow_accept_slot(job, root, state, slot, value)
        slot["status"] = "valid"
    await host._workflow_batch_finished(job, root, state, ledger)
