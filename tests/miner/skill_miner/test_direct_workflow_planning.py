"""Direct planning keeps bounded controls, exact method dispositions and original lineage."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import mining_policy_block
from team_miner.skill_miner.direct_workflow_planning import (
    CAPACITY_SCHEMA,
    DIRECT_WORKFLOW_PROTOCOL,
    MAX_DIRECT_METHODS,
    MAX_RELATED_CONTEXT_CHARS,
    related_context,
    validate_capacity,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.bounded_compile_plan import candidate, planning_catalog
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner, begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs
from tests.miner.skill_miner.support.source_scope import sources


def selected(count=24, groups=8):
    units, items = planning_catalog(count)
    value = {
        "schema_version": planning.SELECTION_SCHEMA, "input_ids": [unit["id"] for unit in units],
        "skills": [{**{key: value for key, value in candidate([]).items() if key != "source_refs"},
                    "name": f"workflow-{index}",
                    "derived_from": [item["item_id"] for item in items[index::groups]]}
                   for index in range(groups)],
        "knowledge": [], "deferred": [], "resolved": [],
    }
    return items, value


def test_twenty_four_methods_keep_exact_once_dispositions_and_all_source_lineage():
    items, value = selected()
    original = copy.deepcopy(value)
    checked = planning.validate_selection(value, items=items, input_ids=value["input_ids"],
                                          max_input_items=MAX_DIRECT_METHODS)
    assert len(checked["skills"]) == 8 and value == original
    for row in checked["skills"]:
        assigned = [item for item in items if item["item_id"] in row["derived_from"]]
        assert row["source_refs"] == sorted({ref for item in assigned for ref in item["source_refs"]})
    assert sorted(identity for row in checked["skills"] for identity in row["derived_from"]) == sorted(
        item["item_id"] for item in items
    )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "source_refs"])
def test_direct_window_does_not_relax_dispositions_or_allow_model_source_rewrites(mutation):
    items, value = selected()
    if mutation == "missing":
        value["skills"][0]["derived_from"].pop()
    elif mutation == "duplicate":
        value["skills"][1]["derived_from"].append(value["skills"][0]["derived_from"][0])
    elif mutation == "unknown":
        value["skills"][0]["derived_from"].append("foreign-item")
    else:
        value["skills"][0]["source_refs"] = ["invented-source"]
    with pytest.raises(ValueError):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"], max_input_items=24)


def test_legacy_window_and_candidate_output_caps_remain_unchanged():
    items, value = selected(9, 1)
    with pytest.raises(ValueError, match="eight methods"):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"])
    items, value = selected(25, 1)
    with pytest.raises(ValueError, match="24 methods"):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"], max_input_items=24)
    items, value = selected(24, 9)
    with pytest.raises(ValueError, match="eight candidates"):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"], max_input_items=24)


@pytest.mark.parametrize("limit", [True, 0, 25, "24"])
def test_method_cap_is_a_bounded_integer(limit):
    items, value = selected(1, 1)
    with pytest.raises(ValueError, match="integer"):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"], max_input_items=limit)


def capacity():
    return {"schema_version": CAPACITY_SCHEMA, "input_ids": ["shared-container"],
            "source_refs": ["scope-1", "scope-2"], "reason": "These independent workflows require smaller windows."}


def test_capacity_checks_exact_container_ids_without_mistaking_them_for_method_count():
    value = capacity()
    assert validate_capacity(value, ["shared-container"], ["scope-2", "scope-1"])
    assert not validate_capacity({"schema_version": planning.SELECTION_SCHEMA}, [], [])
    assert not validate_capacity([], [], [])


@pytest.mark.parametrize("field,replacement", [
    ("input_ids", ["unknown"]), ("input_ids", ["shared-container", "shared-container"]),
    ("source_refs", ["scope-1"]), ("source_refs", ["scope-1", "scope-2", "foreign"]),
    ("source_refs", ["scope-1", "scope-1", "scope-2"]), ("reason", " "), ("reason", "x" * 1001),
    ("skills", []),
])
def test_capacity_cannot_hide_missing_assigned_ids_or_turn_into_empty_success(field, replacement):
    value = capacity()
    value[field] = replacement
    with pytest.raises(ValueError):
        validate_capacity(value, ["shared-container"], ["scope-1", "scope-2"])


def context_fixture(tmp_path, *, notes=3):
    root, state = sources(tmp_path, ["Original method and separate unrelated appendix.\n" * 650,
                                    "Globally shared original constraint."])
    state.update(direct_workflow_protocol=DIRECT_WORKFLOW_PROTOCOL, efficiency_protocol="te-mining-efficient.v2",
                 catalog_counts={"method": 1, "fact": notes, "gap": 0})
    last = state["chunk_count"] - 1
    records = [{"item": {"kind": "method"}, "ordinals": [0]}]
    for index in range(notes):
        ordinal = (0, 1, last)[index % 3]
        _save(root / "catalog/fact" / f"{index:08d}.json",
              {"item": {"kind": "fact", "text": f"Readonly note {index}", "item_id": f"fact-{index}"},
               "ordinals": [ordinal]})
    return root, state, records


def test_readonly_context_uses_exact_fragments_and_verified_distant_shared_constraints(tmp_path):
    root, state, records = context_fixture(tmp_path)
    before = {path.name: path.read_bytes() for path in (root / "chunks").glob("*.json")}
    result = related_context(root, state, records)
    assert [note["text"] for note in result["related_facts"]] == ["Readonly note 0", "Readonly note 2"]
    assert [note["relation"] for note in result["related_facts"]] == [
        "method_fragment", "verified_shared_constraint"
    ]
    assert all("item_id" not in note and "source_refs" not in note for note in result["related_facts"])
    assert result["related_facts"][1]["source_fragment_ids"] == [
        _read(root / "chunk-metadata" / f"{state['chunk_count'] - 1:08d}.json")["id"]
    ]
    assert result["readonly_context_audit"]["shared_context_verified"]
    assert before == {path.name: path.read_bytes() for path in (root / "chunks").glob("*.json")}


@pytest.mark.parametrize("incomplete", [False, True])
def test_old_or_incomplete_indexes_do_not_pretend_whole_file_fallback_is_verified_shared_context(tmp_path, incomplete):
    root, state, records = context_fixture(tmp_path)
    if incomplete:
        state["unit_count"] -= 1
    else:
        path = root / "units/0/00000000.json"
        unit = _read(path)
        unit["value"]["schema_version"] = planning.INDEX_SCHEMA
        _save(path, unit)
    result = related_context(root, state, records)
    assert [note["text"] for note in result["related_facts"]] == ["Readonly note 0"]
    assert not result["readonly_context_audit"]["shared_context_verified"]


def test_actual_escaped_json_budget_omits_whole_notes_without_clipping_fragment_ids(tmp_path):
    root, state, records = context_fixture(tmp_path, notes=1001)
    for index in range(1001):
        path = root / "catalog/fact" / f"{index:08d}.json"
        record = _read(path)
        record["ordinals"] = [0]
        record["item"]["text"] = '\\"\n' * 280
        _save(path, record)
    result = related_context(root, state, records)
    audit = result["readonly_context_audit"]
    assert len(planning._json(result)) <= MAX_RELATED_CONTEXT_CHARS
    assert result["context_omitted"] and audit["matched_notes"] == 1001
    assert audit["omitted_notes"] + audit["selected_before_authority_budget"] == 1001
    assert all(note["text"] == '\\"\n' * 280 and len(note["source_fragment_ids"]) == 1
               for note in result["related_facts"])
    assert len(list((root / "catalog/fact").glob("*.json"))) == 1001


def test_context_rejects_foreign_round_symlinks_and_wrong_protocol(tmp_path):
    root, state, records = context_fixture(tmp_path)
    with pytest.raises(ValueError, match="protocol"):
        related_context(root, {**state, "direct_workflow_protocol": "old"}, records)
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps(_read(root / "catalog/fact/00000000.json")))
    path = root / "catalog/fact/00000000.json"
    path.unlink()
    path.symlink_to(foreign)
    with pytest.raises(ValueError, match="own frozen round|symbolic"):
        related_context(root, state, records)


class DirectFake(ExtractFake):
    def __init__(self):
        super().__init__()
        self.capacity_above = 0
        self.capacity_ack_lost = False
        self.selection_packets = []
        self.force_capacity = False
        self.large_context = False

    async def request(self, account, method, path, **kwargs):
        accepted = await super().request(account, method, path, **kwargs)
        body = kwargs.get("json", {})
        if method == "POST" and path == "/api/v1/compile" and "/stages/extract_batch-" in body["from"][0]:
            prefix = body["from"][0] + "/"
            for uri, text in list(self.contents.items()):
                if not uri.startswith(prefix):
                    continue
                packet = json.loads(text)
                if self.large_context:
                    target = body["to"] + "/" + packet["output_path"]
                    output = json.loads(self.contents[target])
                    output["methods"][0].update(task="M" * 350, boundary="B" * 1000)
                    output["facts"] = [{"text": f"Context {index}: " + "C" * 970,
                                        "source_refs": packet["derived_controls"]["allowed_source_refs"]}
                                       for index in range(6)]
                    self.contents[target] = json.dumps(output, ensure_ascii=False)
                if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                    refs = packet["derived_controls"]["allowed_source_refs"]
                    self.contents[body["to"] + "/" + packet["output_path"]] = json.dumps({
                        "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": packet["input_ids"],
                        "methods": [], "facts": [{"text": "本轮复核没有新增领域规则。", "source_refs": refs}],
                        "deferred": [], "context_refs": [], "context_uncertain": False,
                    }, ensure_ascii=False)
        if method != "POST" or path != "/api/v1/compile" or "/stages/plan-" not in body["from"][0]:
            return accepted
        packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
        controls = packet["derived_controls"]
        self.selection_packets.append(copy.deepcopy(packet))
        uri = body["to"] + "/selection.json"
        output = json.loads(self.contents[uri])
        if self.force_capacity or self.capacity_above and len(controls["items"]) > self.capacity_above:
            output = {"schema_version": CAPACITY_SCHEMA, "input_ids": controls["input_ids"],
                      "source_refs": controls["allowed_source_refs"], "reason": "Independent workflow capacity."}
        else:
            row = output["skills"][0]
            row["derived_from"] = [item["item_id"] for item in controls["items"]]
            known = next((candidate for candidate in controls.get("known_candidates", [])
                          if candidate["name"] == row["name"]), None)
            if known is not None:
                row = {"reuse_candidate_id": known["candidate_id"], "derived_from": row["derived_from"],
                       "reason": "同一原文方法及其全部来源范围未改变。"}
            output["skills"] = [row]
        self.contents[uri] = json.dumps(output, ensure_ascii=False)
        if output["schema_version"] == CAPACITY_SCHEMA and self.capacity_ack_lost:
            self.capacity_ack_lost = False
            raise httpx.ReadTimeout("Capacity acceptance ACK was lost")
        return accepted


class DirectOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root, full=True)
        self.ov = self.fixture.ov = DirectFake()

    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"], staged_workflow_protocol=DIRECT_WORKFLOW_PROTOCOL), entries


def test_fresh_direct_flow_preserves_raw_method_records_and_completes_twelve_cases(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        job = await drive(owner, await begin(owner, 24))
        assert job["status"] == "waiting_human", job.get("error")
        root, state = await owner._staged_load(job)
        assert state["direct_workflow_protocol"] == DIRECT_WORKFLOW_PROTOCOL
        assert not any("merge_batch-" in body["from"][0] or "merge_index-" in body["from"][0]
                       for _, body in owner.ov.calls)
        assert len(owner.ov.selection_packets) == 1
        assert len(owner.ov.selection_packets[0]["derived_controls"]["items"]) > 8
        catalog = {_read(path)["item"]["item_id"]: _read(path)
                   for path in (root / "catalog/method").glob("*.json")}
        proposals = [_read(path) for path in (root / "proposals").glob("*.json")]
        assert all(row == catalog[row["item"]["item_id"]] for row in proposals)
        assert len(proposals) == len(catalog)
        dataset = _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")
        assert len(dataset["cases"]) == 12 and job["candidate_quality"]["refund-review"]["can_submit"]

    asyncio.run(check())


def test_capacity_split_recovers_lost_ack_with_same_key_and_preserves_exact_method_coverage(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        owner.ov.capacity_above = 2
        owner.ov.capacity_ack_lost = True
        job = await begin(owner, 8)
        while True:
            root, state = await owner._staged_load(job)
            if state["current"]["stage"] == "plan":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        current = copy.deepcopy(state["current"])
        assert not job["ov_task_id"]
        frozen = (root / current["request_file"]).read_bytes()
        posts = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == posts and job["ov_task_id"] == owner.ov.accepted[current["key"]][0]
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        root, state = await owner._staged_load(result)
        assert state["capacity_splits"] == 1 and not state.get("direct_window_end")
        assert (root / current["request_file"]).read_bytes() == frozen
        audits = [_read(path) for path in (root / "direct-planning/splits").glob("*.json")]
        assert len(audits) == 1 and audits[0]["record_indexes"] == [0, 1, 2, 3]
        successful = [packet for packet in owner.ov.selection_packets if len(packet["derived_controls"]["items"]) <= 2]
        assigned = [item["item_id"] for packet in successful for item in packet["derived_controls"]["items"]]
        assert len(assigned) == len(set(assigned)) == state["proposal_count"]
        assert len(_read(owner.repo.workspace(result) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12

    asyncio.run(check())


def test_direct_round_two_reuses_only_same_protocol_and_reassembles_complete_dataset(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        job, entries = owner.new(8)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        before = len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["direct_workflow_protocol"] == DIRECT_WORKFLOW_PROTOCOL
        assert state["reused_extract_count"] > 0 and state["reused_candidate_count"] == 1
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before
        assert len(_read(owner.repo.workspace(second) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12

    asyncio.run(check())


async def at_plan(owner, count):
    job = await begin(owner, count)
    while True:
        root, state = await owner._staged_load(job)
        if state["current"]["stage"] == "plan":
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])


@pytest.mark.parametrize("tamper", ["mapping", "proposal"])
def test_direct_original_to_proposal_lineage_tamper_is_rejected_before_dispositions(tmp_path, tamper):
    async def check():
        owner = DirectOwner(tmp_path)
        job, root, state = await at_plan(owner, 2)
        if tamper == "mapping":
            path = root / "direct-planning/mapping/00000000.json"
            value = _read(path)
            value["record_sha256"] = "0" * 64
        else:
            path = root / "proposals/00000000.json"
            value = _read(path)
            value["leaf_items"] = ["foreign-original-method"]
        _save(path, value)
        job = await drive(owner, job)
        assert job["status"] == "failed", job.get("error")
        assert not (root / "plan.json").exists()
        assert not list(owner.repo.workspace(job).glob("compiled_skill/*/benchmark.json"))

    asyncio.run(check())


def test_single_method_capacity_cannot_split_forever_or_claim_a_valid_empty_result(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        owner.ov.force_capacity = True
        job = await drive(owner, await begin(owner, 2))
        assert job["status"] == "failed", job.get("error")
        root, state = await owner._staged_load(job)
        assert state["selection_cursor"] == 0
        assert not state.get("capacity_splits") and not state.get("direct_window_end")
        assert len(owner.ov.selection_packets) == 3
        assert "two exact" in job["error"]
        assert not list(owner.repo.workspace(job).glob("compiled_skill/*/benchmark.json"))

    asyncio.run(check())


def test_final_context_budget_splits_locally_before_native_without_losing_methods(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        owner.ov.large_context = True
        job, entries = owner.new(48)
        policy = mining_policy_block("Retain original Evidence and verify each documented condition. " * 180)
        job = await drive(owner, await owner._prepare_staged_round(job, policy, entries, POLICY, ""))
        assert job["status"] == "waiting_human", job.get("error")
        root, state = await owner._staged_load(job)
        assert state["input_budget_splits"] > 0
        assigned = [item["item_id"] for packet in owner.ov.selection_packets
                    for item in packet["derived_controls"]["items"]]
        assert len(assigned) == len(set(assigned)) == state["proposal_count"] == 24
        assert all(len(planning._json(packet)) <= 32000 for packet in owner.ov.selection_packets)
        assert not state.get("capacity_splits")
        assert len(_read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12

    asyncio.run(check())


def test_adding_new_job_marker_does_not_migrate_an_existing_frozen_legacy_round(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path, full=True)
        job = await begin(owner, 8)
        root, before = await owner._staged_load(job)
        frozen = {path.name: path.read_bytes() for path in (root / "compilers").glob("*.md")}
        job = owner.repo.update_job(job["job_id"], staged_workflow_protocol=DIRECT_WORKFLOW_PROTOCOL)
        job = await drive(owner, job)
        assert job["status"] == "waiting_human", job.get("error")
        _, state = await owner._staged_load(job)
        assert not before.get("direct_workflow_protocol") and not state.get("direct_workflow_protocol")
        assert frozen == {path.name: path.read_bytes() for path in (root / "compilers").glob("*.md")}
        assert any("merge_batch-" in body["from"][0] for _, body in owner.ov.calls)

    asyncio.run(check())
