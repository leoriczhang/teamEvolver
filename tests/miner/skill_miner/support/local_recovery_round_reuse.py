# ruff: noqa: F401
"""Local selection recovery preserves frozen assignments and reviewed round dependencies."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from team_miner.skill_miner.compile_strategy import mining_policy_block
from team_miner.skill_miner.direct_workflow_planning import (
    LOCAL_SELECTION_RECOVERY_PROTOCOL,
    SELECTION_RECOVERY_PROTOCOL,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import SOURCE_INPUT_CLARITY_PROTOCOL, _read, _save, _sha
from team_miner.skill_miner.round_reuse import PROTOCOL_FIELDS, try_reuse_candidate
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs
from tests.miner.skill_miner.support.selection_window_recovery import RecoveryOwner


class LocalRecoveryFake(DirectFake):
    """Fail only the first original window; later independent windows are valid."""

    def __init__(self, *, threshold=6, refuse_all=False, source_failure=False):
        super().__init__()
        self.threshold, self.refuse_all = threshold, refuse_all
        self.failed_window_ids = None
        self.successful_assignments = []
        if source_failure:
            self.break_indexes[0] = ["deferred_text"]

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return result
        source = request["from"][0]
        if "/stages/extract_batch-" in source:
            packets = [json.loads(text) for uri, text in self.contents.items() if uri.startswith(source + "/")]
            for packet in packets:
                target = request["to"] + "/" + packet["output_path"]
                if target not in self.contents or not all("/original/" in row["source_uri"]
                                                        for row in packet["evidence"]):
                    continue
                value = json.loads(self.contents[target])
                # Preserve the intentional source schema defect until the real bounded repair.
                if any("reason" not in row for row in value.get("deferred", [])):
                    continue
                value["methods"] = [{"task": "退款复核", "boundary": "只按原始政策核实申请，缺值时询问用户。",
                                     "source_refs": [ref]}
                                    for ref in packet["derived_controls"]["allowed_source_refs"]]
                value["facts"], value["context_refs"] = [], []
                self.contents[target] = json.dumps(value, ensure_ascii=False)
            return result
        if "/stages/plan-" not in source:
            return result
        packet = json.loads(self.contents[source + "/packet.json"])
        assigned = [row["item_id"] for row in packet["derived_controls"]["items"]]
        if self.failed_window_ids is None:
            self.failed_window_ids = set(assigned)
        if self.refuse_all or len(assigned) > self.threshold and set(assigned) <= self.failed_window_ids:
            target = request["to"] + "/selection.json"
            value = json.loads(self.contents[target])
            value["skills"][0]["derived_from"].pop()
            self.contents[target] = json.dumps(value, ensure_ascii=False)
        else:
            self.successful_assignments.append(assigned)
        return result

class LocalRecoveryOwner(RecoveryOwner):
    def __init__(self, root, *, protocol=LOCAL_SELECTION_RECOVERY_PROTOCOL, clarity=True, **fake_options):
        super().__init__(root)
        self.recovery_protocol, self.clarity = protocol, clarity
        self.ov = self.fixture.ov = LocalRecoveryFake(**fake_options)

    def new(self, count=54):
        job, entries = super().new(count)
        for index, entry in enumerate(entries):
            text = f"原文文档{index}：依据客户退款申请复核既有政策；资料缺值时询问用户，不猜测规则。\n"
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=_sha(text), content_bytes=len(text.encode()))
        job = self.repo.update_job(
            job["job_id"], staged_selection_recovery_protocol=self.recovery_protocol,
            staged_source_input_clarity_protocol=SOURCE_INPUT_CLARITY_PROTOCOL if self.clarity else "",
        )
        return job, entries

async def reviewed(owner, count=54):
    job, entries = owner.new(count)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert final["status"] == "waiting_human", final.get("error")
    return final

def dataset(owner, job):
    return _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")

def frozen_files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for folder in ("chunks", "children", "compilers", "aux-compilers")
            for path in (root / folder).rglob("*") if path.is_file()}

async def historical_enabled_second_round(owner):
    """Build a real reviewed cache, then encode its pre-optional-field frozen header."""
    first = await reviewed(owner, 9)
    second, entries = await next_inputs(owner, first)
    second = await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, "")
    for _ in range(20):
        root, state = await owner._staged_load(second)
        if state["current"]["stage"] == "plan":
            break
        assert second["status"] not in {"failed", "stopped"}, second.get("error")
        second = await owner._advance_staged_round(second, owner.ov.tasks[second["ov_task_id"]])
    else:
        raise AssertionError("Historical reviewed source cache did not reach selection")
    path = root / "reuse/manifest.json"
    header = _read(path)
    assert header["enabled"] and header["extract_count"] > 0 and header["candidate_count"] == 1
    assert header["protocols"].pop("source_input_clarity_protocol") is None
    assert not state.get("source_input_clarity_protocol")
    state.pop("source_input_clarity_protocol", None)
    _save(path, header)
    # This is the fixture's historical commitment, not a bypass of the source,
    # checkpoint, compiler, Native request/result or feedback dependency checks.
    state["round_reuse_manifest_sha256"] = _sha(path.read_text(encoding="utf-8"))
    _save(root / "state.json", state)
    return first, second, root, state
