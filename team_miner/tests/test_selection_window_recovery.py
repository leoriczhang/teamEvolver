"""Invalid model bookkeeping retries smaller frozen assignments without accepting partial results."""

import asyncio
import copy
import json

import pytest

from team_miner.direct_workflow_planning import SELECTION_RECOVERY_PROTOCOL
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.tests.test_direct_workflow_planning import DirectFake, DirectOwner
from team_miner.tests.test_extract_batches import begin
from team_miner.tests.test_local_scope_pipeline import LocalScopeOwner
from team_miner.tests.test_ov_staged_orchestration import drive
from team_miner.tests.test_round_reuse import POLICY, next_inputs


class InvalidSelectionFake(DirectFake):
    def __init__(self, *, failure="missing", threshold=4):
        super().__init__()
        self.failure, self.threshold = failure, threshold
        self.valid_assignments = []

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/plan-" not in request["from"][0]:
            return result
        packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
        assigned = [row["item_id"] for row in packet["derived_controls"]["items"]]
        if len(assigned) <= self.threshold:
            self.valid_assignments.append(assigned)
            return result
        uri = request["to"] + "/selection.json"
        value = json.loads(self.contents[uri])
        origins = value["skills"][0]["derived_from"]
        if self.failure == "missing":
            origins.pop()
        elif self.failure == "duplicate":
            origins.append(origins[0])
        elif self.failure == "oversized":
            value["padding"] = "Z" * 13000
        else:
            self.contents[uri] = "{incomplete JSON"
            return result
        self.contents[uri] = json.dumps(value)
        return result


class RecoveryOwner(LocalScopeOwner):
    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"],
                                   staged_selection_recovery_protocol=SELECTION_RECOVERY_PROTOCOL), entries


def test_missing_dispositions_shrink_once_and_keep_every_method_and_twelve_cases(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake()
        job = await drive(owner, await begin(owner, 48))
        assert job["status"] == "waiting_human", job.get("error")
        root, state = await owner._staged_load(job)
        assert state["validation_splits"] == 3 and state["direct_recovery_limit"] == 3
        accepted = [identity for batch in owner.ov.valid_assignments for identity in batch]
        assert len(accepted) == len(set(accepted)) == state["proposal_count"]
        assert len(list((root / "method-decisions").glob("*.json"))) == state["proposal_count"]
        audits = [_read(path) for path in (root / "direct-planning/validation-splits").glob("*.json")]
        assert all(row["accepted_dispositions"] == 0 and row["attempt"] == 1 for row in audits)
        assert all(row["planning_coverage"]["missing_item_ids"] for row in audits)
        assert all(len(packet["derived_controls"]["items"]) <= 3
                   for packet in owner.ov.selection_packets[6:])
        dataset = _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")
        assert len(dataset["cases"]) == 12

    asyncio.run(check())


def test_irreducible_invalid_single_method_is_a_finite_failure_without_fake_artifacts(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake(threshold=0)
        job = await drive(owner, await begin(owner, 8))
        root, state = await owner._staged_load(job)
        assert job["status"] == "failed" and "disposition" in job["error"].lower()
        assert state["direct_recovery_limit"] == 1 and state["selection_cursor"] == 0
        assert [len(packet["derived_controls"]["items"]) for packet in owner.ov.selection_packets] == [
            8, 8, 4, 4, 2, 2, 1, 1, 1,
        ]
        assert not list((root / "method-decisions").glob("*.json"))
        assert not job["materialized"]

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["duplicate", "json", "oversized"])
def test_other_invalid_model_outputs_recover_without_accepting_or_truncating_them(tmp_path, failure):
    async def check():
        owner = RecoveryOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake(failure=failure)
        job = await drive(owner, await begin(owner, 8))
        assert job["status"] == "waiting_human", job.get("error")
        root, state = await owner._staged_load(job)
        assert state["validation_splits"] == 1 and state["direct_recovery_limit"] == 4
        accepted = [identity for batch in owner.ov.valid_assignments for identity in batch]
        assert len(accepted) == len(set(accepted)) == state["proposal_count"]
        assert len(_read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12
        audit = _read(next((root / "direct-planning/validation-splits").glob("*.json")))
        assert audit["accepted_dispositions"] == 0

    asyncio.run(check())


def test_restart_after_split_does_not_repeat_source_or_modify_rejected_request(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake()
        job = await begin(owner, 8)
        next_stage = owner._staged_next
        crashed = False

        async def crash_after_journal(job, root, state):
            nonlocal crashed
            if state.get("validation_splits") and state.get("current") is None and not crashed:
                crashed = True
                raise RuntimeError("process exited after durable split")
            return await next_stage(job, root, state)

        owner._staged_next = crash_after_journal
        with pytest.raises(RuntimeError, match="durable split"):
            await drive(owner, job)
        job = owner.repo.get_job(job["job_id"])
        root, state = await owner._staged_load(job)
        assert state["current"] is None and state["selection_cursor"] == 0
        extract_count = len(owner.ov.extract_batches)
        originals = {str(path.relative_to(root)): path.read_bytes()
                     for folder in (root / "chunks", root / "children", root / "compilers")
                     for path in folder.rglob("*") if path.is_file()}
        owner._staged_next = next_stage
        job = await drive(owner, await owner._resume_staged_round(job))
        assert job["status"] == "waiting_human", job.get("error")
        assert len(owner.ov.extract_batches) == extract_count
        assert all((root / relative).read_bytes() == content for relative, content in originals.items())
        _, state = await owner._staged_load(job)
        assert state["validation_splits"] == 1

    asyncio.run(check())


def test_damaged_frozen_assignment_fails_without_a_new_model_repair(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        job = await begin(owner, 8)
        while True:
            root, state = await owner._staged_load(job)
            if state["current"]["stage"] == "plan":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        path = root / "proposals/00000000.json"
        record = _read(path)
        record["item"]["task"] += "changed frozen data"
        _save(path, record)
        before = len(owner.ov.calls)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "failed" and "Frozen direct planning" in job["error"]
        assert len(owner.ov.calls) == before
        _, state = await owner._staged_load(job)
        assert not state.get("validation_splits") and state["selection_cursor"] == 0

    asyncio.run(check())


def test_old_frozen_round_cannot_acquire_recovery_from_a_late_job_setting(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake(threshold=0)
        job = await begin(owner, 8)
        root, state = await owner._staged_load(job)
        frozen = {path.name: path.read_bytes() for path in (root / "compilers").glob("*.md")}
        job = owner.repo.update_job(job["job_id"], staged_selection_recovery_protocol=SELECTION_RECOVERY_PROTOCOL)
        job = await drive(owner, job)
        _, state = await owner._staged_load(job)
        assert job["status"] == "failed" and len(owner.ov.selection_packets) == 3
        assert not state.get("validation_splits") and not state.get("selection_recovery_protocol")
        assert frozen == {path.name: path.read_bytes() for path in (root / "compilers").glob("*.md")}

    asyncio.run(check())


def test_recovery_protocol_reuses_reviewed_round_without_reprocessing_outputs(tmp_path):
    async def check():
        owner = RecoveryOwner(tmp_path)
        owner.ov = owner.fixture.ov = InvalidSelectionFake()
        job, entries = owner.new(12)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        original = copy.deepcopy(first)
        before = len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, original)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        _, state = await owner._staged_load(second)
        assert state["selection_recovery_protocol"] == SELECTION_RECOVERY_PROTOCOL
        assert state["reused_candidate_count"] == 1 and state["reused_extract_count"] > 0
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before

    asyncio.run(check())
