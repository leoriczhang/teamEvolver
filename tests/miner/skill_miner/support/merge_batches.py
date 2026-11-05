# ruff: noqa: F401
"""Public directory/file Map merge batches preserve exact method dispositions and recovery."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import (
    mining_policy_block,
    render_merge_batch_compiler,
    stage_compiler_body,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_merge_batch_orchestration import MERGE_BATCH_PROTOCOL, _verified
from team_miner.skill_miner.ov_staged_orchestration import _alias, _freeze_chunks, _read, _save, _sha
from team_miner.skill_miner.workflow_planning import WORKFLOW_PLANNING_PROTOCOL, prepare_workflow_order
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner, begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive


class MergeFake(ExtractFake):
    def __init__(self):
        super().__init__()
        self.merge_batches, self.break_windows = [], {}
        self.merge_post_status = 0
        self.malformed_merge_ack = False

    async def request(self, account, method, path, **kwargs):
        body = kwargs.get("json", {})
        merging = method == "POST" and path == "/api/v1/compile" and "/stages/merge_batch-" in body["from"][0]
        if not merging:
            return await super().request(account, method, path, **kwargs)
        if self.merge_post_status:
            raise HTTPException(self.merge_post_status, "Rejected inert merge submission")
        key = kwargs["extra_headers"]["Idempotency-Key"]
        is_new, lost = key not in self.accepted, None
        try:
            result = await super().request(account, method, path, **kwargs)
        except httpx.ReadTimeout as error:
            result, lost = {"task_id": self.accepted[key][0]}, error
        if is_new:
            packets = [json.loads(text) for uri, text in sorted(self.contents.items())
                       if uri.startswith(body["from"][0] + "/") and uri.rsplit("/", 1)[-1].startswith("packet-")]
            self.merge_batches.append(copy.deepcopy(packets))
            for packet in packets:
                before = int(packet["output_path"].split("-")[-1].split(".")[0])
                errors = self.break_windows.get(before, [])
                failure = errors.pop(0) if errors else ""
                uri = body["to"] + "/" + packet["output_path"]
                if failure == "missing":
                    self.contents.pop(uri, None)
                    continue
                if uri not in self.contents:
                    continue
                output = json.loads(self.contents[uri])
                if failure == "foreign":
                    output["methods"][0]["derived_from"] = ["sibling-unassigned-method"]
                elif failure == "duplicate":
                    output["methods"].append(copy.deepcopy(output["methods"][0]))
                elif failure == "incomplete":
                    output["methods"].pop()
                elif failure == "native_ids":
                    output["input_ids"] = ["0123456789abcdef01234567"]
                elif failure == "item_header":
                    output["input_ids"] = [item["item_id"] for item in packet["derived_controls"]["items"]]
                elif failure == "missing_resolved":
                    output.pop("resolved")
                self.contents[uri] = json.dumps(output, ensure_ascii=False)
        if lost:
            raise lost
        if self.malformed_merge_ack:
            self.malformed_merge_ack = False
            return {}
        return result

class MergeOwner(ExtractOwner):
    def __init__(self, root, *, full=False):
        super().__init__(root, full=full)
        self.ov = self.fixture.ov = MergeFake()
        self.capture_merge = not full

    async def _staged_next(self, job, root, state):
        if state["phase"] == "selection" and self.capture_merge:
            state["test_merge_complete"] = True
            return await self._staged_save(job, root, state, status="waiting_human", ov_task_id="")
        return await super()._staged_next(job, root, state)

async def seeded(owner, count=40, *, revision=""):
    job, entries = owner.new(1)
    root = owner._staged_root(job)
    chunk_count = _freeze_chunks(root, entries, job["source_snapshot"])
    (root / "compiler-base.md").write_text(SKILL_BODY, encoding="utf-8")
    (root / "revision.txt").write_text(revision, encoding="utf-8")
    chunk = _read(root / "chunks/00000000.json")
    ref = "scope-source"
    for index in range(count):
        item = {"item_id": f"item-method-{index:08d}", "kind": "method",
                "task": f"Perform the original supported operation {index:08d}",
                "boundary": "Preserve the explicitly supplied procedure and its conditions",
                "source_refs": [ref], "source_files": [chunk["source_uri"]]}
        _save(root / "catalog/method" / f"{index:08d}.json",
              {"item": item, "unit_id": _alias(f"original-unit-{index}"), "ordinals": [0],
               "bindings": {ref: [0]}, "leaf_items": [f"original-leaf-{index:08d}"]})
    _save(root / "catalog/manifest.json", {"method": count, "fact": 0, "gap": 0})
    prepare_workflow_order(root, count)
    state = {"protocol": "te-staged-compile.v1", "planning_version": 2, "round": 1, "phase": "merge_index",
             "merge_batch_protocol": MERGE_BATCH_PROTOCOL, "workflow_planning_protocol": WORKFLOW_PLANNING_PROTOCOL,
             "base_sha256": _sha(SKILL_BODY), "language_policy": {
                 "language": "en", "version": 1, "basis": "frozen_original_source"},
             "catalog_counts": {"method": count, "fact": 0, "gap": 0}, "chunk_count": chunk_count,
             "cursor": 0, "proposal_count": 0, "completed_tasks": 0, "attempted_tasks": 0,
             "estimated_tasks": count + 1, "current": None}
    job = await owner._staged_save(job, root, state, status="preparing")
    return await owner._staged_next(job, root, state)

async def finish(owner, job):
    for _ in range(200):
        if job["status"] in {"waiting_human", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Bounded method merge did not converge")
