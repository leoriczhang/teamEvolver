# ruff: noqa: F401
"""Independent bounded source indexes through the unchanged public directory Compile API."""

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
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_extract_batch_orchestration import EXTRACT_BATCH_PROTOCOL, _verified
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.artifact_batches import ArtifactFake, ArtifactOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive


class ExtractFake(ArtifactFake):
    """Only model results are synthetic; directory admission and per-file persistence stay real."""

    def __init__(self):
        super().__init__()
        self.candidate_count = 1
        self.extract_batches = []
        self.break_indexes = {}
        self.extract_ack = ""
        self.extract_post_status = 0

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/extract_batch-" not in request["from"][0]:
            return await super().request(account, method, path, **kwargs)
        key = kwargs["extra_headers"]["Idempotency-Key"]
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert len(request["from"]) == 1 and request["from"][0] in self.directories
        if self.extract_post_status:
            raise HTTPException(self.extract_post_status, "Compile request rejected")
        self.calls.append((key, copy.deepcopy(request)))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_extract_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents) if uri.startswith(prefix)]
            assert 1 <= len(packets) <= 8
            assert len({packet["output_path"] for packet in packets}) == len(packets)
            assert all(len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) <= 32000
                       for packet in packets)
            self.extract_batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed",
                                  "meta": {"pipeline": {"warnings": ["One independent output may be missing"]}}}
            for packet in packets:
                ordinal = int(packet["output_path"].split("-")[-1].split(".")[0])
                errors = self.break_indexes.get(ordinal, [])
                failure = errors.pop(0) if errors else ""
                if failure == "missing":
                    self.tasks[task_id]["meta"]["pipeline"]["errors"] = [
                        "File not found: viking://agent/skills/compiler/skill-miner.index.v2"
                    ]
                    continue
                refs = packet["derived_controls"]["allowed_source_refs"]
                output = {
                    "schema_version": planning.INDEX_SCHEMA_V2,
                    # Public native IDs are deliberately different from logical TE IDs.
                    "input_ids": ["0123456789abcdef01234567"],
                    "methods": [{"task": "退款复核", "boundary": "仅按原始政策复核", "source_refs": refs}],
                    "facts": [], "deferred": [], "context_refs": refs[:1], "context_uncertain": False,
                }
                if failure == "foreign":
                    output["methods"][0]["source_refs"] = ["viking://resources/unassigned.md"]
                if failure == "context_foreign":
                    output["context_refs"] = ["unassigned-scope"]
                if failure == "incomplete":
                    output["methods"][0]["source_refs"] = refs[:1]
                if failure == "deferred_text":
                    output["deferred"] = [{"text": "原文提及的附件未提供。", "source_refs": refs}]
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(
                    output, ensure_ascii=False
                )
        if self.extract_ack == "lost":
            self.extract_ack = ""
            raise httpx.ReadTimeout("Accepted extraction response was lost")
        if self.extract_ack == "malformed":
            self.extract_ack = ""
            return {}
        return {"task_id": task_id}

class ExtractOwner(ArtifactOwner):
    """Capture extraction completion by default; full=True runs actual remaining TE orchestration."""

    def __init__(self, root, *, full=False):
        super().__init__(root)
        self.fixture.ov = self.ov = ExtractFake()
        self.full = full

    def new(self, count=17):
        job, entries = self.fixture.new(count)
        job = self.repo.update_job(job["job_id"], staged_execution_protocol=admission.OPTIMIZED_PROTOCOL,
                                   staged_efficiency_protocol="te-mining-efficient.v2",
                                   staged_workflow_protocol="", staged_source_scope_protocol="",
                                   staged_planning_alias_protocol="", staged_case_group_protocol="",
                                   staged_compact_planning_protocol="", staged_quote_alias_protocol="",
                                   staged_identity_partition_protocol="", staged_uncertainty_scope_protocol="",
                                   staged_workflow_partition_protocol="")
        # This fixture covers the frozen efficient-v2 transport; fresh gates are tested independently.
        for entry in entries:
            # Eleven thousand Unicode characters fit one chunk, but only two fit one bounded packet.
            text = "按原文确认申请并复核退款政策；缺值时询问用户。" * 480
            path = Path(entry["local_path"])
            path.write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries

    async def _staged_catalog_ready(self, job, root, state):
        if self.full:
            return await super()._staged_catalog_ready(job, root, state)
        state["test_extraction_complete"] = True
        return await self._staged_save(job, root, state, status="waiting_human", ov_task_id="")

async def begin(owner, count=17):
    job, entries = owner.new(count)
    return await owner._prepare_staged_round(
        job, SKILL_BODY, entries, {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}, ""
    )

async def finish_extract(owner, job):
    for _ in range(30):
        if job["status"] in {"waiting_human", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Bounded extract repair did not terminate")
