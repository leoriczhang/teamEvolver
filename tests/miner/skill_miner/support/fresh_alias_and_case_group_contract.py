# ruff: noqa: F401
"""Fresh machine aliases and Case groups retain the host's exact frozen authorities."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_identity import candidate_id
from team_miner.skill_miner.candidate_refinement import BUSINESS_FIELDS, latest_refinement
from team_miner.skill_miner.ov_case_batch_orchestration import _batch_ledger
from team_miner.skill_miner.ov_case_group_orchestration import (
    CASE_GROUP_COMPILER_STAGE,
    CASE_GROUP_PROTOCOL,
    _group_packet,
    render_case_group_compiler,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import (
    _chunks,
    _json,
    _method_groups,
    _package,
    _read,
    _save,
    _selection_packet,
)
from team_miner.skill_miner.planning_aliases import PROTOCOL, PlanningAliasManifestError
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.local_scope_pipeline import LocalScopeOwner
from tests.miner.skill_miner.support.ov_case_group_orchestration import GroupFake, assigned_indices
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class AliasFake(DirectFake):
    def __init__(self):
        super().__init__()
        self.bad_aliases = []
        self.lose_plan_ack = False

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/plan-" not in request["from"][0]:
            return result
        target = request["to"] + "/selection.json"
        value = json.loads(self.contents[target])
        if self.bad_aliases:
            mutation = self.bad_aliases.pop(0)
            origins = value["skills"][0]["derived_from"]
            if mutation == "unknown":
                origins[0] = "m999"
            elif mutation == "duplicate":
                origins.append(origins[0])
            elif mutation == "missing":
                origins.pop()
            elif mutation == "role":
                origins[0] = value["input_ids"][0]
            self.contents[target] = json.dumps(value, ensure_ascii=False)
        if self.lose_plan_ack:
            self.lose_plan_ack = False
            raise httpx.ReadTimeout("Plan accepted but its acknowledgement was lost")
        return result

class AliasOwner(LocalScopeOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = AliasFake()

    def new(self, count=8):
        job, entries = super().new(count)
        # The original fixture's long documents are unnecessary for metadata recovery.
        # Distinct complete local originals still travel through real extraction/scoping.
        for index, entry in enumerate(entries):
            text = (f"Refund procedure {index}: collect the request, verify the supplied policy "
                    "and explain the result.\n")
            path = Path(entry["local_path"])
            path.write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()),
                         relative_path=f"original/原始政策-{index}-" + "源" * 80 + ".md")
        return self.repo.update_job(job["job_id"], staged_planning_alias_protocol=PROTOCOL,
                                    staged_case_group_protocol=""), entries

async def first_selection(owner, count=8):
    job = await begin(owner, count)
    for _ in range(100):
        root, state = await owner._staged_load(job)
        if (state.get("current") or {}).get("stage") == "plan":
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("No bounded selection assignment was submitted")

def frozen_child(root, current):
    folder = root / "children" / current["child"]
    return {str(path.relative_to(root)): path.read_bytes() for path in folder.iterdir()
            if path.is_file() and path.name != "metadata.json"}

def assert_frozen(root, originals):
    assert all((root / path).read_bytes() == content for path, content in originals.items())

def dataset(owner, job):
    return _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")

class GroupContractFake(AliasFake, GroupFake):
    def __init__(self):
        super().__init__()
        self.scoped_sources = False
        self.refine_second_round = False

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return result
        if self.scoped_sources and "/stages/extract_batch-" in request["from"][0]:
            prefix = request["from"][0] + "/"
            for uri, text in list(self.contents.items()):
                if not uri.startswith(prefix):
                    continue
                packet = json.loads(text)
                controls = packet["derived_controls"]
                reverse = {chunk_id: ref for ref, chunk_id in controls["reference_map"].items()}
                value = json.loads(self.contents[request["to"] + "/" + packet["output_path"]])
                value.update(methods=[], facts=[], deferred=[], context_refs=[], context_uncertain=False)
                for chunk in packet["evidence"]:
                    ref = reverse[chunk["id"]]
                    if "原始政策-0-" in chunk["source_uri"] or "原始政策-1-" in chunk["source_uri"]:
                        value["methods"].append({"task": "退款复核", "boundary": "仅按原始政策复核",
                                                 "source_refs": [ref]})
                    else:
                        value["facts"].append({"text": "Mandatory original policy constraint.", "source_refs": [ref]})
                        value["context_refs"].append(ref)
                self.contents[request["to"] + "/" + packet["output_path"]] = _json(value)
        if (self.refine_second_round and "/rounds/round-2/" in request["from"][0]
                and "/stages/plan-" in request["from"][0]):
            packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
            known = next(row for row in packet["derived_controls"]["known_candidates"]
                         if row["name"] == "refund-review")
            assert known["candidate_id"].startswith("c") and known["refinement_allowed"]
            uri = request["to"] + "/selection.json"
            value = json.loads(self.contents[uri])
            row = {key: copy.deepcopy(known[key]) for key in BUSINESS_FIELDS}
            row.update(inputs=known["inputs"] + ["Request category"],
                       boundary="Classify the supplied request under its original policy before review.",
                       revise_candidate_id=known["candidate_id"],
                       base_metadata_sha256=known["base_metadata_sha256"],
                       reason="Retain the reviewed identity while clarifying its supported execution boundary.",
                       derived_from=value["skills"][0]["derived_from"])
            value["skills"] = [row]
            self.contents[uri] = _json(value)
        return result

class GroupContractOwner(AliasOwner):
    def __init__(self, root, *, scoped_sources=False):
        super().__init__(root)
        self.ov = self.fixture.ov = GroupContractFake()
        self.ov.scoped_sources = scoped_sources

    def new(self, count=8):
        job, entries = super().new(count)
        if self.ov.scoped_sources:
            assert count == 5
            for index, (entry, size) in enumerate(zip(entries, [11000, 11000, 4800, 4800, 4800], strict=True)):
                lead = "Original procedure.\n" if index < 2 else "Mandatory original policy constraint.\n"
                text = lead + "x" * (size - len(lead))
                Path(entry["local_path"]).write_text(text, encoding="utf-8")
                entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        job = self.repo.update_job(job["job_id"], staged_case_group_protocol=CASE_GROUP_PROTOCOL,
                                   staged_source_fragment_protocol="", staged_source_input_clarity_protocol="")
        return job, entries

async def first_group(owner, count=8):
    job = await begin(owner, count)
    for _ in range(200):
        root, state = await owner._staged_load(job)
        if (state.get("current") or {}).get("input_protocol") == CASE_GROUP_PROTOCOL:
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("No grouped Case assignment was submitted")

def assert_group_bounds(root):
    for metadata in (root / "children").glob("case_batch-*/metadata.json"):
        current = _read(metadata)
        if current.get("input_protocol") != CASE_GROUP_PROTOCOL:
            continue
        locators = _read(root / current["packet_file"])
        assert 1 <= len(locators) <= 4
        body = (root / current["compiler_file"]).read_text()
        request = _read(root / current["request_file"])
        assert set(request) == {"from", "to", "skill", "instruction"} and request["from"] == [current["input_uri"]]
        for locator in locators:
            packet = _read(root / locator["local_path"])
            assert 1 <= len(packet["derived_controls"]["cases"]) <= 4
            assert len(packet["evidence"]) <= 8 and len(_json(packet["evidence"])) <= 26000
            assert len(_json(packet)) <= 32000
            assert len(body) + len(_json(packet)) + len(request["instruction"]) <= 48000
