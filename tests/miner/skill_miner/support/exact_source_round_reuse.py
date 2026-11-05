# ruff: noqa: F401
"""Reviewed adjacent rounds reuse exact source associations and complete twelve-Case bundles."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import render_stage_batch_compiler
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.skill_miner.round_reuse import (
    PROTOCOL_FIELDS,
    _profile,
    _sources,
    _writer_compilers,
    try_reuse_candidate,
    try_reuse_extract,
    try_reuse_source_unit_extract,
)
from team_miner.skill_miner.skill_body_patch import OUTPUT_SCHEMA
from team_miner.skill_miner.source_unit_extraction import next_source_unit_assignment
from team_miner.skill_miner.source_unit_scope import _digest
from team_miner.skill_miner.source_units import PROTOCOL, freeze_source_units, load_source_units
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import ExtractOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, candidate_and_scope, next_inputs, prepared_only


class PatchFake(DirectFake):
    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/artifact_batch-" not in request["from"][0]:
            return result
        for uri in sorted(self.contents):
            if not uri.startswith(request["from"][0] + "/"):
                continue
            packet = json.loads(self.contents[uri])
            controls = packet["derived_controls"].get("body_patch")
            if controls is None:
                continue
            atom = controls["next_atom_id"]
            value = {
                "schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                "base_state_sha256": controls["base_state_sha256"], "input_ids": controls["input_ids"],
                "title": "完整退款复核方法", "add": [], "attach": [],
                "step_order": list(controls["step_order"]), "no_change_reason": "本批只有来源上下文。",
            }
            if controls["operative_ids"]:
                value.update(no_change_reason="", add=[{
                    "id": atom, "kind": "step", "text": "确认申请并按原始政策复核。",
                    "support_ids": [controls["operative_ids"][0]],
                }], step_order=[*controls["step_order"], atom])
            self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
        return result

class ExactReuseOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root, full=True)
        self.fixture.ov = self.ov = PatchFake()
        self.pause_next = False

    async def _staged_next(self, job, root, state):
        if self.pause_next:
            return await self._staged_save(job, root, state)
        return await super()._staged_next(job, root, state)

    def new(self, count=1):
        job, entries = super().new(count)
        job = self.repo.update_job(job["job_id"], max_rounds=3,
                                   staged_source_scope_protocol="te-source-scope.v3",
                                   staged_workflow_protocol="te-direct-workflow-planning.v1",
                                   staged_source_unit_protocol=PROTOCOL,
                                   staged_skill_body_protocol="te-skill-body-patch.v1")
        for entry in entries:
            text = "# 退款复核\n\n| 适用条件 | 操作 |\n| --- | --- |\n| 已收到申请 | 先确认再核实政策。 |\n"
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries

async def first_round(owner):
    job, entries = owner.new()
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], input_manifest=str(manifest))
    result = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert result["status"] == "waiting_human", result.get("error")
    return result

def rebound_candidate(prior_root, root, name, manifest):
    candidate, scope = candidate_and_scope(root, name)
    previous = load_source_units(prior_root, scope["source_units"]["unit_ids"])
    current = load_source_units(root, list(range(manifest["unit_count"])))
    def key(row):
        return row["source_uri"].split("/source/", 1)[1], row["start_char"], row["end_char"], row["sha256"]
    by_range = {key(row): row["id"] for row in current}
    mapping = {row["id"]: by_range[key(row)] for row in previous}
    precise = scope["source_units"]
    for field in ("unit_ids", "operative_ids", "context_ids"):
        precise[field] = [mapping[identity] for identity in precise[field]]
    for row in precise["method_groups"]:
        row["unit_ids"] = [mapping[identity] for identity in row["unit_ids"]]
    rows = load_source_units(root, precise["unit_ids"])
    precise["source_sha256"] = _digest([{field: row[field] for field in
                                       ("id", "source_uri", "start_char", "end_char", "sha256", "source_sha256")}
                                      for row in rows])
    precise["sha256"] = _digest({field: value for field, value in precise.items() if field != "sha256"})
    return candidate, scope

def make_scope(root, manifest, *, method_index=3, context_index=0):
    rows = load_source_units(root, [method_index, context_index])
    method, context = next(row for row in rows if row["unit_index"] == method_index), next(
        row for row in rows if row["unit_index"] == context_index
    )
    precise = {"protocol": PROTOCOL, "unit_ids": [row["id"] for row in rows],
               "operative_ids": [method["id"]], "context_ids": [context["id"]],
               "method_groups": [{"item_id": "method-1", "task": "完成核实", "boundary": "只按原始政策",
                                  "unit_ids": [method["id"]]}],
               "source_sha256": _digest([{key: row[key] for key in
                                          ("id", "source_uri", "start_char", "end_char", "sha256", "source_sha256")}
                                         for row in rows])}
    precise["sha256"] = _digest(precise)
    return {"source_units": precise, "ordinals": [0], "method_ordinals": [0]}

def profile_fixture(tmp_path):
    root = tmp_path / "round_inputs/round-1/staged"
    path = root.parent / "original.txt"
    path.parent.mkdir(parents=True)
    text = "# 政策\n\n| 条件 | 行动 |\n| --- | --- |\n| A | 先核实 |\n| B | 停止 |\n"
    path.write_text(text, encoding="utf-8")
    prefix = "viking://resources/runs/same-job/rounds/round-1/source"
    chunks = planning.source_chunks(text, prefix + "/original/doc.md", max_chars=12000)
    for index, chunk in enumerate(chunks):
        _save(root / "chunks" / f"{index:08d}.json", {**chunk, "ordinal": index})
    manifest = freeze_source_units(root, {"source_unit_protocol": PROTOCOL, "chunk_count": len(chunks)})
    sources = _sources([{"kind": "source", "relative_path": "original/doc.md", "local_path": str(path),
                         "sha256": _sha(text)}], root.parent)
    return root, prefix, sources, manifest
