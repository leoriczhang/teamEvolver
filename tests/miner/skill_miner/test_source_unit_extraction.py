"""Exact units run inside the existing native file Map, with isolated bounded repairs."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_extract_batch_orchestration import _verified
from team_miner.skill_miner.ov_staged_orchestration import _read
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from team_miner.skill_miner.source_unit_extraction import (
    bind_source_unit_index,
    build_source_unit_packet,
    next_source_unit_assignment,
    render_source_unit_extract_compiler,
    verify_source_unit_context,
)
from team_miner.skill_miner.source_units import PROTOCOL, SourceUnitCapacityError, load_source_units
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner, begin, finish_extract
from tests.miner.skill_miner.support.source_units import frozen

POLICY = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}


def assignment(root, text):
    _, manifest = frozen(root, [text], max_chars=12000)
    context = next_source_unit_assignment(root, cursor=0, count=manifest["unit_count"],
                                          unit_index=0, language_policy=POLICY)
    return manifest, context, build_source_unit_packet(root, context, POLICY)


def output_for(packet):
    refs = packet["derived_controls"]["allowed_source_refs"]
    rows = [ref for ref, unit in zip(refs, packet["evidence"], strict=True) if unit["kind"] == "table_row"]
    other = [ref for ref in refs if ref not in rows]
    return {
        "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": ["native-container"],
        "methods": [{"task": "完整独立复核方法", "boundary": "仅按该行原始条件", "source_refs": [ref]}
                    for ref in rows],
        "facts": [{"text": "原始结构与背景", "source_refs": other}] if other else [],
        "deferred": [], "context_refs": [], "context_uncertain": False,
    }


def test_distinct_rows_in_one_old_chunk_receive_distinct_exact_scopes(tmp_path):
    text = "| 类别 | 方法 |\n| --- | --- |\n| A | 先核实再确认 |\n| B | 拒绝后结束 |\n"
    _, context, packet = assignment(tmp_path, text)
    raw = output_for(packet)
    original = copy.deepcopy(raw)
    checked = bind_source_unit_index(tmp_path, raw, packet, context, "child::index-00000000.json")
    assert raw == original
    scopes = [method["source_refs"][0] for method in checked["value"]["methods"]]
    assert len(set(scopes)) == 2
    assert checked["bindings"][scopes[0]] == checked["bindings"][scopes[1]] == [0]
    assert checked["source_unit_bindings"][scopes[0]] != checked["source_unit_bindings"][scopes[1]]
    assert all(len(checked["source_unit_bindings"][scope]) == 1 for scope in scopes)


@pytest.mark.parametrize("reference", ["viking://resources/source-0.md", "unassigned-unit", "10001"])
def test_exact_scope_rejects_whole_file_and_unassigned_aliases(tmp_path, reference):
    _, context, packet = assignment(tmp_path, "| 任务 |\n| --- |\n| 完整复核 |\n")
    raw = output_for(packet)
    raw["methods"][0]["source_refs"] = [reference]
    with pytest.raises(ValueError, match="assigned source-unit handles"):
        bind_source_unit_index(tmp_path, raw, packet, context, "child")


def test_full_unit_ids_restore_only_the_same_assigned_aliases(tmp_path):
    _, context, packet = assignment(tmp_path, "| 任务 |\n| --- |\n| 完整复核 |\n")
    raw = output_for(packet)
    ref = raw["methods"][0]["source_refs"][0]
    raw["methods"][0]["source_refs"] = [packet["derived_controls"]["reference_map"][ref]]
    checked = bind_source_unit_index(tmp_path, raw, packet, context, "child")
    assert checked["source_unit_bindings"][checked["value"]["methods"][0]["source_refs"][0]] == [
        packet["derived_controls"]["reference_map"][ref],
    ]


def test_assignment_budgets_complete_units_and_covers_primary_units_once(tmp_path):
    _, manifest = frozen(tmp_path, ["# 章节\n\n| 项 | 说明 |\n| --- | --- |\n" + "| 甲 | 条件。 |\n" * 50])
    cursor, primary, packets = 0, [], []
    while cursor < manifest["unit_count"]:
        context = next_source_unit_assignment(tmp_path, cursor=cursor, count=manifest["unit_count"],
                                              unit_index=len(packets), language_policy=POLICY, max_packet_chars=6000)
        packet = build_source_unit_packet(tmp_path, context, POLICY)
        verify_source_unit_context(tmp_path, context)
        primary.extend(context["assigned_unit_ids"])
        packets.append(packet)
        cursor = context["after"]
    assert primary == [unit["id"] for unit in manifest["units"]]
    assert len(packets) > 1
    assert all(len(planning._json(packet)) <= 6000 for packet in packets)
    for packet in packets[1:]:
        assert {unit["kind"] for unit in packet["evidence"]} >= {"heading", "table_header"}


def test_oversize_unit_fails_explicit_capacity_with_full_parent_archived(tmp_path):
    text = "```text\n" + "长代码" * 12000 + "\n```\n"
    _, manifest = frozen(tmp_path, [text])
    with pytest.raises(SourceUnitCapacityError, match="input-capacity limit, not missing Evidence"):
        next_source_unit_assignment(tmp_path, cursor=0, count=manifest["unit_count"],
                                    unit_index=0, language_policy=POLICY)
    units = load_source_units(tmp_path, list(range(manifest["unit_count"])))
    assert len(units) == 1 and units[0]["content"] == text


def test_context_cannot_swap_in_a_neighbour_or_wrong_original_ordinal(tmp_path):
    _, context, _ = assignment(tmp_path, "# 标题\n\n段落甲\n\n段落乙\n")
    for field in ("source_unit_ids", "assigned_unit_ids", "ordinals", "input_ids"):
        bad = copy.deepcopy(context)
        bad[field] = bad[field][1:]
        with pytest.raises(ValueError):
            verify_source_unit_context(tmp_path, bad)


class ExactExtractFake(ExtractFake):
    pass


class ExactExtractOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = ExactExtractFake()

    def new(self, count=3):
        job, entries = super().new(count)
        job = self.repo.update_job(job["job_id"], staged_source_scope_protocol=SOURCE_SCOPE_PROTOCOL_V3,
                                   staged_workflow_protocol="te-direct-workflow-planning.v1",
                                   staged_source_unit_protocol=PROTOCOL)
        for index, entry in enumerate(entries):
            text = f"# 方法 {index}\n\n| 条件 | 完整方法 |\n| --- | --- |\n" + "".join(
                f"| 条件 {row} | " + "首先核实输入，获得确认后执行；" * 80 + "完成。 |\n" for row in range(24)
            )
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries


@pytest.mark.asyncio
async def test_exact_units_use_existing_map_batches_and_preserve_original_chunks(tmp_path):
    owner = ExactExtractOwner(tmp_path)
    job = await begin(owner, 3)
    root, initial = await owner._staged_load(job)
    assert initial["source_unit_protocol"] == PROTOCOL
    original = {path.name: path.read_bytes() for path in (root / "chunks").glob("*.json")}
    job = await finish_extract(owner, job)
    assert job["status"] == "waiting_human", job.get("error")
    root, state = await owner._staged_load(job)
    assert state["source_unit_cursor"] == state["source_unit_count"]
    assert state["cursor"] == state["chunk_count"]
    packets = [packet for batch in owner.ov.extract_batches for packet in batch]
    assigned = [identity for packet in packets for identity in packet["derived_controls"]["assigned_unit_ids"]]
    manifest = _read(root / "source-units/manifest.json")
    assert assigned == [unit["id"] for unit in manifest["units"]]
    assert len(owner.ov.extract_batches) == (len(packets) + 7) // 8 < len(packets)
    assert original == {path.name: path.read_bytes() for path in (root / "chunks").glob("*.json")}
    for path in (root / "units/0").glob("*.json"):
        result = _read(path)
        assert result["source_unit_protocol"] == PROTOCOL and result["source_unit_bindings"]
    for path in (root / "extract-batches/accepted").glob("*.json"):
        child = _read(root / "children" / _read(path)["child"] / "metadata.json")
        _verified(root, child)
    assert not admission.reservations(owner.repo.jobs_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "foreign", "context_foreign"])
async def test_exact_unit_repair_keeps_successful_siblings_and_original_assignment(tmp_path, failure):
    owner = ExactExtractOwner(tmp_path)
    owner.ov.break_indexes[1] = [failure]
    job = await begin(owner, 3)
    first = copy.deepcopy(owner.ov.extract_batches[0])
    assert len(first) > 1
    job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    repair = owner.ov.extract_batches[1]
    assert len(repair) == 1 and repair[0]["output_path"] == first[1]["output_path"]
    assert repair[0]["evidence"] == first[1]["evidence"]
    assert repair[0]["derived_controls"]["assigned_unit_ids"] == first[1]["derived_controls"]["assigned_unit_ids"]
    job = await finish_extract(owner, job)
    assert job["status"] == "waiting_human", job.get("error")
    outputs = [packet["output_path"] for batch in owner.ov.extract_batches for packet in batch]
    assert outputs.count(first[0]["output_path"]) == 1 and outputs.count(first[1]["output_path"]) == 2


def test_exact_compiler_reuses_index_v2_contract_and_prohibits_chunk_widening():
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY

    rendered = render_source_unit_extract_compiler(SKILL_BODY)
    assert "skill-miner.index.v2" in rendered and "te-exact-source-units.v1" in rendered
    assert "not automatically shared business rules" in rendered
    assert "different rows sharing one original chunk" in rendered
