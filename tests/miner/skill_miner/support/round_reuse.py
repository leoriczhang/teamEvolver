# ruff: noqa: F401
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import _alias, _freeze_chunks, _raw_group, _read, _save, _sha
from team_miner.skill_miner.round_reuse import (
    OPTIMIZED_EXECUTION_PROTOCOL,
    _file_sha,
    _profile,
    _sources,
    prepare_round_reuse,
    try_reuse_candidate,
    try_reuse_extract,
)
from tests.miner.skill_miner.support.artifact_batches import ArtifactFake, ArtifactOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive

POLICY = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}

class ScopedFake(ArtifactFake):
    """Two unrelated methods, each explicitly supported by one assigned source file."""

    def __init__(self):
        super().__init__()
        self.candidate_count = 2

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        body = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/extract-" not in body["from"][0]:
            return result
        packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
        aliases = packet["derived_controls"]["reference_map"]
        eligible = [alias for alias, chunk_id in aliases.items()
                    if any(chunk["id"] == chunk_id and "/original/" in chunk["source_uri"]
                           for chunk in packet["evidence"])]
        output = json.loads(self.contents[body["to"] + "/index.json"])
        output["methods"] = [
            {**row, "source_refs": [eligible[index % len(eligible)]]}
            for index, row in enumerate(output["methods"])
        ] if eligible else []
        if not eligible:
            output["facts"] = [{
                "text": "该文档仅记录本轮复核，没有额外领域规则。", "source_refs": list(aliases),
            }]
        self.contents[body["to"] + "/index.json"] = json.dumps(output, ensure_ascii=False)
        return result

class ReuseOwner(ArtifactOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = ScopedFake()

async def reviewed_first(owner, *, question=False):
    job, entries = owner.new(2)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=3, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    if question:
        pending = copy.deepcopy(first["pending_checkpoint"])
        pending["questions"] = [{
            "qid": "source-question", "question": "请确认该方法的边界", "reason": "已知来源的复核问题",
            "required": False, "source_refs": [first["source_snapshot"] + "/original/doc-0.md"],
        }]
        first = owner.repo.update_job(first["job_id"], pending_checkpoint=pending)
    return first

async def next_inputs(owner, first, *, feedback="", answer=False):
    answers = {"source-question": "请核实明确边界，不推测额外事实。"} if answer else {}
    job = owner.repo.prepare_next_round(first["job_id"], first["pending_checkpoint"]["id"], answers, [], {},
                                       feedback=feedback)
    entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
    manifest = owner.repo._job_dir(job["job_id"]) / f"round_inputs/round-{job['current_round']}/manifest.json"
    job = owner.repo.update_job(job["job_id"], input_manifest=str(manifest))
    owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
    return job, entries

def prepared_only(owner, job, entries, *, body=SKILL_BODY, policy=POLICY):
    from team_miner.skill_miner.case_context import CASE_NARRATIVE_PROTOCOL

    root = owner._staged_root(job)
    count = _freeze_chunks(root, entries, job["source_snapshot"])
    (root / "compiler-base.md").write_text(body, encoding="utf-8")
    (root / "revision.txt").write_text("Frozen review context", encoding="utf-8")
    compilers = {}
    efficient = job.get("staged_efficiency_protocol") == "te-mining-efficient.v2"
    for stage in ("extract", "merge_index", "plan", "skill_section", "evaluation", "case"):
        value = planning.stage_compiler_body(body, stage)
        if efficient:
            from team_miner.skill_miner.compile_strategy import render_efficient_stage_compiler

            value = render_efficient_stage_compiler(body, stage)
        (root / "compilers").mkdir(exist_ok=True)
        (root / "compilers" / f"{stage}.md").write_text(value, encoding="utf-8")
        compilers[stage] = _sha(value)
    state = {
        "execution_protocol": OPTIMIZED_EXECUTION_PROTOCOL, "round": job["current_round"],
        "compilers": compilers, "base_sha256": _sha(body), "language_policy": copy.deepcopy(policy),
        "chunk_count": count, "cursor": 0, "unit_count": 0,
        "case_narrative_protocol": CASE_NARRATIVE_PROTOCOL,
    }
    if job.get("staged_skill_body_protocol"):
        state["skill_body_protocol"] = job["staged_skill_body_protocol"]
    if efficient:
        from team_miner.skill_miner.ov_extract_batch_orchestration import EXTRACT_BATCH_PROTOCOL
        from team_miner.skill_miner.workflow_planning import WORKFLOW_PLANNING_PROTOCOL

        state.update(efficiency_protocol="te-mining-efficient.v2", extract_batch_protocol=EXTRACT_BATCH_PROTOCOL,
                     workflow_planning_protocol=WORKFLOW_PLANNING_PROTOCOL)
    prepare_round_reuse(job, root, state, entries)
    return root, state

def candidate_and_scope(root, name="refund-review-0"):
    previous = root.parent.parent.parent / "rounds/round-1"
    plan = _read(previous / "reviewed_output/semantic_reports/capability-plan.json")
    candidate = copy.deepcopy(next(row for row in plan["skills"] if row["name"] == name))
    old_prefix = root.parent.parent.parent.name + "/rounds/round-1/"
    new_prefix = root.parent.parent.parent.name + "/rounds/round-2/"
    candidate["source_refs"] = [ref.replace(old_prefix, new_prefix) for ref in candidate["source_refs"]]
    scope = _read(previous / "input/staged/scopes" / f"{name}.json")
    return candidate, scope

async def reviewed_batch_inputs(tmp_path):
    from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner

    class ReuseExtractFake(ExtractFake):
        async def request(self, account, method, path, **kwargs):
            result = await super().request(account, method, path, **kwargs)
            body = kwargs.get("json", {})
            if (method == "POST" and path == "/api/v1/compile"
                    and "/stages/extract_batch-" in body["from"][0]):
                prefix = body["from"][0] + "/"
                for uri in sorted(self.contents):
                    if not uri.startswith(prefix):
                        continue
                    packet = json.loads(self.contents[uri])
                    if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                        refs = packet["derived_controls"]["allowed_source_refs"]
                        self.contents[body["to"] + "/" + packet["output_path"]] = json.dumps({
                            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": packet["input_ids"],
                            "methods": [], "facts": [{"text": "本轮复核记录未增加领域规则。", "source_refs": refs}],
                            "deferred": [], "context_refs": [], "context_uncertain": False,
                        }, ensure_ascii=False)
            return result

    owner = ExtractOwner(tmp_path, full=True)
    owner.fixture.ov = owner.ov = ReuseExtractFake()
    job, entries = owner.new(4)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    return owner, first, await next_inputs(owner, first)
