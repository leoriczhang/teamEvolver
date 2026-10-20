# ruff: noqa: F401
"""Grouped output changes transport, never the twelve individually validated Case contract."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import mining_policy_block, render_case_batch_compiler
from team_miner.skill_miner.ov_case_group_orchestration import (
    CASE_GROUP_CAPACITY_SCHEMA,
    CASE_GROUP_COMPILER_STAGE,
    CASE_GROUP_PROTOCOL,
    CASE_GROUP_SCHEMA,
    MAX_GROUP_OUTPUT_CHARS,
    decode_group,
    render_case_group_compiler,
    verified_group_batch,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _sha
from tests.miner.skill_miner.fixtures import build_document
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.local_scope_pipeline import LocalScopeOwner
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchFake, BatchOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive, start
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class GroupFake(BatchFake):
    def __init__(self):
        super().__init__()
        self.group_batches = []
        self.group_break_slots = {}
        self.case_capacity_above = 4
        self.lose_group_ack = False
        self.group_same_key_503 = False
        self.group_extra = False

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/case_batch-" not in request["from"][0]:
            return await super().request(account, method, path, **kwargs)
        prefix = request["from"][0] + "/"
        packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents)
                   if uri.startswith(prefix) and not uri.rsplit("/", 1)[-1].startswith(".")]
        if not packets or "cases" not in packets[0]["derived_controls"]:
            return await super().request(account, method, path, **kwargs)
        if len(request["from"]) != 1 or request["from"][0] not in self.directories:
            raise HTTPException(422, "Compile source must be a directory")
        assert 1 <= len(packets) <= 8
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if self.group_same_key_503:
            self.group_same_key_503 = False
            raise HTTPException(503, "Temporary public admission failure")
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_group_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            self.group_batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed",
                                   "meta": {"pipeline": {"warnings": ["Partial output"], "errors": []}}}
            for packet in packets:
                controls = packet["derived_controls"]
                assignments = controls["cases"]
                if len(assignments) > self.case_capacity_above:
                    value = {"schema_version": CASE_GROUP_CAPACITY_SCHEMA,
                             "case_ids": [row["case_id"] for row in assignments],
                             "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in packet["evidence"])),
                             "reason": "Complete assignments require smaller output groups."}
                else:
                    value = {"schema_version": CASE_GROUP_SCHEMA, "cases": [], "deferred": []}
                    for own in assignments:
                        index = planning.CASE_DIMENSIONS.index(own["coverage_dimension"])
                        failures = self.group_break_slots.get(index, [])
                        failure = failures.pop(0) if failures else ""
                        if failure == "missing":
                            continue
                        case = build_document(controls["skill_name"], [{"query": "Fixture"}])["cases"][0]
                        case["case_id"] = own["case_id"]
                        case["query"] = "根据原文执行不同验证场景 " + own["coverage_dimension"] + own["case_id"]
                        case["provenance"]["session_ids"] = own["allowed_source_refs"]
                        if failure == "invalid":
                            case["checks"] = case["checks"][:1]
                        elif failure == "foreign":
                            case["provenance"]["session_ids"] = ["viking://unassigned/foreign.md"]
                        elif failure == "cross":
                            case["provenance"]["session_ids"] = assignments[-1]["allowed_source_refs"]
                        elif failure == "duplicate-query":
                            case["query"] = value["cases"][0]["query"] if value["cases"] else "Identical query"
                        elif failure == "materials-object":
                            case["materials"] = {"scenario": "原文中的场景", "source_dialogue": "原文中的对话"}
                        elif failure == "materials-text":
                            case["materials"] = "  原文中的场景\n原文中的对话：保留标点和换行。\n"
                        if failure == "deferred":
                            value["deferred"].append({"case_id": own["case_id"], "reason": "原文缺完整条件",
                                                      "source_refs": own["allowed_source_refs"]})
                        else:
                            value["cases"].append(case)
                            if failure == "duplicate-id":
                                value["cases"].append(copy.deepcopy(case))
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
            if self.group_extra:
                self.contents[request["to"] + "/unexpected.json"] = "{}"
        if self.lose_group_ack:
            self.lose_group_ack = False
            raise httpx.ReadTimeout("Group accepted before ACK was lost")
        return {"task_id": task_id}

class GroupFlowFake(DirectFake, GroupFake):
    pass

class GroupOwner(LocalScopeOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = GroupFlowFake()

    def new(self, count=8):
        job, entries = super().new(count)
        for index, entry in enumerate(entries):
            text = f"Procedure {index}: collect the request, verify the supplied policy and explain the decision.\n"
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return self.repo.update_job(job["job_id"], staged_case_group_protocol=CASE_GROUP_PROTOCOL), entries

async def to_group(owner, count=8):
    job = await begin(owner, count)
    for _ in range(500):
        root, state = await owner._staged_load(job)
        if (state.get("current") or {}).get("input_protocol") == CASE_GROUP_PROTOCOL:
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("No Case group was submitted")

def assigned_indices(owner):
    return [planning.CASE_DIMENSIONS.index(own["coverage_dimension"])
            for batch in owner.ov.group_batches for packet in batch for own in packet["derived_controls"]["cases"]]
