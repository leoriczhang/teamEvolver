# ruff: noqa: F401
"""Independent identity-resume regressions using only synthetic local Compile transport."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.ov_staged_orchestration import FakeCompile, Owner, drive


class SeparateSourceCompile(FakeCompile):
    """Two independent methods; their writer Evidence must never cross sources."""

    def __init__(self):
        super().__init__()
        self.candidate_count = 2
        self.packets = []
        self.reuse_prior = False
        self.reuse_responses = []

    async def request(self, account, method, path, **kwargs):
        fresh = method == "POST" and path == "/api/v1/compile" and (
            kwargs["extra_headers"]["Idempotency-Key"] not in self.accepted
        )
        accepted = await super().request(account, method, path, **kwargs)
        if not fresh:
            return accepted
        request = kwargs["json"]
        packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
        self.packets.append(copy.deepcopy(packet))
        controls = packet["derived_controls"]
        uri = request["to"] + "/" + packet["output_path"]
        if packet["stage"] == "extract":
            refs_by_id = {identity: alias for alias, identity in controls["reference_map"].items()}
            methods, facts = [], []
            for chunk in packet["evidence"]:
                if not chunk["source_uri"].rsplit("/", 1)[-1].startswith("doc-"):
                    facts.append({
                        "text": "补证回执记录，未提出新增可执行方法", "source_refs": [refs_by_id[chunk["id"]]],
                    })
                    continue
                index = int(chunk["source_uri"].rsplit("doc-", 1)[-1].split(".", 1)[0])
                methods.append({
                    "task": f"独立复核方法-{index}",
                    "boundary": f"仅适用文档 {index} 的操作条件",
                    "source_refs": [refs_by_id[chunk["id"]]],
                })
            self.contents[uri] = json.dumps({
                "schema_version": planning.INDEX_SCHEMA,
                "input_ids": packet["input_ids"],
                "methods": methods, "facts": facts, "deferred": [],
            }, ensure_ascii=False)
        elif packet["stage"] == "plan" and self.reuse_prior:
            output = json.loads(self.contents[uri])
            by_name = {row["name"]: row for row in controls["known_candidates"]}
            assert output["schema_version"] in {planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3}
            output["skills"] = [
                {"derived_from": row["derived_from"],
                 "reuse_candidate_id": by_name[row["name"]]["candidate_id"],
                 "reason": "同一独立工作流，沿用已复核身份"}
                for row in output["skills"]
            ]
            self.reuse_responses.append(copy.deepcopy(output))
            self.contents[uri] = json.dumps(output, ensure_ascii=False)
        return accepted

class PausingOwner(Owner):
    """Pause at the durable no-child boundary before one old writer submission."""

    def __init__(self, root, *, pause_stage=""):
        super().__init__(root)
        self.ov = SeparateSourceCompile()
        self.pause_stage = pause_stage
        self.paused = False

    async def _staged_submit(self, job, root, state, stage, output_path, chunks, controls, context):
        if self.pause_stage == stage and context.get("skill_name") == "refund-review-0":
            self.pause_stage = ""
            self.paused = True
            assert state["current"] is None
            return await self._staged_save(job, root, state, status="preparing", ov_task_id="")
        return await super()._staged_submit(job, root, state, stage, output_path, chunks, controls, context)

LANGUAGE_POLICY = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
