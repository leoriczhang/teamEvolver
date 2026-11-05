"""Public directory/file Map merge batches preserve exact method dispositions and recovery."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import (
    mining_policy_block,
    render_merge_batch_compiler,
    stage_compiler_body,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_merge_batch_orchestration import MERGE_BATCH_PROTOCOL, _verified
from team_miner.skill_miner.ov_staged_orchestration import _alias, _freeze_chunks, _read, _save, _sha
from team_miner.skill_miner.workflow_planning import WORKFLOW_PLANNING_PROTOCOL, prepare_workflow_order
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner, begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive


class MergeFake(ExtractFake):
    def __init__(self):
        super().__init__()
        self.merge_batches, self.break_windows = [], {}
        self.merge_post_status = 0
        self.malformed_merge_ack = False

    async def request(self, account, method, path, **kwargs):
        body = kwargs.get("json", {})
        merging = method == "POST" and path == "/api/v1/compile" and "/stages/merge_batch-" in body["from"][0]
        if not merging:
            return await super().request(account, method, path, **kwargs)
        if self.merge_post_status:
            raise HTTPException(self.merge_post_status, "Rejected inert merge submission")
        key = kwargs["extra_headers"]["Idempotency-Key"]
        is_new, lost = key not in self.accepted, None
        try:
            result = await super().request(account, method, path, **kwargs)
        except httpx.ReadTimeout as error:
            result, lost = {"task_id": self.accepted[key][0]}, error
        if is_new:
            packets = [json.loads(text) for uri, text in sorted(self.contents.items())
                       if uri.startswith(body["from"][0] + "/") and uri.rsplit("/", 1)[-1].startswith("packet-")]
            self.merge_batches.append(copy.deepcopy(packets))
            for packet in packets:
                before = int(packet["output_path"].split("-")[-1].split(".")[0])
                errors = self.break_windows.get(before, [])
                failure = errors.pop(0) if errors else ""
                uri = body["to"] + "/" + packet["output_path"]
                if failure == "missing":
                    self.contents.pop(uri, None)
                    continue
                if uri not in self.contents:
                    continue
                output = json.loads(self.contents[uri])
                if failure == "foreign":
                    output["methods"][0]["derived_from"] = ["sibling-unassigned-method"]
                elif failure == "duplicate":
                    output["methods"].append(copy.deepcopy(output["methods"][0]))
                elif failure == "incomplete":
                    output["methods"].pop()
                elif failure == "native_ids":
                    output["input_ids"] = ["0123456789abcdef01234567"]
                elif failure == "item_header":
                    output["input_ids"] = [item["item_id"] for item in packet["derived_controls"]["items"]]
                elif failure == "missing_resolved":
                    output.pop("resolved")
                self.contents[uri] = json.dumps(output, ensure_ascii=False)
        if lost:
            raise lost
        if self.malformed_merge_ack:
            self.malformed_merge_ack = False
            return {}
        return result


class MergeOwner(ExtractOwner):
    def __init__(self, root, *, full=False):
        super().__init__(root, full=full)
        self.ov = self.fixture.ov = MergeFake()
        self.capture_merge = not full

    async def _staged_next(self, job, root, state):
        if state["phase"] == "selection" and self.capture_merge:
            state["test_merge_complete"] = True
            return await self._staged_save(job, root, state, status="waiting_human", ov_task_id="")
        return await super()._staged_next(job, root, state)


async def seeded(owner, count=40, *, revision=""):
    job, entries = owner.new(1)
    root = owner._staged_root(job)
    chunk_count = _freeze_chunks(root, entries, job["source_snapshot"])
    (root / "compiler-base.md").write_text(SKILL_BODY, encoding="utf-8")
    (root / "revision.txt").write_text(revision, encoding="utf-8")
    chunk = _read(root / "chunks/00000000.json")
    ref = "scope-source"
    for index in range(count):
        item = {"item_id": f"item-method-{index:08d}", "kind": "method",
                "task": f"Perform the original supported operation {index:08d}",
                "boundary": "Preserve the explicitly supplied procedure and its conditions",
                "source_refs": [ref], "source_files": [chunk["source_uri"]]}
        _save(root / "catalog/method" / f"{index:08d}.json",
              {"item": item, "unit_id": _alias(f"original-unit-{index}"), "ordinals": [0],
               "bindings": {ref: [0]}, "leaf_items": [f"original-leaf-{index:08d}"]})
    _save(root / "catalog/manifest.json", {"method": count, "fact": 0, "gap": 0})
    prepare_workflow_order(root, count)
    state = {"protocol": "te-staged-compile.v1", "planning_version": 2, "round": 1, "phase": "merge_index",
             "merge_batch_protocol": MERGE_BATCH_PROTOCOL, "workflow_planning_protocol": WORKFLOW_PLANNING_PROTOCOL,
             "base_sha256": _sha(SKILL_BODY), "language_policy": {
                 "language": "en", "version": 1, "basis": "frozen_original_source"},
             "catalog_counts": {"method": count, "fact": 0, "gap": 0}, "chunk_count": chunk_count,
             "cursor": 0, "proposal_count": 0, "completed_tasks": 0, "attempted_tasks": 0,
             "estimated_tasks": count + 1, "current": None}
    job = await owner._staged_save(job, root, state, status="preparing")
    return await owner._staged_next(job, root, state)


async def finish(owner, job):
    for _ in range(200):
        if job["status"] in {"waiting_human", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Bounded method merge did not converge")


def test_eight_independent_windows_share_a_compile_with_complete_exact_once_lineage(tmp_path):
    async def check():
        owner = MergeOwner(tmp_path)
        result = await finish(owner, await seeded(owner, 1000))
        assert result["status"] == "waiting_human", result.get("error")
        root, state = await owner._staged_load(result)
        assert state["test_merge_complete"] and state["cursor"] == state["proposal_count"] == 1000
        packets = [packet for batch in owner.ov.merge_batches for packet in batch]
        assert len(packets) == 125 and len(owner.ov.merge_batches) == 16
        assert all(1 <= len(batch) <= 8 for batch in owner.ov.merge_batches)
        assigned = [item["item_id"] for packet in packets for item in packet["derived_controls"]["items"]]
        assert len(assigned) == len(set(assigned)) == 1000
        assert all(len(packet["derived_controls"]["items"]) <= 8 for packet in packets)
        proposals = [_read(path) for path in sorted((root / "proposals").glob("*.json"))]
        leaves = [leaf for row in proposals for leaf in row["leaf_items"]]
        assert sorted(leaves) == [f"original-leaf-{index:08d}" for index in range(1000)]
        assert all(row["ordinals"] == [0] and row["item"]["source_files"]
                   for row in proposals)
        assert all(request["from"][0] in owner.ov.directories for _, request in owner.ov.calls)
        for path in (root / "children").glob("*/metadata.json"):
            _verified(root, _read(path))
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["missing", "foreign", "duplicate", "incomplete", "native_ids", "missing_resolved"])
def test_partial_success_durably_retains_siblings_and_repairs_only_the_bad_window(tmp_path, failure):
    async def check():
        owner = MergeOwner(tmp_path)
        owner.ov.break_windows[8] = [failure]
        job = await seeded(owner, 32)
        root, initial = await owner._staged_load(job)
        old = copy.deepcopy(initial["current"])
        frozen_request = (root / old["request_file"]).read_bytes()
        old_task = copy.deepcopy(owner.ov.tasks[job["ov_task_id"]])
        job = await owner._advance_staged_round(job, old_task)
        state = (await owner._staged_load(job))[1]
        assert state["cursor"] == state["proposal_count"] == 0
        assert [len(batch) for batch in owner.ov.merge_batches] == [4, 1]
        assert owner.ov.merge_batches[1][0]["output_path"] == "merge-00000008.json"
        assert len([path for path in (root / "merge-batches/outcomes").glob("*.json") if _read(path)["valid"]]) == 3
        assert (root / old["request_file"]).read_bytes() == frozen_request
        # A late response from the old child cannot consume the next accepted child or increase progress.
        before = copy.deepcopy(state)
        await owner._advance_merge_batch(job, root, state, old_task)
        assert state == before
        result = await finish(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        state = (await owner._staged_load(result))[1]
        assert state["proposal_count"] == 32 and state["completed_tasks"] == 2
        assert len(list((root / "proposals").glob("*.json"))) == 32

    asyncio.run(check())


def test_exhausted_missing_window_never_discards_successful_siblings_or_advances_catalog(tmp_path):
    async def check():
        owner = MergeOwner(tmp_path)
        owner.ov.break_windows[8] = ["missing"] * 3
        result = await finish(owner, await seeded(owner, 32))
        root, state = await owner._staged_load(result)
        assert result["status"] == "failed" and "Merge repair limit" in result["error"]
        assert state["cursor"] == state["proposal_count"] == 0
        assert [len(batch) for batch in owner.ov.merge_batches] == [4, 1, 1]
        assert len([path for path in (root / "merge-batches/outcomes").glob("*.json") if _read(path)["valid"]]) == 3
        assert not list((root / "proposals").glob("*.json"))

    asyncio.run(check())


@pytest.mark.parametrize("ack", ["lost", "malformed"])
def test_unknown_ack_lookup_and_stop_preserve_the_same_native_task_and_request(tmp_path, ack):
    async def check():
        owner = MergeOwner(tmp_path)
        owner.ov.lose_ack = ack == "lost"
        owner.ov.malformed_merge_ack = ack == "malformed"
        job = await seeded(owner, 32)
        root, state = await owner._staged_load(job)
        old = copy.deepcopy(state["current"])
        assert not job.get("ov_task_id") and len(owner.ov.accepted) == 1
        frozen = (root / old["request_file"]).read_bytes()
        stopped = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        recovered = MergeOwner(tmp_path)
        recovered.ov = recovered.fixture.ov = owner.ov
        job = await recovered._resume_staged_round(stopped)
        assert job["ov_task_id"] == owner.ov.accepted[old["key"]][0] and job["status"] == "stopping"
        job = await recovered._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "stopped" and len(owner.ov.calls) == 1
        assert (root / old["request_file"]).read_bytes() == frozen
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["packet", "lineage", "ledger", "manifest"])
def test_frozen_window_tampering_is_rejected_without_repost_or_proposal_commit(tmp_path, tamper):
    async def check():
        owner = MergeOwner(tmp_path)
        job = await seeded(owner, 32)
        root, state = await owner._staged_load(job)
        current = state["current"]
        manifest = _read(root / current["packet_file"])
        if tamper == "packet":
            path = root / manifest[0]["local_path"]
            value = _read(path)
            value["derived_controls"]["items"][0]["task"] = "Changed method"
        elif tamper == "lineage":
            index = manifest[0]["context"]["record_indexes"][0]
            path = root / "catalog/method" / f"{index:08d}.json"
            value = _read(path)
            value["leaf_items"] = ["unverified-origin"]
        elif tamper == "ledger":
            path = root / current["ledger_file"]
            value = _read(path)
            value["base_proposal_count"] += 1
        else:
            path, value = root / current["packet_file"], manifest
            value[0]["output_path"] = "another-window.json"
        _save(path, value)
        with pytest.raises(ValueError):
            await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == 1 and not list((root / "proposals").glob("*.json"))
        assert current["task_id"] == job["ov_task_id"]

    asyncio.run(check())


def test_restart_after_proposal_write_before_state_commit_is_idempotent(tmp_path, monkeypatch):
    async def check():
        owner = MergeOwner(tmp_path)
        job = await seeded(owner, 32)
        root, _ = await owner._staged_load(job)
        task_id, update = job["ov_task_id"], owner.repo.update_job
        raised = False

        def fail_once(job_id, **changes):
            nonlocal raised
            if changes.get("ov_task_id") == "" and not raised:
                raised = True
                raise OSError("Public job metadata interrupted after exact merge commit")
            return update(job_id, **changes)

        monkeypatch.setattr(owner.repo, "update_job", fail_once)
        with pytest.raises(OSError):
            await owner._advance_staged_round(job, owner.ov.tasks[task_id])
        monkeypatch.setattr(owner.repo, "update_job", update)
        result = await owner._advance_staged_round(owner.repo.get_job(job["job_id"]), owner.ov.tasks[task_id])
        state = (await owner._staged_load(result))[1]
        assert result["status"] == "waiting_human" and state["proposal_count"] == 32
        assert state["completed_tasks"] == 1 and len(owner.ov.calls) == 1
        assert len(list((root / "proposals").glob("*.json"))) == 32

    asyncio.run(check())


def test_transient_content_error_keeps_child_key_and_does_not_consume_format_attempt(tmp_path):
    async def check():
        owner = MergeOwner(tmp_path)
        job = await seeded(owner, 32)
        root, state = await owner._staged_load(job)
        old = copy.deepcopy(state["current"])
        owner.ov.read_status = 503
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        ledger = _read(root / old["ledger_file"])
        assert all(slot["format_attempts"] == 0 for slot in ledger["slots"])
        assert (await owner._staged_load(job))[1]["current"]["key"] == old["key"]
        owner.ov.read_status = 0
        result = await finish(owner, job)
        assert result["status"] == "waiting_human" and len(owner.ov.calls) == 1

    asyncio.run(check())


def test_complete_item_id_headers_and_current_feedback_remain_scoped_non_evidence(tmp_path):
    async def check():
        owner = MergeOwner(tmp_path)
        owner.ov.break_windows[0] = ["item_header"]
        note = {"feedback": "Preserve the verified operation boundaries.", "review_questions": [
            {"qid": "review-one", "question": "Check the conditions in the assigned source."}]}
        result = await finish(owner, await seeded(owner, 8, revision=json.dumps(note)))
        assert result["status"] == "waiting_human", result.get("error")
        packet = owner.ov.merge_batches[0][0]
        assert packet["evidence"] == [] and packet["revision_context_is_evidence"] is False
        assert note["feedback"] in packet["revision_context"]
        assert all(len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) <= 32000
                   for value in owner.ov.merge_batches[0])

    asyncio.run(check())


def test_fresh_protocol_integrates_extraction_selection_and_complete_twelve_case_artifact(tmp_path):
    async def check():
        owner = MergeOwner(tmp_path, full=True)
        result = await drive(owner, await begin(owner, 8))
        assert result["status"] == "waiting_human" and result["candidate_count"] == 1, result.get("error")
        state = (await owner._staged_load(result))[1]
        assert state["merge_batch_protocol"] == MERGE_BATCH_PROTOCOL
        assert owner.ov.merge_batches
        dataset = _read(owner.repo.workspace(result) / "compiled_skill/refund-review/benchmark.json")
        assert len(dataset["cases"]) == len({row["case_id"] for row in dataset["cases"]}) == 12
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


def test_merge_renderer_contains_valid_inline_schema_and_isolates_its_complete_custom_policy():
    custom = "Full generic workflow policy " + "p" * 12000
    base = "---\nname: custom-miner\ndescription: Custom policy\n---\n" + mining_policy_block(
        common=custom, stages={"merge_index": "merge-specific", "merge_batch": "batch-specific",
                               "plan": "selection-policy-must-not-leak"})
    legacy = stage_compiler_body(SKILL_BODY, "merge_index")
    body = render_merge_batch_compiler(base)
    contract = json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
    instructions = contract["synthesize"]["instructions"]
    assert instructions.count(custom) == 1
    assert instructions.count("merge-specific") == instructions.count("batch-specific") == 1
    assert "selection-policy-must-not-leak" not in body
    assert contract["synthesize"]["input_unit"] == "file" and contract["synthesize"]["execution"] == "direct"
    assert "not a resource or file" in instructions
    example_text = instructions.split("Example for one assigned method m1/container u1: ", 1)[1]
    example = json.loads(example_text.split(". ", 1)[0])
    item = {"item_id": "m1", "kind": "method", "task": "Preserve the assigned workflow",
            "boundary": "Preserve its source-supported boundary", "source_refs": ["ref"]}
    planning.validate_merge(example, items=[item], input_ids=["u1"])
    assert stage_compiler_body(SKILL_BODY, "merge_index") == legacy


@pytest.mark.parametrize("cap", [0, 9, True, 1.5])
def test_merge_renderer_rejects_invalid_batch_caps(cap):
    with pytest.raises(ValueError):
        render_merge_batch_compiler(SKILL_BODY, cap)
