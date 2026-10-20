# ruff: noqa: F401
"""Run the public fresh profile through extraction, planning, review, body, Cases and two rounds.

Only Native model responses are inert. No host phase, scope, routing or materialization is replaced.
"""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import deferred_evidence_review as review
from team_miner.skill_miner.evidence_quote_units import QUOTE_SCHEMA, QUOTE_STAGE
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT, SKILL_BODY, utc_now
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.routing_recovery import ERROR, prepare_recovery
from team_miner.skill_miner.source_scope import _prepare_local_source_routing
from team_miner.skill_miner.workflow_metadata import OUTPUT_SCHEMA as METADATA_SCHEMA
from team_miner.skill_miner.workflow_partition import OUTPUT_SCHEMA as PARTITION_SCHEMA
from tests.miner.skill_miner.support.artifact_batches import ArtifactOwner
from tests.miner.skill_miner.support.body_patch_pipeline import RAW, PatchFake
from tests.miner.skill_miner.support.extract_batches import ExtractFake
from tests.miner.skill_miner.support.ov_case_group_orchestration import GroupFake
from tests.miner.skill_miner.support.source_unit_extraction import POLICY, output_for
from tests.miner.skill_miner.support.workflow_metadata import _candidate


class FreshFake(ExtractFake, GroupFake):
    def __init__(self):
        super().__init__()
        self.patch_model = PatchFake()
        self.stages = []
        self.defer_metadata = False
        self.partition_groups = 2
        self.identity_pair_groups = False
        self.unmatched_second_pair = False
        self.metadata_count = 0

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return await super().request(account, method, path, **kwargs)
        prefix = request["from"][0] + "/"
        packets = [json.loads(text) for uri, text in sorted(self.contents.items()) if uri.startswith(prefix)]
        assert packets
        self.stages.extend(packet["stage"] for packet in packets)
        if packets[0]["stage"] in {"workflow_partition", "workflow_metadata", "identity_group", QUOTE_STAGE}:
            key = kwargs["extra_headers"]["Idempotency-Key"]
            self.calls.append((key, copy.deepcopy(request)))
            if key in self.accepted:
                task, old = self.accepted[key]
                assert old == request
                return {"task_id": task}
            task = "cmp_fresh_" + str(len(self.accepted))
            self.accepted[key] = (task, copy.deepcopy(request))
            self.tasks[task] = {"task_id": task, "status": "completed"}
            for packet in packets:
                controls, stage = packet["derived_controls"], packet["stage"]
                if stage == "workflow_partition":
                    ids = controls["assigned_ids"]
                    split = max(1, len(ids) // 2)
                    groups = [ids[:split], ids[split:]] if len(ids) > 1 else [ids]
                    if self.partition_groups != 2:
                        width = max(1, (len(ids) + self.partition_groups - 1) // self.partition_groups)
                        groups = [ids[start:start + width] for start in range(0, len(ids), width)]
                    value = {"schema_version": PARTITION_SCHEMA,
                             "assignment_sha256": controls["assignment_sha256"],
                             "rows": [[identity, group[-1]] for group in groups for identity in group],
                             "reasons": {group[-1]: "同一完整对练流程包含这些已分配步骤"
                                         for group in groups if len(group) > 1}}
                elif stage == "workflow_metadata":
                    value = _candidate({"assignment_sha256": controls["assignment_sha256"]})
                    assert value["schema_version"] == METADATA_SCHEMA
                    if controls.get("mining_intent", {}).get("active"):
                        matches = not self.unmatched_second_pair or self.metadata_count % 4 < 2
                        value["intent"] = {"matches_direction": matches, "complete_for_goal": True,
                                           "priority": 90, "reason": "来源支持完整对练流程"}
                    self.metadata_count += 1
                    if self.defer_metadata:
                        value = {"schema_version": METADATA_SCHEMA,
                                 "assignment_sha256": controls["assignment_sha256"],
                                 "deferred": {"reason": "需按原文复核完整执行条件"}}
                elif stage == QUOTE_STAGE:
                    operative = controls["method_input_ids"]
                    value = {"schema_version": QUOTE_SCHEMA, "input_ids": packet["input_ids"], "reviews": [{
                        "item_id": controls["items"][0]["item_id"],
                        "outcome": "candidate" if operative else "insufficient_this_fragment",
                        "reason": "当前原文支持完整方法" if operative else "当前只有结构上下文，继续复核原文",
                        "source_refs": controls["allowed_source_refs"], "supporting_quote_ids": operative[:1],
                        "partial_steps": [],
                        "candidate": _candidate({"assignment_sha256": ""})["candidate"] if operative else None,
                    }]}
                else:
                    ids = controls["required_candidate_ids"]
                    groups = ([ids[start:start + 2] for start in range(0, len(ids), 2)]
                              if self.identity_pair_groups and len(ids) > 2 else
                              [[identity] for identity in ids] if self.identity_pair_groups else [ids])
                    value = {"schema_version": "skill-miner.identity-groups.v2", "candidate_ids": ids,
                             "groups": [{"candidate_ids": group, "survivor_candidate_id": group[0],
                                         "reason": "同一完整方法，保留全部步骤与来源"} for group in groups]}
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
            return {"task_id": task}
        result = await super().request(account, method, path, **kwargs)
        for packet in packets:
            output = request["to"] + "/" + packet["output_path"]
            if packet["stage"] == "extract":
                value = output_for(packet)
                # Reproduce the earlier legitimate omission of worksheet labels/blank separators.
                value["facts"] = [{"text": "原始列定义", "source_refs": [ref]}
                                  for ref, unit in zip(packet["derived_controls"]["allowed_source_refs"],
                                                       packet["evidence"], strict=True)
                                  if unit["kind"] == "table_header"]
                self.contents[output] = json.dumps(value, ensure_ascii=False)
            elif packet["stage"] == "skill_section":
                self.contents[output] = json.dumps(self.patch_model.output(packet), ensure_ascii=False)
        return result

class FreshOwner(ArtifactOwner):
    def __init__(self, root, *, rounds=1, documents=5, mode="single", direction=None, target_count=None):
        super().__init__(root)
        self.ov = self.fixture.ov = FreshFake()
        self.rounds = rounds
        self.documents, self.mode = documents, mode
        self.direction = "完整客户沟通对练方法" if direction is None and mode == "single" else direction or ""
        self.target_count = target_count

    def new(self):
        job = self.repo.create_job(
            name="完整新任务配置", account="default", source_uri="viking://resources/source",
            source_snapshot_uri=RUNS_ROOT + "/pending/source", output_uri=RUNS_ROOT + "/pending/output",
            document_count=self.documents, max_rounds=self.rounds, human_checkpoints=True, skip_empty_checkpoints=True,
            consolidate_reviewed_candidates=True, mining_mode=self.mode,
            mining_direction=self.direction, target_skill_count=self.target_count,
        )
        self.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        folder = self.repo._job_dir(job["job_id"]) / "round_inputs/round-1"
        folder.mkdir(parents=True, exist_ok=True)
        entries = []
        for index in range(self.documents):
            # Many exact rows in one transport chunk, shared headers and a row crossing a chunk boundary.
            raw = "## Sheet1\n\n" + RAW + "| 交流 | " + "按当前客户提供的条件进行沟通；" * 100 + " |\n"
            path = folder / f"source-{index}.md"
            path.write_text(raw)
            entries.append({"kind": "source", "relative_path": f"original/doc-{index}.md", "local_path": str(path),
                            "sha256": hashlib.sha256(raw.encode()).hexdigest(), "content_bytes": len(raw.encode())})
        manifest = folder / "manifest.json"
        _save(manifest, entries)
        return self.repo.update_job(job["job_id"], input_manifest=str(manifest)), entries

async def run(owner, job, target_round):
    for _ in range(200):
        if len(job["rounds_history"]) == target_round or job["status"] in {"failed", "stopped", "waiting_human"}:
            return job
        assert job.get("ov_task_id"), job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Fresh full host profile did not terminate within bounded submissions")
