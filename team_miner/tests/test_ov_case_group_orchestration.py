"""Grouped output changes transport, never the twelve individually validated Case contract."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from team_miner import bounded_compile_plan as planning
from team_miner.compile_strategy import mining_policy_block, render_case_batch_compiler
from team_miner.ov_case_group_orchestration import (
    CASE_GROUP_CAPACITY_SCHEMA,
    CASE_GROUP_COMPILER_STAGE,
    CASE_GROUP_PROTOCOL,
    CASE_GROUP_SCHEMA,
    MAX_GROUP_OUTPUT_CHARS,
    decode_group,
    render_case_group_compiler,
    verified_group_batch,
)
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _json, _read, _save, _sha
from team_miner.tests.dataset_fixtures import build_document
from team_miner.tests.test_direct_workflow_planning import DirectFake
from team_miner.tests.test_extract_batches import begin
from team_miner.tests.test_local_scope_pipeline import LocalScopeOwner
from team_miner.tests.test_ov_case_batch_orchestration import BatchFake, BatchOwner
from team_miner.tests.test_ov_staged_orchestration import drive, start
from team_miner.tests.test_round_reuse import POLICY, next_inputs


class GroupFake(BatchFake):
    def __init__(self):
        super().__init__()
        self.group_batches = []
        self.group_break_slots = {}
        self.case_capacity_above = 4
        self.lose_group_ack = False
        self.group_same_key_503 = False
        self.group_extra = False

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/case_batch-" not in request["from"][0]:
            return await super().request(account, method, path, **kwargs)
        prefix = request["from"][0] + "/"
        packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents)
                   if uri.startswith(prefix) and not uri.rsplit("/", 1)[-1].startswith(".")]
        if not packets or "cases" not in packets[0]["derived_controls"]:
            return await super().request(account, method, path, **kwargs)
        if len(request["from"]) != 1 or request["from"][0] not in self.directories:
            raise HTTPException(422, "Compile source must be a directory")
        assert 1 <= len(packets) <= 8
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if self.group_same_key_503:
            self.group_same_key_503 = False
            raise HTTPException(503, "Temporary public admission failure")
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_group_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            self.group_batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed",
                                   "meta": {"pipeline": {"warnings": ["Partial output"], "errors": []}}}
            for packet in packets:
                controls = packet["derived_controls"]
                assignments = controls["cases"]
                if len(assignments) > self.case_capacity_above:
                    value = {"schema_version": CASE_GROUP_CAPACITY_SCHEMA,
                             "case_ids": [row["case_id"] for row in assignments],
                             "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in packet["evidence"])),
                             "reason": "Complete assignments require smaller output groups."}
                else:
                    value = {"schema_version": CASE_GROUP_SCHEMA, "cases": [], "deferred": []}
                    for own in assignments:
                        index = planning.CASE_DIMENSIONS.index(own["coverage_dimension"])
                        failures = self.group_break_slots.get(index, [])
                        failure = failures.pop(0) if failures else ""
                        if failure == "missing":
                            continue
                        case = build_document(controls["skill_name"], [{"query": "Fixture"}])["cases"][0]
                        case["case_id"] = own["case_id"]
                        case["query"] = "根据原文执行不同验证场景 " + own["coverage_dimension"] + own["case_id"]
                        case["provenance"]["session_ids"] = own["allowed_source_refs"]
                        if failure == "invalid":
                            case["checks"] = case["checks"][:1]
                        elif failure == "foreign":
                            case["provenance"]["session_ids"] = ["viking://unassigned/foreign.md"]
                        elif failure == "cross":
                            case["provenance"]["session_ids"] = assignments[-1]["allowed_source_refs"]
                        elif failure == "duplicate-query":
                            case["query"] = value["cases"][0]["query"] if value["cases"] else "Identical query"
                        elif failure == "materials-object":
                            case["materials"] = {"scenario": "原文中的场景", "source_dialogue": "原文中的对话"}
                        elif failure == "materials-text":
                            case["materials"] = "  原文中的场景\n原文中的对话：保留标点和换行。\n"
                        if failure == "deferred":
                            value["deferred"].append({"case_id": own["case_id"], "reason": "原文缺完整条件",
                                                      "source_refs": own["allowed_source_refs"]})
                        else:
                            value["cases"].append(case)
                            if failure == "duplicate-id":
                                value["cases"].append(copy.deepcopy(case))
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
            if self.group_extra:
                self.contents[request["to"] + "/unexpected.json"] = "{}"
        if self.lose_group_ack:
            self.lose_group_ack = False
            raise httpx.ReadTimeout("Group accepted before ACK was lost")
        return {"task_id": task_id}


class GroupFlowFake(DirectFake, GroupFake):
    pass


class GroupOwner(LocalScopeOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = GroupFlowFake()

    def new(self, count=8):
        job, entries = super().new(count)
        for index, entry in enumerate(entries):
            text = f"Procedure {index}: collect the request, verify the supplied policy and explain the decision.\n"
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return self.repo.update_job(job["job_id"], staged_case_group_protocol=CASE_GROUP_PROTOCOL), entries


async def to_group(owner, count=8):
    job = await begin(owner, count)
    for _ in range(500):
        root, state = await owner._staged_load(job)
        if (state.get("current") or {}).get("input_protocol") == CASE_GROUP_PROTOCOL:
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("No Case group was submitted")


def assigned_indices(owner):
    return [planning.CASE_DIMENSIONS.index(own["coverage_dimension"])
            for batch in owner.ov.group_batches for packet in batch for own in packet["derived_controls"]["cases"]]


def test_three_four_case_groups_complete_twelve_cases_in_one_native_request(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        job, root, state = await to_group(owner)
        assert state["case_group_protocol"] == CASE_GROUP_PROTOCOL
        current = state["current"]
        assert current["input_protocol"] == CASE_GROUP_PROTOCOL
        assert [len(packet["derived_controls"]["cases"]) for packet in owner.ov.group_batches[0]] == [4, 4, 4]
        final = await drive(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        assert len(owner.ov.group_batches) == 1 and not owner.ov.batches
        dataset = _read(owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json")
        assert len(dataset["cases"]) == 12
        assert len({planning.normalize_case_query(case["query"]) for case in dataset["cases"]}) == 12
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert all(slot["status"] == "valid" for slot in ledger["slots"])
        body = (root / current["compiler_file"]).read_text()
        request = _read(root / current["request_file"])
        for locator in _read(root / current["packet_file"]):
            packet = _read(root / locator["local_path"])
            assert len(_json(packet)) <= 32000
            assert len(body) + len(_json(packet)) + len(request["instruction"]) <= 48000
            assert len(packet["evidence"]) <= 8 and len(_json(packet["evidence"])) <= 26000
    asyncio.run(check())


@pytest.mark.parametrize("failure,expected", [
    ("materials-object", [
        {"name": "scenario", "content": "原文中的场景"},
        {"name": "source_dialogue", "content": "原文中的对话"},
    ]),
    ("materials-text", [{"content": "  原文中的场景\n原文中的对话：保留标点和换行。\n"}]),
])
def test_material_shape_is_losslessly_normalized_without_remote_repair(tmp_path, failure, expected):
    async def check():
        owner = GroupOwner(tmp_path)
        owner.ov.group_break_slots[0] = [failure]
        job, root, _ = await to_group(owner)
        final = await drive(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        assert len(owner.ov.group_batches) == 1
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][0]["status"] == "valid"
        assert ledger["slots"][0]["format_attempts"] == 0
        dataset = _read(owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json")
        assert dataset["cases"][0]["materials"] == expected
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["invalid", "missing", "foreign", "duplicate-id", "duplicate-query"])
def test_partial_group_only_bad_case_is_repaired_and_siblings_remain_durable(tmp_path, failure):
    async def check():
        owner = GroupOwner(tmp_path)
        owner.ov.group_break_slots[1] = [failure]
        job, root, _ = await to_group(owner)
        final = await drive(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        indices = assigned_indices(owner)
        assert indices.count(1) == 2
        assert all(indices.count(index) == 1 for index in range(12) if index != 1)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][1]["format_attempts"] == 1
    asyncio.run(check())


def test_capacity_splits_only_pending_groups_and_falls_back_to_original_case_receiver(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        owner.ov.case_capacity_above = 1
        final = await drive(owner, await begin(owner, 8))
        assert final["status"] == "waiting_human", final.get("error")
        assert len(owner.ov.group_batches) == 2  # 3x4, then 6x2; singletons use the Case receiver.
        assert len(owner.ov.batches) == 2
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert all(slot["format_attempts"] == 0 for slot in ledger["slots"])
        assert len(_read(owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12
    asyncio.run(check())


def test_group_lost_ack_resume_and_stop_never_submit_a_replacement(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        owner.ov.lose_group_ack = True
        job, root, state = await to_group(owner)
        original = copy.deepcopy(state["current"])
        calls = len(owner.ov.calls)
        recovered = await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == calls
        _, state = await owner._staged_load(recovered)
        assert state["current"]["child"] == original["child"]
        assert recovered["ov_task_id"]
        stopped = owner.repo.update_job(job["job_id"], status="stopping")
        await owner._resume_staged_round(stopped)
        assert len(owner.ov.calls) == calls
    asyncio.run(check())


def test_group_same_key_503_retries_exact_frozen_request(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        owner.ov.group_same_key_503 = True
        job, root, state = await to_group(owner)
        current = copy.deepcopy(state["current"])
        calls = len(owner.ov.calls)
        resumed = await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == calls + 1
        assert owner.ov.calls[-1] == owner.ov.calls[-2]
        assert _read(root / current["request_file"]) == owner.ov.calls[-1][1]
        assert resumed["ov_task_id"]
    asyncio.run(check())


def test_single_case_transport_completes_twelve_cases_in_two_native_requests(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        final = await drive(owner, await start(owner, count=12))
        assert final["status"] == "waiting_human", final.get("error")
        root, state = await owner._staged_load(final)
        assert not state.get("case_group_protocol")
        assert len(owner.ov.batches) == 2
        assert not (root / "aux-compilers" / (CASE_GROUP_COMPILER_STAGE + ".md")).exists()
    asyncio.run(check())


def test_grouped_reviewed_round_reuses_all_twelve_cases_and_compiler_profile(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        job, entries = owner.new(8)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        before = len(owner.ov.group_batches)
        second, entries = await next_inputs(owner, first)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["reused_candidate_count"] == 1
        assert len(owner.ov.group_batches) == before
        assert len(_read(owner.repo.workspace(second) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12
        assert _read(root / "reuse/manifest.json")["candidate_count"] == 1
    asyncio.run(check())


def test_renderer_keeps_old_hash_and_actual_synthesize_individual_gates():
    before = render_case_batch_compiler(SKILL_BODY)
    body = render_case_group_compiler(SKILL_BODY)
    assert before == render_case_batch_compiler(SKILL_BODY)
    contract = json.loads(body.split("```json\n")[-1].split("\n```", 1)[0])
    instructions = contract["synthesize"]["instructions"]
    assert CASE_GROUP_SCHEMA in instructions and CASE_GROUP_CAPACITY_SCHEMA in instructions
    assert "each Case JSON still at most 12000" in instructions
    assert "ONLY that assignment's allowed_source_refs" in instructions
    assert "12-24" in instructions and "1-8" in instructions
    assert "p.map(sources" in body and "p.finalize(files" in body
    assert len(body) < 16000


def test_renderer_preserves_business_policy_bytes_even_when_they_match_owned_rules():
    shared = (
        "KEEP POLICY START\n"
        "Each Compile reads one TE-owned bounded packet file.\n"
        "Return only the current complete canonical case JSON, never a dataset or sibling cases.\n"
        "assigned case_id and skill_name from controls; derived_controls.coverage_dimension\n"
        "original source_uri values from THIS raw Evidence group.\n"
        "return the explicit deferred object instead.\n"
        "Other cases are separate assignments.\n"
        "KEEP POLICY END"
    )
    common = "COMMON:\n" + shared
    custom = {stage: stage.upper() + ":\n" + shared for stage in ("case", "case_batch", CASE_GROUP_COMPILER_STAGE)}
    source = "---\nname: custom-policy\ndescription: User policy fixture.\n---\n" + mining_policy_block(common, custom)
    old = render_case_batch_compiler(source)
    rendered = render_case_group_compiler(source)
    contract = json.loads(rendered.split("```json\n")[-1].split("\n```", 1)[0])
    instructions = contract["synthesize"]["instructions"]
    assert instructions.count(common) == 1
    assert all(instructions.count(policy) == 1 for policy in custom.values())
    assert "Current per-file stage: case.\nReturn one bounded group" in instructions
    assert render_case_batch_compiler(source) == old


def test_renderer_identity_field_paths_match_the_actual_frozen_group_packet(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        _, root, state = await to_group(owner)
        packet = owner.ov.group_batches[0][0]
        controls = packet["derived_controls"]
        assert controls["skill_name"] == "refund-review"
        assert all("case_id" in row and "skill_name" not in row for row in controls["cases"])
        body = (root / state["current"]["compiler_file"]).read_text()
        contract = json.loads(body.split("```json\n")[-1].split("\n```", 1)[0])
        instructions = contract["synthesize"]["instructions"]
        assert "derived_controls.cases[i].case_id and the shared derived_controls.skill_name" in instructions
        assert "shared skill_name from derived_controls.cases" not in instructions
    asyncio.run(check())


@pytest.mark.parametrize("limit", [True, 0, 9, "8"])
def test_renderer_rejects_unbounded_map_width(limit):
    with pytest.raises(ValueError):
        render_case_group_compiler(SKILL_BODY, limit)


def test_group_wrapper_never_raises_each_case_output_budget():
    context = {"case_slots": [{"case_id": "custom", "case_index": 0}], "source_refs": ["original"]}
    case = build_document("workflow", [{"query": "Fixture"}])["cases"][0]
    case["case_id"], case["query"] = "custom", "X" * 12000
    checked = decode_group({"schema_version": CASE_GROUP_SCHEMA, "cases": [case], "deferred": []}, context)
    assert checked[0][1] == "Individual Case exceeds 12K"
    assert MAX_GROUP_OUTPUT_CHARS == 49024


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "missing", "single", "extra"])
def test_capacity_has_exact_unique_coverage_and_single_case_remains_bounded(mutation):
    context = {"case_slots": [{"case_id": "A", "case_index": 0}, {"case_id": "B", "case_index": 1}],
               "source_refs": ["first", "second"]}
    value = {"schema_version": CASE_GROUP_CAPACITY_SCHEMA, "case_ids": ["B", "A"],
             "source_refs": ["second", "first"], "reason": "Complete output requires splitting."}
    assert all(row[2] for row in decode_group(value, context).values())
    if mutation == "unknown":
        value["source_refs"][0] = "foreign"
    elif mutation == "duplicate":
        value["source_refs"] = ["first", "first"]
    elif mutation == "missing":
        value["case_ids"].pop()
    elif mutation == "single":
        context["case_slots"].pop()
        value["case_ids"] = ["A"]
    else:
        value["extra"] = True
    with pytest.raises(ValueError, match="capacity"):
        decode_group(value, context)


def test_valid_group_transactions_replay_after_case_file_before_ledger_crash(tmp_path, monkeypatch):
    async def check():
        from team_miner import ov_staged_orchestration as host

        owner = GroupOwner(tmp_path)
        job, root, state = await to_group(owner)
        current = copy.deepcopy(state["current"])
        original_save = host._save
        interrupted = False

        def fail_once(path, value):
            nonlocal interrupted
            if path == root / "case-batches/refund-review/ledger.json" and not interrupted:
                interrupted = True
                raise OSError("Simulated crash after durable Case outcome and file")
            return original_save(path, value)

        monkeypatch.setattr(host, "_save", fail_once)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert interrupted
        assert _read(root / "case-batches/refund-review/ledger.json")["slots"][0]["status"] == "pending"
        original_case = (root / "cases/refund-review/00.json").read_bytes()
        monkeypatch.setattr(host, "_save", original_save)
        final = await drive(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        assert len(owner.ov.group_batches) == 1
        assert (root / "cases/refund-review/00.json").read_bytes() == original_case
        assert _read(root / "state.json")["completed_tasks"] == state["completed_tasks"] + 1
        assert _read(root / current["request_file"]) == owner.ov.accepted[current["key"]][1]
    asyncio.run(check())


@pytest.mark.parametrize("mutation", ["case_scope", "source_roles", "packet", "compiler", "nested"])
def test_resume_rejects_frozen_group_tampering_without_new_post(tmp_path, mutation):
    async def check():
        owner = GroupOwner(tmp_path)
        job, root, state = await to_group(owner)
        current = state["current"]
        locators = _read(root / current["packet_file"])
        if mutation in {"case_scope", "source_roles"}:
            row = locators[0]
            packet = _read(root / row["local_path"])
            if mutation == "case_scope":
                foreign = row["context"]["case_slots"][0]["source_refs"][:1]
                assert foreign != row["context"]["case_slots"][0]["source_refs"]
                row["context"]["case_slots"][0]["source_refs"] = foreign
                packet["derived_controls"]["cases"][0]["allowed_source_refs"] = foreign
            else:
                row["context"]["case_slots"][0]["method_input_ids"] = []
                packet["derived_controls"]["cases"][0]["method_input_ids"] = []
            (root / row["local_path"]).write_text(_json(packet))
            row["sha256"] = _sha(_json(packet))
            _save(root / current["packet_file"], locators)
            current["packet_sha256"] = _sha(_json(locators) + "\n")
            _save(root / "state.json", state)
        elif mutation == "packet":
            (root / locators[0]["local_path"]).write_text("{}")
        elif mutation == "compiler":
            (root / "aux-compilers" / (current["compiler_stage"] + ".md")).write_text("changed")
        else:
            owner.ov.input_extras[current["input_uri"] + "/.hidden/unassigned.json"] = "{}"
        calls = len(owner.ov.calls)
        result = await owner._resume_staged_round(job)
        assert result["status"] == "failed"
        assert len(owner.ov.calls) == calls
        assert result["ov_task_id"]  # Accepted Native identity stays available for exact stop/audit.
    asyncio.run(check())


def test_repeated_invalid_single_case_uses_finite_original_repair_limit(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        owner.ov.group_break_slots[1] = ["invalid", "invalid"]
        owner.ov.break_slots[1] = ["invalid"]
        final = await drive(owner, await begin(owner, 8))
        assert final["status"] == "failed"
        assert "repair limit" in final["error"]
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][1]["format_attempts"] == 3
        assert all(slot["status"] == "valid" for slot in ledger["slots"] if slot["index"] != 1)
        assert len(owner.ov.batches) == 1 and len(owner.ov.group_batches) == 2
    asyncio.run(check())


def test_group_verifier_preserves_each_case_original_group_within_union(tmp_path):
    async def check():
        owner = GroupOwner(tmp_path)
        _, root, state = await to_group(owner)
        _, _, locators, _ = verified_group_batch(root, state["current"])
        for row in locators:
            union = set(row["context"]["input_ids"])
            assert all(set(own["input_ids"]) <= union for own in row["context"]["case_slots"])
            assert all(own["case_group_count"] >= 1 for own in row["context"]["case_slots"])
    asyncio.run(check())


def test_large_lossless_source_unions_reduce_group_width_without_truncating_originals(tmp_path):
    class LargeOwner(GroupOwner):
        def new(self, count=8):
            job, entries = LocalScopeOwner.new(self, count)
            return self.repo.update_job(job["job_id"], staged_case_group_protocol=CASE_GROUP_PROTOCOL), entries

    async def check():
        owner = LargeOwner(tmp_path)
        final = await drive(owner, await begin(owner, 8))
        assert final["status"] == "waiting_human", final.get("error")
        root, state = await owner._staged_load(final)
        assert state["chunk_count"] > 8
        assert sorted(assigned_indices(owner)) == list(range(12))
        assert any(len(packet["derived_controls"]["cases"]) < 4
                   for batch in owner.ov.group_batches for packet in batch)
        for batch in owner.ov.group_batches:
            assert len(batch) <= 8
            for packet in batch:
                assert len(packet["evidence"]) <= 8 and len(_json(packet["evidence"])) <= 26000
                for chunk in packet["evidence"]:
                    ordinal = _read(root / "chunk-ids" / (chunk["id"] + ".json"))["ordinal"]
                    assert chunk == _read(root / "chunks" / f"{ordinal:08d}.json")
        assert len(_read(owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12
    asyncio.run(check())
