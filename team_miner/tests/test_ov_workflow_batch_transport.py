"""Real host transport, with synthetic independent public Compile responses only."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from fastapi import HTTPException

from team_miner import compile_admission as admission
from team_miner.direct_workflow_planning import FrozenPlanningIntegrityError
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import STAGED_PROTOCOL, _json, _package, _read, _save, _sha
from team_miner.ov_workflow_batch_transport import WORKFLOW_BATCH_PROTOCOL, _verified
from team_miner.ov_workflow_batch_transport import _instruction as workflow_instruction
from team_miner.tests.test_ov_case_batch_orchestration import BatchFake
from team_miner.tests.test_ov_staged_orchestration import Owner
from team_miner.workflow_metadata import (
    OUTPUT_SCHEMA as METADATA_SCHEMA,
)
from team_miner.workflow_metadata import (
    build_workflow_metadata,
    validate_workflow_metadata_manifest,
    validate_workflow_metadata_output,
)
from team_miner.workflow_partition import (
    CAPACITY_SCHEMA,
    OUTPUT_SCHEMA,
    build_workflow_partition,
    restore_workflow_partition,
    validate_workflow_partition_manifest,
    validate_workflow_partition_output,
)
from team_miner.workflow_partition_compiler import (
    render_workflow_metadata_compiler,
    render_workflow_partition_compiler,
)

POLICY = {"version": 1, "language": "zh-CN", "basis": "frozen_original_source"}


class WorkflowFake(BatchFake):
    def __init__(self):
        super().__init__()
        self.workflow_batches = []
        self.failures = {}
        self.post_status = 0
        self.workflow_lost_ack = False
        self.foreign_output = False

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if (method != "POST" or path != "/api/v1/compile"
                or "/stages/workflow-batch-" not in request["from"][0]):
            return await super().request(account, method, path, **kwargs)
        if self.post_status:
            raise HTTPException(self.post_status, "Definite workflow request rejection")
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert request["from"][0] in self.directories
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_workflow_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(text) for uri, text in sorted(self.contents.items()) if uri.startswith(prefix)]
            assert 1 <= len(packets) <= 8
            self.workflow_batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
            for packet in packets:
                controls = packet["derived_controls"]
                errors = self.failures.get(packet["output_path"], [])
                failure = errors.pop(0) if errors else ""
                if failure == "missing":
                    self.tasks[task_id]["meta"] = {"pipeline": {"errors": ["Independent Map output missing"]}}
                    continue
                if packet["stage"] == "workflow_partition":
                    value = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                             "rows": [[identity, identity] for identity in controls["assigned_ids"]], "reasons": {}}
                    if failure == "capacity":
                        value = {"schema_version": CAPACITY_SCHEMA,
                                 "assignment_sha256": controls["assignment_sha256"],
                                 "input_ids": controls["assigned_ids"], "reason": "独立方法需更小窗口"}
                    if failure == "invalid":
                        value["rows"] = [["m9999", "m9999"]]
                else:
                    value = {"schema_version": METADATA_SCHEMA,
                             "assignment_sha256": controls["assignment_sha256"],
                             "candidate": {"name": "refund-review", "task": "根据政策复核退款申请",
                                           "trigger": "收到退款申请", "inputs": ["退款申请", "原始政策"],
                                           "outputs": ["复核结论"], "boundary": "仅适用所给政策",
                                           "rationale": "流程完整"}}
                    if failure == "invalid":
                        value["candidate"]["name"] = "Bad Name"
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
            if self.foreign_output:
                self.contents[request["to"] + "/foreign.json"] = "{}"
        if self.workflow_lost_ack:
            self.workflow_lost_ack = False
            raise httpx.ReadTimeout("Accepted Native receipt was lost")
        return {"task_id": task_id}


class WorkflowOwner(Owner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = WorkflowFake()
        self.accepted_slots = []
        self.finished = []
        self.validation_calls = 0

    def _workflow_compiler(self, root, state, stage):
        renderer = (render_workflow_partition_compiler if stage == "workflow_partition"
                    else render_workflow_metadata_compiler)
        return renderer(SKILL_BODY)

    async def _workflow_validate_slot(self, root, state, slot):
        self.validation_calls += 1
        validator = (validate_workflow_partition_manifest if slot["packet"]["stage"] == "workflow_partition"
                     else validate_workflow_metadata_manifest)
        try:
            validator(slot["context"]["manifest"], controls=slot["packet"]["derived_controls"])
        except ValueError as exc:
            raise FrozenPlanningIntegrityError(str(exc)) from exc

    async def _workflow_accept_slot(self, job, root, state, slot, value):
        validator = (validate_workflow_partition_output if slot["packet"]["stage"] == "workflow_partition"
                     else validate_workflow_metadata_output)
        checked = validator(value, slot["context"]["manifest"])
        self.accepted_slots.append(slot["packet"]["output_path"])
        return checked

    async def _workflow_batch_finished(self, job, root, state, ledger):
        assert all(slot["status"] == "valid" for slot in ledger["slots"])
        self.finished.append(copy.deepcopy(ledger))
        state.update(current=None, complete=True)
        return await self._staged_save(job, root, state, status="waiting_human", ov_task_id="")


def record(index):
    ref = "scope-original-" + str(index)
    item = {"item_id": "method-original-" + str(index), "kind": "method", "task": "根据原始政策复核退款",
            "boundary": "仅适用已提交政策", "source_refs": [ref],
            "source_files": ["viking://resources/policy-" + str(index) + ".md"]}
    return {"item": item, "bindings": {ref: [index]}, "ordinals": [index], "unit_id": "unit-" + str(index),
            "leaf_items": [item["item_id"]]}


def slot(index, stage):
    partition = build_workflow_partition([record(index)])
    if stage == "workflow_partition":
        built = partition
    else:
        controls = partition["controls"]
        output = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                  "rows": [[identity, identity] for identity in controls["assigned_ids"]], "reasons": {}}
        group = restore_workflow_partition(output, partition["manifest"])["independent"][0]
        metadata_controls, metadata_manifest = build_workflow_metadata(group)
        built = {"controls": metadata_controls, "manifest": metadata_manifest}
    packet = {"schema_version": "te-staged-compile.packet.v1", "stage": stage,
              "output_path": f"workflow-output-{index:03d}.json", "evidence": [],
              "derived_controls": built["controls"], "output_language_policy": POLICY}
    return {"packet": packet, "context": {"manifest": built["manifest"], "original_index": index},
            "status": "pending", "format_attempts": 0, "error": ""}


async def begin(owner, stage="workflow_partition", count=8, mutate=None):
    job, _ = owner.new()
    job = owner.repo.update_job(job["job_id"], staged_execution_protocol=admission.OPTIMIZED_PROTOCOL)
    root = owner._staged_root(job)
    state = {"protocol": STAGED_PROTOCOL, "round": 1, "phase": stage, "language_policy": POLICY,
             "execution_protocol": admission.OPTIMIZED_PROTOCOL,
             "completed_tasks": 0, "attempted_tasks": 0, "estimated_tasks": 1}
    ledger = {"protocol": WORKFLOW_BATCH_PROTOCOL, "stage": stage, "before": 0, "after": count,
              "slots": [slot(index, stage) for index in range(count)]}
    if mutate:
        mutate(ledger)
    job = await owner._submit_workflow_batch(job, root, state, "workflow-tests/ledger.json", ledger)
    return job, root, _read(root / "state.json")


async def finish(owner, job):
    for _ in range(5):
        if job["status"] in {"waiting_human", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Workflow batch did not terminate within its finite test assignments")


@pytest.mark.parametrize("stage", ["workflow_partition", "workflow_metadata"])
def test_eight_independent_packets_use_one_unchanged_native_map(tmp_path, stage):
    async def check():
        owner = WorkflowOwner(tmp_path)
        job, root, state = await begin(owner, stage)
        _verified(root, state["current"])
        job = await finish(owner, job)
        assert job["status"] == "waiting_human"
        assert len(owner.ov.calls) == 1 and len(owner.ov.workflow_batches[0]) == 8
        assert len(owner.accepted_slots) == 8
        assert owner.validation_calls >= 24
        assert not admission.reservations(owner.repo.jobs_root)
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["invalid", "missing"])
def test_partial_output_repairs_only_bad_slot_and_keeps_frozen_siblings(tmp_path, failure):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.failures["workflow-output-003.json"] = [failure]
        job, root, state = await begin(owner)
        old = copy.deepcopy(state["current"])
        request = (root / old["request_file"]).read_bytes()
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert len(owner.ov.workflow_batches[1]) == 1
        assert len(owner.accepted_slots) == 7
        repair = owner.ov.workflow_batches[1][0]
        original = owner.ov.workflow_batches[0][3]
        assert repair["output_path"] == original["output_path"]
        assert repair["evidence"] == original["evidence"]
        assert repair["derived_controls"]["assignment_sha256"] == original["derived_controls"]["assignment_sha256"]
        assert repair["derived_controls"]["repair_error"]
        assert (root / old["request_file"]).read_bytes() == request
        final = await finish(owner, job)
        assert final["status"] == "waiting_human" and len(owner.accepted_slots) == 8
        assert not admission.reservations(owner.repo.jobs_root)
    asyncio.run(check())


def test_capacity_is_valid_host_receipt_without_a_format_retry(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.failures["workflow-output-000.json"] = ["capacity"]
        job, _, _ = await begin(owner)
        final = await finish(owner, job)
        assert final["status"] == "waiting_human" and len(owner.ov.calls) == 1
        ledger = owner.finished[0]
        assert ledger["slots"][0]["acceptance"]["schema_version"] == CAPACITY_SCHEMA
        assert all(slot["format_attempts"] == 0 for slot in ledger["slots"])
    asyncio.run(check())


def test_three_failed_slot_attempts_are_finite_and_keep_accepted_siblings(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.failures["workflow-output-001.json"] = ["invalid"] * 3
        job, root, _ = await begin(owner)
        final = await finish(owner, job)
        assert final["status"] == "failed" and "repair limit" in final["error"]
        assert [len(packets) for packets in owner.ov.workflow_batches] == [8, 1, 1]
        ledger = _read(root / "workflow-tests/ledger.json")
        assert ledger["slots"][1]["format_attempts"] == 3
        assert len(owner.accepted_slots) == 7 and not owner.finished
    asyncio.run(check())


@pytest.mark.parametrize("stop", [False, True])
def test_lost_ack_restart_recovers_exact_native_key_and_stop_never_submits_new_work(tmp_path, stop):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.workflow_lost_ack = True
        job, root, state = await begin(owner)
        old = copy.deepcopy(state["current"])
        frozen = (root / old["request_file"]).read_bytes()
        if stop:
            job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        second = WorkflowOwner(tmp_path)
        second.ov = owner.ov
        job = await second._resume_workflow_batch(job, root, _read(root / "state.json"))
        assert len(second.ov.calls) == 1 and job["ov_task_id"] == owner.ov.accepted[old["key"]][0]
        if stop:
            assert job["status"] == "stopping"
            job = await second._advance_staged_round(job, second.ov.tasks[job["ov_task_id"]])
            assert job["status"] == "stopped" and not second.accepted_slots
        else:
            assert (await finish(second, job))["status"] == "waiting_human"
        assert (root / old["request_file"]).read_bytes() == frozen
    asyncio.run(check())


@pytest.mark.parametrize("damage", ["packet", "before", "after", "manifest"])
def test_frozen_host_tampering_fails_before_any_new_native_call(tmp_path, damage):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.workflow_lost_ack = True
        job, root, state = await begin(owner)
        current = state["current"]
        ledger = _read(root / current["ledger_file"])
        if damage in {"before", "after"}:
            ledger[damage] += 1
        elif damage == "packet":
            ledger["slots"][0]["packet"]["derived_controls"]["assignment_sha256"] = "changed"
        else:
            ledger["slots"][0]["context"]["manifest"]["assignment_sha256"] = "bad"
        _save(root / current["ledger_file"], ledger)
        job = await owner._resume_workflow_batch(job, root, state)
        assert job["status"] == "failed" and len(owner.ov.calls) == 1
        assert not owner.accepted_slots
    asyncio.run(check())


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
def test_definite_native_rejection_does_not_leak_admission(tmp_path, status):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.post_status = status
        with pytest.raises(HTTPException):
            await begin(owner)
        assert not admission.reservations(owner.repo.jobs_root) and not owner.ov.accepted
    asyncio.run(check())


def test_foreign_directory_file_cannot_be_consumed_as_an_assigned_output(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.foreign_output = True
        job, _, _ = await begin(owner)
        final = await finish(owner, job)
        assert final["status"] == "failed" and "unexpected output" in final["error"]
        assert len(owner.accepted_slots) == 8 and not owner.finished
    asyncio.run(check())


def test_preflight_host_manifest_failure_cannot_spend_a_native_call(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        original = owner._workflow_validate_slot
        async def reject(root, state, slot):
            await original(root, state, slot)
            raise FrozenPlanningIntegrityError("Original catalog SHA changed")
        owner._workflow_validate_slot = reject
        job, _, _ = await begin(owner)
        assert job["status"] == "failed" and "catalog SHA changed" in job["error"]
        assert not owner.ov.calls and not admission.reservations(owner.repo.jobs_root)
    asyncio.run(check())


def test_accepted_sibling_receipt_tamper_is_rejected_even_when_repair_sends_only_the_bad_slot(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        owner.ov.failures["workflow-output-003.json"] = ["missing"]
        job, root, _ = await begin(owner)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        state = _read(root / "state.json")
        ledger = _read(root / state["current"]["ledger_file"])
        ledger["slots"][0]["acceptance"]["rows"] = [["m9999", "m9999"]]
        _save(root / state["current"]["ledger_file"], ledger)
        job = await owner._resume_workflow_batch(job, root, state)
        assert job["status"] == "failed" and len(owner.ov.calls) == 2
        assert not owner.finished
    asyncio.run(check())


def test_transient_output_read_reuses_exact_native_without_consuming_format_attempts(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        job, root, state = await begin(owner)
        old = copy.deepcopy(state["current"])
        original = owner._knowledge_request
        async def unavailable(account, method, path, **kwargs):
            if path == "/api/v1/content/read" and kwargs["params"]["uri"].startswith(old["output_uri"]):
                raise HTTPException(503, "Temporary content outage")
            return await original(account, method, path, **kwargs)
        owner._knowledge_request = unavailable
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        ledger = _read(root / old["ledger_file"])
        assert all(row["format_attempts"] == 0 for row in ledger["slots"])
        assert _read(root / "state.json")["current"]["key"] == old["key"]
        owner._knowledge_request = original
        assert (await finish(owner, job))["status"] == "waiting_human"
        assert len(owner.ov.calls) == 1
    asyncio.run(check())


def test_repair_reserve_bounds_complete_40k_packet_and_escaped_diagnostic(tmp_path):
    async def check():
        owner = WorkflowOwner(tmp_path)
        published = _package(owner._workflow_compiler(None, {}, "workflow_partition"))[1]
        budget = min(40000 - 3072, 48000 - len(published) - len(workflow_instruction(POLICY)) - 3072)
        def fill(ledger):
            packet = ledger["slots"][0]["packet"]
            packet["revision_context"] = {"text": "", "is_evidence": False}
            packet["revision_context"]["text"] = "x" * (budget - len(_json(packet)))
            assert len(_json(packet)) == budget
        original = owner._workflow_accept_slot
        attempts = 0
        async def reject_once(job, root, state, slot, value):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("\x00" * 1500 + "完整错误保留本地" * 100)
            return await original(job, root, state, slot, value)
        owner._workflow_accept_slot = reject_once
        job, root, _ = await begin(owner, count=1, mutate=fill)
        final = await finish(owner, job)
        assert final["status"] == "waiting_human" and len(owner.ov.calls) == 2
        first, repair = [batch[0] for batch in owner.ov.workflow_batches]
        assert first["revision_context"] == repair["revision_context"]
        assert repair["derived_controls"]["repair_error_excerpted"]
        assert len(_json(repair["derived_controls"]["repair_error"])) <= 2000
        assert all(len(_json(p)) <= 40000 and len(_json(p)) + len(published)
                   + len(workflow_instruction(POLICY)) <= 48000 for batch in owner.ov.workflow_batches for p in batch)
        outcomes = list((root / "children").glob("*/*.outcome.json"))
        assert any(len(_read(p)["error"]) > len(repair["derived_controls"]["repair_error"]) for p in outcomes)
    asyncio.run(check())


def test_outcome_saved_before_ledger_crash_recovers_without_repeating_acceptance(tmp_path, monkeypatch):
    import team_miner.ov_workflow_batch_transport as transport
    async def check():
        owner = WorkflowOwner(tmp_path)
        job, root, state = await begin(owner, count=1)
        original_host = transport._host
        original_save = original_host()[7]
        crashed = False
        def crash(path, value):
            nonlocal crashed
            if (path == root / state["current"]["ledger_file"]
                    and value["slots"][0]["status"] == "valid" and not crashed):
                crashed = True
                raise OSError("Ledger write interrupted after exact acceptance outcome")
            original_save(path, value)
        def fake_host():
            values = list(original_host())
            values[7] = crash
            return tuple(values)
        monkeypatch.setattr(transport, "_host", fake_host)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert crashed and len(owner.accepted_slots) == 1
        final = await finish(owner, job)
        assert final["status"] == "waiting_human" and len(owner.accepted_slots) == 1
        assert len(owner.ov.calls) == 1
    asyncio.run(check())


@pytest.mark.parametrize("crash_stage", ["workflow_partition", "workflow_metadata"])
def test_real_workflow_projection_restart_does_not_duplicate_candidates_or_advance_past_inputs(
    tmp_path, monkeypatch, crash_stage,
):
    import team_miner.ov_workflow_planning as workflow
    owner = WorkflowOwner(tmp_path)
    job, _ = owner.new()
    root = owner._staged_root(job)
    state = {"phase": "workflow_partition", "language_policy": POLICY, "workflow_revision": "",
             "workflow_packet_budget": 35000, "workflow_controls_budget": 32000,
             "workflow_partition_count": 0, "workflow_group_count": 0, "selection_cursor": 0}
    for index in range(12):
        row = record(index)
        _save(root / "catalog/method" / f"{index:08d}.json", row)
        uri = row["item"]["source_files"][0]
        _save(root / "source-files" / (_sha(uri) + ".json"), {"uri": uri, "ordinals": [index]})
    state["workflow_partition_count"] = workflow._partition_assignments(root, state, list(range(12)), start=0)

    def accepted(stage, folder, count, before=0):
        slots = []
        for index in range(before, before + count):
            context = _read(root / f"workflow-planning/{folder}/{index:08d}.json")
            assigned = workflow._assignment(root, context)
            controls = assigned["packet"]["derived_controls"]
            if stage == "workflow_partition":
                value = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                         "rows": [[identity, identity] for identity in controls["assigned_ids"]], "reasons": {}}
            else:
                value = {"schema_version": METADATA_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                         "candidate": {"name": "refund-review", "task": "根据政策复核退款申请",
                                       "trigger": "收到退款申请", "inputs": ["退款申请", "原始政策"],
                                       "outputs": ["复核结论"], "boundary": "仅适用所给政策", "rationale": "流程完整"}}
            slots.append({"packet": assigned["packet"], "context": context, "status": "valid",
                          "format_attempts": 0, "error": "", "acceptance": value})
        return {"protocol": WORKFLOW_BATCH_PROTOCOL, "stage": stage,
                "before": before, "after": before + count, "slots": slots}

    partition = accepted("workflow_partition", "queue", state["workflow_partition_count"])
    original_io = workflow._io
    original_save = original_io()[3]
    crashed = False
    def crash(path, value):
        nonlocal crashed
        if path == root / "plan.json" and state["phase"] == crash_stage and not crashed:
            crashed = True
            raise OSError("Projection interrupted before immutable commit journal")
        original_save(path, value)
    def fake_io():
        values = list(original_io())
        values[3] = crash
        return tuple(values)
    monkeypatch.setattr(workflow, "_io", fake_io)
    if crash_stage == "workflow_partition":
        with pytest.raises(OSError):
            owner._workflow_apply_batch(root, state, partition)
    owner._workflow_apply_batch(root, state, partition)
    owner._workflow_apply_batch(root, state, partition)
    assert state["selection_cursor"] == 12 and state["workflow_group_count"] == 12
    assert state["workflow_partition_cursor"] == partition["after"]
    state["phase"] = "workflow_metadata"
    metadata = accepted("workflow_metadata", "metadata-queue", 8)
    if crash_stage == "workflow_metadata":
        with pytest.raises(OSError):
            owner._workflow_apply_batch(root, state, metadata)
    owner._workflow_apply_batch(root, state, metadata)
    owner._workflow_apply_batch(root, state, metadata)
    assert state["workflow_metadata_cursor"] == 8
    tail = accepted("workflow_metadata", "metadata-queue", 4, before=8)
    owner._workflow_apply_batch(root, state, tail)
    owner._workflow_apply_batch(root, state, tail)
    plan = _read(root / "plan.json")
    assert crashed and len(plan["skills"]) == 12
    assert len({row["name"] for row in plan["skills"]}) == 12
    assert state["selection_cursor"] == 12 and state["workflow_metadata_cursor"] == 12
    assert len(list((root / "method-decisions").glob("*.json"))) == 12


@pytest.mark.parametrize("damage", ["cursor", "cursor_with_hash", "routing", "acceptance"])
def test_committed_workflow_replay_rejects_corrupt_counts_and_a_different_accepted_ledger(tmp_path, damage):
    import team_miner.ov_workflow_planning as workflow
    owner = WorkflowOwner(tmp_path)
    job, _ = owner.new()
    root = owner._staged_root(job)
    state = {"phase": "workflow_partition", "language_policy": POLICY, "workflow_revision": "",
             "workflow_packet_budget": 35000, "workflow_controls_budget": 32000,
             "workflow_partition_count": 0, "workflow_group_count": 0, "selection_cursor": 0}
    _save(root / "catalog/method/00000000.json", record(0))
    state["workflow_partition_count"] = workflow._partition_assignments(root, state, [0], start=0)
    context = _read(root / "workflow-planning/queue/00000000.json")
    assigned = workflow._assignment(root, context)
    controls = assigned["packet"]["derived_controls"]
    ledger = {"protocol": WORKFLOW_BATCH_PROTOCOL, "stage": "workflow_partition", "before": 0, "after": 1,
              "slots": [{"packet": assigned["packet"], "context": context, "status": "valid",
                         "format_attempts": 0, "error": "",
                         "acceptance": {"schema_version": OUTPUT_SCHEMA,
                                        "assignment_sha256": controls["assignment_sha256"],
                                        "rows": [[identity, identity] for identity in controls["assigned_ids"]],
                                        "reasons": {}}}]}
    before = copy.deepcopy(state)
    owner._workflow_apply_batch(root, state, ledger)
    journal = root / "workflow-planning/commits/workflow_partition-00000000.json"
    receipt = _read(journal)
    if damage == "cursor":
        receipt["changes"]["selection_cursor"] = 999
        _save(journal, receipt)
    elif damage == "cursor_with_hash":
        receipt["changes"]["workflow_partition_cursor"] = 999
        receipt["changes_sha256"] = _sha(_json(receipt["changes"]))
        _save(journal, receipt)
    elif damage == "routing":
        ledger["after"] = 2
    else:
        ledger["slots"][0]["acceptance"]["rows"] = [["m9999", "m9999"]]
    replay = copy.deepcopy(before)
    with pytest.raises(FrozenPlanningIntegrityError, match="journal|ledger receipt"):
        owner._workflow_apply_batch(root, replay, ledger)
    assert replay == before
