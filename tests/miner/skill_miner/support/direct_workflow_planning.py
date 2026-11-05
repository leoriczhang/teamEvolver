# ruff: noqa: F401
"""Direct planning keeps bounded controls, exact method dispositions and original lineage."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import mining_policy_block
from team_miner.skill_miner.direct_workflow_planning import (
    CAPACITY_SCHEMA,
    DIRECT_WORKFLOW_PROTOCOL,
    MAX_DIRECT_METHODS,
    MAX_RELATED_CONTEXT_CHARS,
    related_context,
    validate_capacity,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.bounded_compile_plan import candidate, planning_catalog
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner, begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs
from tests.miner.skill_miner.support.source_scope import sources


def selected(count=24, groups=8):
    units, items = planning_catalog(count)
    value = {
        "schema_version": planning.SELECTION_SCHEMA, "input_ids": [unit["id"] for unit in units],
        "skills": [{**{key: value for key, value in candidate([]).items() if key != "source_refs"},
                    "name": f"workflow-{index}",
                    "derived_from": [item["item_id"] for item in items[index::groups]]}
                   for index in range(groups)],
        "knowledge": [], "deferred": [], "resolved": [],
    }
    return items, value

def capacity():
    return {"schema_version": CAPACITY_SCHEMA, "input_ids": ["shared-container"],
            "source_refs": ["scope-1", "scope-2"], "reason": "These independent workflows require smaller windows."}

def context_fixture(tmp_path, *, notes=3):
    root, state = sources(tmp_path, ["Original method and separate unrelated appendix.\n" * 650,
                                    "Globally shared original constraint."])
    state.update(direct_workflow_protocol=DIRECT_WORKFLOW_PROTOCOL, efficiency_protocol="te-mining-efficient.v2",
                 catalog_counts={"method": 1, "fact": notes, "gap": 0})
    last = state["chunk_count"] - 1
    records = [{"item": {"kind": "method"}, "ordinals": [0]}]
    for index in range(notes):
        ordinal = (0, 1, last)[index % 3]
        _save(root / "catalog/fact" / f"{index:08d}.json",
              {"item": {"kind": "fact", "text": f"Readonly note {index}", "item_id": f"fact-{index}"},
               "ordinals": [ordinal]})
    return root, state, records

class DirectFake(ExtractFake):
    def __init__(self):
        super().__init__()
        self.capacity_above = 0
        self.capacity_ack_lost = False
        self.selection_packets = []
        self.force_capacity = False
        self.large_context = False

    async def request(self, account, method, path, **kwargs):
        accepted = await super().request(account, method, path, **kwargs)
        body = kwargs.get("json", {})
        if method == "POST" and path == "/api/v1/compile" and "/stages/extract_batch-" in body["from"][0]:
            prefix = body["from"][0] + "/"
            for uri, text in list(self.contents.items()):
                if not uri.startswith(prefix):
                    continue
                packet = json.loads(text)
                if self.large_context:
                    target = body["to"] + "/" + packet["output_path"]
                    output = json.loads(self.contents[target])
                    output["methods"][0].update(task="M" * 350, boundary="B" * 1000)
                    output["facts"] = [{"text": f"Context {index}: " + "C" * 970,
                                        "source_refs": packet["derived_controls"]["allowed_source_refs"]}
                                       for index in range(6)]
                    self.contents[target] = json.dumps(output, ensure_ascii=False)
                if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                    refs = packet["derived_controls"]["allowed_source_refs"]
                    self.contents[body["to"] + "/" + packet["output_path"]] = json.dumps({
                        "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": packet["input_ids"],
                        "methods": [], "facts": [{"text": "本轮复核没有新增领域规则。", "source_refs": refs}],
                        "deferred": [], "context_refs": [], "context_uncertain": False,
                    }, ensure_ascii=False)
        if method != "POST" or path != "/api/v1/compile" or "/stages/plan-" not in body["from"][0]:
            return accepted
        packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
        controls = packet["derived_controls"]
        self.selection_packets.append(copy.deepcopy(packet))
        uri = body["to"] + "/selection.json"
        output = json.loads(self.contents[uri])
        if self.force_capacity or self.capacity_above and len(controls["items"]) > self.capacity_above:
            output = {"schema_version": CAPACITY_SCHEMA, "input_ids": controls["input_ids"],
                      "source_refs": controls["allowed_source_refs"], "reason": "Independent workflow capacity."}
        else:
            row = output["skills"][0]
            row["derived_from"] = [item["item_id"] for item in controls["items"]]
            known = next((candidate for candidate in controls.get("known_candidates", [])
                          if candidate["name"] == row["name"]), None)
            if known is not None:
                row = {"reuse_candidate_id": known["candidate_id"], "derived_from": row["derived_from"],
                       "reason": "同一原文方法及其全部来源范围未改变。"}
            output["skills"] = [row]
        self.contents[uri] = json.dumps(output, ensure_ascii=False)
        if output["schema_version"] == CAPACITY_SCHEMA and self.capacity_ack_lost:
            self.capacity_ack_lost = False
            raise httpx.ReadTimeout("Capacity acceptance ACK was lost")
        return accepted

class DirectOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root, full=True)
        self.ov = self.fixture.ov = DirectFake()

    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"], staged_workflow_protocol=DIRECT_WORKFLOW_PROTOCOL), entries

async def at_plan(owner, count):
    job = await begin(owner, count)
    while True:
        root, state = await owner._staged_load(job)
        if state["current"]["stage"] == "plan":
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
