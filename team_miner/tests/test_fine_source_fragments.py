"""Fresh citation granularity preserves originals, frozen rounds and reviewed artifact dependencies."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import (
    FINE_EXTRACT_MAX_CHUNKS,
    FINE_SOURCE_FRAGMENT_CHARS,
    SOURCE_FRAGMENT_PROTOCOL,
    _alias,
    _bind_unit,
    _freeze_chunks,
    _group_count,
    _raw_group,
    _read,
    _save,
    _sha,
)
from team_miner.round_reuse import PROTOCOL_FIELDS, prepare_round_reuse, try_reuse_candidate, try_reuse_extract
from team_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3, prepare_source_routing, route_candidate_scope
from team_miner.tests.test_direct_workflow_planning import DirectFake
from team_miner.tests.test_ov_staged_orchestration import drive
from team_miner.tests.test_round_reuse import POLICY, next_inputs
from team_miner.tests.test_selection_window_recovery import RecoveryOwner

PREFIX = "viking://resources/frozen"


def freeze(tmp_path, text, *, budget=FINE_SOURCE_FRAGMENT_CHARS):
    path = tmp_path / "source.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    root = tmp_path / "staged"
    entries = [{"kind": "source", "relative_path": "original/source.md", "local_path": str(path),
                "sha256": _sha(text), "content_bytes": len(text.encode())}]
    count = _freeze_chunks(root, entries, PREFIX, max_chars=budget)
    return root, [_read(root / "chunks" / f"{ordinal:08d}.json") for ordinal in range(count)]


@pytest.mark.parametrize("text", [
    "", "短文", "é e\u0301 中文 👩🏽‍💻 \"\\\t\n" * 700,
    "A" * 4095, "B" * 4096, "C" * 12001, "D" * 150000,
    "| 条件 | 动作 | 例外 |\n|---|---|---|\n|" + "长表格单元格\\\"" * 2500 + "|结束动作|保留例外|\n",
])
def test_frozen_small_large_unicode_escaped_and_long_table_inputs_are_lossless(tmp_path, text):
    root, chunks = freeze(tmp_path, text)
    assert "".join(chunk["content"] for chunk in chunks) == text
    assert chunks[0]["start_char"] == 0 and chunks[-1]["end_char"] == len(text)
    assert all(left["end_char"] == right["start_char"] for left, right in zip(chunks, chunks[1:]))
    assert [chunk["ordinal"] for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert text[chunk["start_char"]:chunk["end_char"]] == chunk["content"]
        assert chunk["sha256"] == hashlib.sha256(chunk["content"].encode()).hexdigest()
        assert chunk["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        original = {key: value for key, value in chunk.items() if key != "ordinal"}
        assert len(json.dumps(original, ensure_ascii=False, separators=(",", ":"))) <= 4096
        metadata = _read(root / "chunk-metadata" / f"{chunk['ordinal']:08d}.json")
        assert metadata["serialized_chars"] == len(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")))
    planning.validate_chunks(chunks)


def test_source_slicing_preserves_literal_line_endings_and_header_columns_across_boundaries():
    text = "Start\r\n" + "H" * 3650 + "|Column A|Column B|Column C|\r\n" + "row\\\"👩🏽‍💻|" * 1900
    chunks = planning.source_chunks(text, PREFIX + "/table.md", max_chars=4096)
    assert len(chunks) > 3
    assert "".join(chunk["content"] for chunk in chunks) == text
    assert all(text[chunk["start_char"]:chunk["end_char"]] == chunk["content"] for chunk in chunks)
    assert "|Column A|Column B|Column C|\r\n" in "".join(chunk["content"] for chunk in chunks)
    assert all(chunk["source_sha256"] == _sha(text) for chunk in chunks)


def test_default_coarse_freeze_remains_byte_identical_to_explicit_old_budget(tmp_path):
    text = "原文条件与操作步骤\\\"\n" * 2700
    fine_root, fine = freeze(tmp_path / "fine", text)
    explicit_root, coarse = freeze(tmp_path / "explicit", text, budget=12000)
    entry = {"kind": "source", "relative_path": "original/source.md",
             "local_path": str(tmp_path / "explicit/source.md"), "sha256": _sha(text)}
    default_root = tmp_path / "default"
    count = _freeze_chunks(default_root, [entry], PREFIX)
    assert count == len(coarse) < len(fine)
    for path in explicit_root.rglob("*.json"):
        assert (default_root / path.relative_to(explicit_root)).read_bytes() == path.read_bytes()
    assert _group_count(default_root, range(count)) == _group_count(default_root, range(count), max_chunks=8)
    assert _raw_group(default_root, range(count), 0) == _raw_group(default_root, range(count), 0, max_chunks=8)
    assert fine_root.is_dir()


def test_extract_groups_are_at_most_three_while_writer_default_keeps_its_actual_size_budget(tmp_path):
    root, chunks = freeze(tmp_path, "exact original conditions\n" * 2500)
    count, cursor, seen, groups = len(chunks), 0, [], 0
    while cursor < count:
        group, after = _raw_group(root, range(count), cursor, max_chunks=3)
        assert 1 <= len(group) <= 3
        assert after == cursor + len(group)
        assert len(json.dumps({"evidence": group}, ensure_ascii=False, separators=(",", ":"))) <= 24000
        seen.extend(chunk["ordinal"] for chunk in group)
        cursor, groups = after, groups + 1
    assert seen == list(range(count))
    assert groups == _group_count(root, range(count), max_chunks=3)
    writer, after = _raw_group(root, range(count), 0)
    assert len(writer) > 3 and after == len(writer)
    assert len(json.dumps({"evidence": writer}, ensure_ascii=False, separators=(",", ":"))) <= 24000


def local_routing(root, chunks, offsets):
    anchors = [next(chunk["ordinal"] for chunk in chunks if chunk["start_char"] <= offset < chunk["end_char"])
               for offset in offsets]
    refs = {chunk["ordinal"]: _alias(chunk["id"]) for chunk in chunks}
    methods = set(anchors)
    value = {"schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"] for chunk in chunks],
             "methods": [{"task": f"Supported independent method {index}", "boundary": "Use original conditions",
                          "source_refs": [refs[ordinal]]} for index, ordinal in enumerate(sorted(methods))],
             "facts": [{"text": "Retained original background", "source_refs": [refs[ordinal]]}
                       for ordinal in range(len(chunks)) if ordinal not in methods],
             "deferred": [], "context_refs": [refs[0]], "context_uncertain": False}
    planning.validate_index(value, input_ids=value["input_ids"], source_refs=list(refs.values()), routing=True)
    unit = _bind_unit(value, {ref: [ordinal] for ordinal, ref in refs.items()}, "accepted-original")
    _save(root / "units/0/00000000.json", unit)
    routing = prepare_source_routing(root, {"source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3,
                                          "chunk_count": len(chunks), "unit_count": 1})
    scopes = [route_candidate_scope(root, {"ordinals": list(range(len(chunks))), "method_ordinals": [ordinal]},
                                   routing) for ordinal in anchors]
    return anchors, scopes


def test_fine_original_refs_can_distinguish_methods_inside_one_coarse_range_without_dropping_headers(tmp_path):
    text = "|condition|action|exception|\n" + "X" * 5990 + "METHOD-A" + "X" * 1600 + "METHOD-B" + "X" * 32000
    offsets = [text.index("METHOD-A"), text.index("METHOD-B")]
    coarse_root, coarse = freeze(tmp_path / "coarse", text, budget=12000)
    fine_root, fine = freeze(tmp_path / "fine", text)
    coarse_anchors, coarse_scopes = local_routing(coarse_root, coarse, offsets)
    fine_anchors, fine_scopes = local_routing(fine_root, fine, offsets)
    assert coarse_anchors[0] == coarse_anchors[1]
    assert fine_anchors[0] != fine_anchors[1]
    coarse_chars = sum(len(coarse[ordinal]["content"]) for scope in coarse_scopes for ordinal in scope["ordinals"])
    fine_chars = sum(len(fine[ordinal]["content"]) for scope in fine_scopes for ordinal in scope["ordinals"])
    assert fine_chars < coarse_chars  # Citation precision, not a claim about actual model latency.
    for index, scope in enumerate(fine_scopes):
        raw = "".join(fine[ordinal]["content"] for ordinal in scope["ordinals"])
        assert f"METHOD-{'AB'[index]}" in raw and "|condition|action|exception|" in raw
        assert scope["full_ordinals"] == list(range(len(fine)))
    assert "".join(chunk["content"] for chunk in fine) == text


class FullRecoveryOwner(RecoveryOwner):
    def __init__(self, root, *, fine=True):
        super().__init__(root)
        self.ov = self.fixture.ov = DirectFake()
        self.fine = fine

    def new(self, count=3):
        job, entries = super().new(count)
        marker = SOURCE_FRAGMENT_PROTOCOL if self.fine else ""
        return self.repo.update_job(job["job_id"], staged_source_fragment_protocol=marker), entries


async def reviewed(owner):
    job, entries = owner.new(3)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    return first


def test_fresh_fine_recovery_round_freezes_settings_and_produces_complete_twelve_case_dataset(tmp_path):
    async def check():
        owner = FullRecoveryOwner(tmp_path)
        first = await reviewed(owner)
        root, state = await owner._staged_load(first)
        assert state["source_fragment_protocol"] == SOURCE_FRAGMENT_PROTOCOL
        assert state["source_fragment_chars"] == FINE_SOURCE_FRAGMENT_CHARS == 4096
        assert state["extract_max_chunks"] == FINE_EXTRACT_MAX_CHUNKS == 3
        assert all(len(packet["evidence"]) <= 3 for batch in owner.ov.extract_batches for packet in batch)
        assert all(packet["evidence"] for batch in owner.ov.extract_batches for packet in batch)
        for name in state["candidate_names"]:
            document = _read(owner.repo.workspace(first) / f"compiled_skill/{name}/benchmark.json")
            assert len(document["cases"]) == len({case["case_id"] for case in document["cases"]}) == 12
            assert first["candidate_quality"][name]["can_submit"]
        assert state["candidate_names"]  # No fixed business candidate count is imposed.
        assert sum(len(_read(path)["content"]) for path in (root / "chunks").glob("*.json")) > 0

    asyncio.run(check())


def test_late_fragment_setting_cannot_migrate_frozen_coarse_state_packets_or_source_ranges(tmp_path):
    async def check():
        owner = FullRecoveryOwner(tmp_path, fine=False)
        job, entries = owner.new(3)
        job = await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, "")
        root, before = await owner._staged_load(job)
        assert not before.get("source_fragment_protocol")
        frozen = {str(path.relative_to(root)): path.read_bytes() for folder in ("chunks", "compilers", "children")
                  for path in (root / folder).rglob("*") if path.is_file()}
        job = owner.repo.update_job(job["job_id"], staged_source_fragment_protocol=SOURCE_FRAGMENT_PROTOCOL)
        final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert final["status"] == "waiting_human", final.get("error")
        _, after = await owner._staged_load(final)
        assert not after.get("source_fragment_protocol") and not after.get("extract_max_chunks")
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())

    asyncio.run(check())


def test_reviewed_second_round_reuses_same_fragment_profile_and_preserves_all_twelve_case_ids(tmp_path):
    async def check():
        owner = FullRecoveryOwner(tmp_path)
        first = await reviewed(owner)
        before = len(owner.ov.extract_batches), len(owner.ov.artifacts), len(owner.ov.batches)
        first_root, first_state = await owner._staged_load(first)
        first_ids = {name: [case["case_id"] for case in _read(owner.repo.workspace(first)
                     / f"compiled_skill/{name}/benchmark.json")["cases"]] for name in first_state["candidate_names"]}
        second, entries = await next_inputs(owner, first)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["reused_extract_count"] > 0 and state["reused_candidate_count"] == len(first_ids)
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before[1:]
        new_batches = owner.ov.extract_batches[before[0]:]
        assert all("/supplements/" in chunk["source_uri"] for batch in new_batches for packet in batch
                   for chunk in packet["evidence"])
        for name, identities in first_ids.items():
            document = _read(owner.repo.workspace(second) / f"compiled_skill/{name}/benchmark.json")
            assert [case["case_id"] for case in document["cases"]] == identities and len(identities) == 12
            assert second["candidate_quality"][name]["can_submit"]
        assert _read(root / "reuse/manifest.json")["previous_root"].endswith("/rounds/round-1")
        assert first_root != root

    asyncio.run(check())


@pytest.mark.parametrize("field,replacement", [
    ("source_fragment_protocol", "unknown-fragment-protocol"), ("source_fragment_chars", 6000),
    ("extract_max_chunks", 4),
])
def test_fragment_dependency_change_cannot_use_previous_extract_or_candidate_cache(tmp_path, field, replacement):
    async def check():
        assert field in PROTOCOL_FIELDS
        owner = FullRecoveryOwner(tmp_path)
        first = await reviewed(owner)
        second, entries = await next_inputs(owner, first)
        final = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert final["status"] == "waiting_human", final.get("error")
        root, state = await owner._staged_load(final)
        before = copy.deepcopy(state)
        # A fixture-only profile change must not match a previously prepared cache locator.
        state[field] = replacement
        chunks, after = _raw_group(root, range(state["chunk_count"]), 0, max_chunks=3)
        context = {"after": after, "ordinals": [chunk["ordinal"] for chunk in chunks],
                   "input_ids": [chunk["id"] for chunk in chunks],
                   "source_refs": [_alias(chunk["id"]) for chunk in chunks]}
        name = state["candidate_names"][0]
        candidate = next(row for row in _read(root / "plan.json")["skills"] if row["name"] == name)
        scope = _read(root / "scopes" / f"{name}.json")
        with pytest.raises(ValueError, match="scope protocol changed"):
            try_reuse_extract(second, root, state, chunks, context)
        with pytest.raises(ValueError, match="scope protocol changed"):
            try_reuse_candidate(second, root, state, candidate, scope, owner.repo)
        assert state["reused_extract_count"] == before["reused_extract_count"]
        assert state["reused_candidate_count"] == before["reused_candidate_count"]
        prepare_round_reuse(second, root, state, entries)
        assert _read(root / "reuse/manifest.json")["candidate_count"] == 0
        assert not try_reuse_candidate(second, root, state, candidate, scope, owner.repo)

    asyncio.run(check())
