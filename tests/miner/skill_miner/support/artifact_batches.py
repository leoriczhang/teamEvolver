# ruff: noqa: F401
"""Independent public file Map outputs and durable single-current artifact batches."""

from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_artifact_batch_orchestration import ARTIFACT_BATCH_STAGE, _ledger
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchFake, BatchOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive, start


class ArtifactFake(BatchFake):
    def __init__(self):
        super().__init__()
        self.artifacts = []
        self.break_artifact = {}
        self.lose_artifact_ack = False
        self.candidate_count = 4

    async def request(self, account, method, path, **kwargs):
        if path.startswith("/api/v1/tasks/"):
            return self.tasks[path.rsplit("/", 1)[-1]]
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/artifact_batch-" not in request["from"][0]:
            return await super().request(account, method, path, **kwargs)
        assert len(request["from"]) == 1 and request["from"][0] in self.directories
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if key not in self.accepted:
            task_id = "cmp_artifact_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents) if uri.startswith(prefix)]
            assert 1 <= len(packets) <= 8
            assert len({packet["output_path"] for packet in packets}) == len(packets)
            self.artifacts.append(copy.deepcopy(packets))
            self.tasks[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "meta": {"pipeline": {"warnings": ["Partial output"], "errors": []}},
            }
            for packet in packets:
                name = packet["derived_controls"]["skill_name"]
                failures = self.break_artifact.get((packet["stage"], name), [])
                failure = failures.pop(0) if failures else ""
                if failure == "missing":
                    continue
                if packet["stage"] == "skill_section":
                    value = {
                        "schema_version": planning.SECTION_SCHEMA,
                        "input_ids": packet["input_ids"],
                        "title": "执行步骤",
                        "content": "根据原始政策复核申请，证据不足时向用户核实。",
                    }
                    if failure == "foreign":
                        value["input_ids"] = ["unknown-source"]
                    if failure == "english_title":
                        value["title"] = "Execution steps"
                    output = json.dumps(value, ensure_ascii=False)
                else:
                    output = "# 评测说明\n按已提供原文验证该候选的独立执行边界。"
                self.contents[request["to"] + "/" + packet["output_path"]] = output
        else:
            task_id, previous = self.accepted[key]
            assert previous == request
        if self.lose_artifact_ack:
            self.lose_artifact_ack = False
            raise httpx.ReadTimeout("Lost artifact acceptance")
        return {"task_id": task_id}

class ArtifactOwner(BatchOwner):
    def __init__(self, root):
        super().__init__(root)
        self.fixture.ov = ArtifactFake()
        self.ov = self.fixture.ov

    def new(self, count=1):
        job, entries = self.fixture.new(count)
        return self.repo.update_job(job["job_id"], staged_execution_protocol=admission.OPTIMIZED_PROTOCOL), entries

async def first_batch(owner):
    job = await start(owner, 8)
    while True:
        root, state = await owner._staged_load(job)
        if state.get("current", {}).get("stage") == ARTIFACT_BATCH_STAGE:
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
