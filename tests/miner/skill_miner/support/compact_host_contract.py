# ruff: noqa: F401
"""Compact planning uses the real frozen host authorities and complete Case contract."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_identity import IDENTITY_PARTITION_PROTOCOL, candidate_id
from team_miner.skill_miner.candidate_refinement import BUSINESS_FIELDS, latest_refinement
from team_miner.skill_miner.compile_strategy import render_compact_aliased_workflow_compiler
from team_miner.skill_miner.direct_workflow_planning import CAPACITY_SCHEMA
from team_miner.skill_miner.evidence_quote_aliases import PROTOCOL as QUOTE_ALIAS_PROTOCOL
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _selection_packet, _sha
from team_miner.skill_miner.workflow_planning import COMPACT_PLANNING_PROTOCOL
from tests.miner.skill_miner.support.fresh_alias_and_case_group_contract import (
    AliasFake,
    AliasOwner,
    assert_frozen,
    dataset,
    first_selection,
    frozen_child,
)
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class CompactFake(AliasFake):
    """Synthetic model output, while every assignment/receipt/ledger stays production code."""

    def __init__(self):
        super().__init__()
        self.long_capacity = False
        self.refine_second_round = False
        self.refined_once = False
        self.duplicate_reuse_outputs = []
        self.reuse_one_row_per_method = False

    async def request(self, account, method, path, **kwargs):
        accepted = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return accepted
        if "/stages/extract_batch-" in request["from"][0]:
            prefix = request["from"][0] + "/"
            for uri, text in list(self.contents.items()):
                if not uri.startswith(prefix):
                    continue
                packet = json.loads(text)
                if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                    continue
                controls = packet["derived_controls"]
                aliases = {chunk_id: ref for ref, chunk_id in controls["reference_map"].items()}
                target = request["to"] + "/" + packet["output_path"]
                output = json.loads(self.contents[target])
                output["methods"] = [{"task": "退款复核", "boundary": "仅按原始政策复核",
                                      "source_refs": [aliases[chunk["id"]]]}
                                     for chunk in packet["evidence"]]
                self.contents[target] = _json(output)
        if "/stages/plan-" not in request["from"][0]:
            return accepted
        packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
        controls = packet["derived_controls"]
        target = request["to"] + "/selection.json"
        value = json.loads(self.contents[target])
        if value["schema_version"] == CAPACITY_SCHEMA:
            if self.long_capacity:
                value["reason"] = "Capacity diagnostic. " + "x" * (1151 - len("Capacity diagnostic. "))
            self.contents[target] = _json(value)
            return accepted
        row = value["skills"][0]
        if (self.refine_second_round and "/rounds/round-2/" in request["from"][0]
                and not self.refined_once):
            known = next(candidate for candidate in controls["known_candidates"]
                         if candidate["name"] == "refund-review")
            assert known["refinement_allowed"]
            row = {key: copy.deepcopy(known[key]) for key in BUSINESS_FIELDS}
            row.update(inputs=known["inputs"] + ["Request category"],
                       boundary="Classify the supplied request under its original policy before review.",
                       revise_candidate_id=known["candidate_id"],
                       base_metadata_sha256=known["base_metadata_sha256"],
                       reason="Clarify this same reviewed workflow using its assigned original methods.",
                       derived_from=value["skills"][0]["derived_from"])
            value["skills"] = [row]
            self.refined_once = True
        elif "reuse_candidate_id" in row and len(row["derived_from"]) > 1:
            midpoint = len(row["derived_from"]) // 2
            value["skills"] = [
                {**row, "reason": "First independent assigned subset supports this exact known identity.",
                 "derived_from": row["derived_from"][:midpoint]},
                {**row, "reason": "Second independent assigned subset supports this exact known identity.",
                 "derived_from": row["derived_from"][midpoint:]},
            ]
            if self.reuse_one_row_per_method:
                value["skills"] = [{**row, "reason": f"Exact assigned authority, independent subset {index}",
                                    "derived_from": [identity]}
                                   for index, identity in enumerate(row["derived_from"])]
            self.duplicate_reuse_outputs.append(copy.deepcopy(value))
        self.contents[target] = _json(value)
        return accepted

class CompactOwner(AliasOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = CompactFake()

    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"],
                                    staged_compact_planning_protocol=COMPACT_PLANNING_PROTOCOL,
                                    staged_quote_alias_protocol=QUOTE_ALIAS_PROTOCOL,
                                    staged_identity_partition_protocol=IDENTITY_PARTITION_PROTOCOL), entries
