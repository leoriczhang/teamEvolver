"""Fresh grouping and metadata traverse real TE orchestration with inert model results."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.workflow_metadata import OUTPUT_SCHEMA as METADATA_SCHEMA
from team_miner.skill_miner.workflow_partition import OUTPUT_SCHEMA as PARTITION_SCHEMA
from team_miner.skill_miner.workflow_partition import PROTOCOL
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.local_scope_pipeline import LocalScopeOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class WorkflowFake(DirectFake):
    """Only Native results are synthetic; frozen packets, scopes and acceptance are real."""

    def __init__(self):
        super().__init__()
        self.workflow_batches = []

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if (method != "POST" or path != "/api/v1/compile"
                or "/stages/workflow-batch-" not in request["from"][0]):
            return await super().request(account, method, path, **kwargs)
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert len(request["from"]) == 1 and request["from"][0] in self.directories
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
            return {"task_id": task_id}
        task_id = "cmp_workflow_" + str(len(self.accepted))
        self.accepted[key] = (task_id, copy.deepcopy(request))
        prefix = request["from"][0] + "/"
        packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents) if uri.startswith(prefix)]
        assert 1 <= len(packets) <= 8
        assert len({packet["output_path"] for packet in packets}) == len(packets)
        self.workflow_batches.append(copy.deepcopy(packets))
        for packet in packets:
            controls = packet["derived_controls"]
            if packet["stage"] == "workflow_partition":
                ids = controls["assigned_ids"]
                value = {
                    "schema_version": PARTITION_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                    "rows": [[identity, ids[-1]] for identity in ids],
                    "reasons": {ids[-1]: "同一完整复核流程包含所列条件与步骤"} if len(ids) > 1 else {},
                }
            else:
                assert packet["stage"] == "workflow_metadata"
                revision = json.loads(packet["revision_context"] or "{}")
                revised = bool(revision.get("feedback"))
                value = {
                    "schema_version": METADATA_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                    "candidate": {"name": "refund-review", "task": "退款复核",
                                  "trigger": "收到用户退款申请", "inputs": ["原始申请和政策条件"],
                                  "outputs": ["复核结论和未解决条件"],
                                  "boundary": "只按提供的原文；保留人工核实步骤" if revised else "只按提供的原文",
                                  "rationale": "原文支持完整独立复核方法"},
                }
            self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
        self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
        return {"task_id": task_id}


class WorkflowOwner(LocalScopeOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = WorkflowFake()

    def new(self, count=5):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"], staged_workflow_partition_protocol=PROTOCOL), entries


async def _first_round(owner):
    job, entries = owner.new(5)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    result = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert result["status"] == "waiting_human", result.get("error")
    return result


def _dataset(owner, job):
    return _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")


@pytest.mark.asyncio
async def test_fresh_partition_metadata_identity_body_and_twelve_cases_complete(tmp_path):
    owner = WorkflowOwner(tmp_path)
    first = await _first_round(owner)
    root, state = await owner._staged_load(first)
    assert state["workflow_partition_protocol"] == PROTOCOL
    assert state["identity_review_complete"] and state["complete"]
    assert first["candidate_count"] == 1
    assert [packet["stage"] for batch in owner.ov.workflow_batches for packet in batch] == [
        "workflow_partition", "workflow_metadata",
    ]
    assert not owner.ov.selection_packets
    catalog = [_read(path) for path in sorted((root / "catalog/method").glob("*.json"))]
    partition, metadata = [batch[0] for batch in owner.ov.workflow_batches]
    assert len(partition["derived_controls"]["method_rows"]) == len(catalog) > 1
    assert len(metadata["derived_controls"]["method_rows"]) == len(catalog)
    assert metadata["derived_controls"]["parent_id"] == partition["derived_controls"]["assigned_ids"][-1]
    assert [row[1:3] for row in metadata["derived_controls"]["method_rows"]] == [
        [row["item"]["task"], row["item"]["boundary"]] for row in catalog
    ]
    assert len(list((root / "method-decisions").glob("*.json"))) == len(catalog)
    assert len(_dataset(owner, first)["cases"]) == 12
    workspace = owner.repo.workspace(first) / "compiled_skill/refund-review"
    assert (workspace / "SKILL.md").is_file() and (workspace / "EVALUATION.md").is_file()
    assert "退款复核" in (workspace / "SKILL.md").read_text()
    assert owner.ov.extract_batches and owner.ov.artifacts and owner.ov.batches


@pytest.mark.asyncio
@pytest.mark.parametrize("feedback", ["", "请保留人工核实步骤；该意见作用范围未知，须重新检查所有候选。"])
async def test_second_round_regroups_and_only_reuses_unchanged_reviewed_results(tmp_path, feedback):
    owner = WorkflowOwner(tmp_path)
    first = await _first_round(owner)
    original_ids = [case["case_id"] for case in _dataset(owner, first)["cases"]]
    before = len(owner.ov.workflow_batches), len(owner.ov.artifacts), len(owner.ov.batches)
    second, entries = await next_inputs(owner, first, feedback=feedback)
    second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
    assert second["status"] == "waiting_human", second.get("error")
    root, state = await owner._staged_load(second)
    assert state["round"] == 2 and state["identity_review_complete"]
    assert state["reused_extract_count"] > 0
    latest = [packet for batch in owner.ov.workflow_batches[before[0]:] for packet in batch]
    assert [packet["stage"] for packet in latest] == ["workflow_partition", "workflow_metadata"]
    assert all(json.loads(packet["revision_context"])["feedback"] == feedback for packet in latest)
    assert all(packet["revision_context_is_evidence"] is False for packet in latest)
    assert len(_dataset(owner, second)["cases"]) == 12
    candidate = _read(root / "plan.json")["skills"][0]
    if feedback:
        assert not state.get("reused_candidate_count")
        assert len(owner.ov.artifacts) > before[1] and len(owner.ov.batches) > before[2]
        assert candidate["boundary"] == "只按提供的原文；保留人工核实步骤"
    else:
        assert state["reused_candidate_count"] == 1
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before[1:]
        assert [case["case_id"] for case in _dataset(owner, second)["cases"]] == original_ids
