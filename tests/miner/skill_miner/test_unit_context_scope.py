"""Uncertain applicability shares assigned original units, without amplifying full files."""

from __future__ import annotations

import copy
import hashlib

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import source_scope
from team_miner.skill_miner.ov_staged_orchestration import (
    _alias,
    _bind_unit,
    _freeze_chunks,
    _method_groups,
    _read,
    _save,
    _sha,
)
from team_miner.skill_miner.source_scope import (
    SOURCE_SCOPE_PROTOCOL_V3,
    UNCERTAINTY_SCOPE_PROTOCOL,
    candidate_context_ordinals,
    prepare_source_routing,
    route_candidate_scope,
    route_final_candidates,
)
from tests.miner.skill_miner.support.source_scope_v3 import PREFIX, local_sources, scope


def enabled(state):
    return {**state, "uncertainty_scope_protocol": UNCERTAINTY_SCOPE_PROTOCOL}


def accept_groups(root, state, assigned):
    """Create exact generic accepted classifications, including every remaining original range."""
    groups = list(assigned)
    seen = {ordinal for ranges, *_ in groups for ordinal in ranges}
    groups.extend(([ordinal], [], [], False) for ordinal in range(state["chunk_count"]) if ordinal not in seen)
    for path in (root / "units/0").glob("*.json"):
        path.unlink()
    for index, (ranges, methods, contexts, unsure) in enumerate(groups):
        chunks = [_read(root / "chunk-metadata" / f"{ordinal:08d}.json") for ordinal in ranges]
        refs = {ordinal: _alias(chunk["id"]) for ordinal, chunk in zip(ranges, chunks)}
        parents = {refs[ordinal]: [ordinal] for ordinal in ranges}
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"] for chunk in chunks],
            "methods": [{"task": "Review the supplied request", "boundary": "Preserve original conditions",
                         "source_refs": [refs[ordinal]]} for ordinal in methods],
            "facts": [{"text": "Original context", "source_refs": [refs[ordinal]]}
                      for ordinal in ranges if ordinal not in methods],
            "deferred": [], "context_refs": [refs[ordinal] for ordinal in contexts],
            "context_uncertain": unsure,
        }
        planning.validate_index(value, input_ids=value["input_ids"], source_refs=list(parents), routing=True)
        _save(root / "units/0" / f"{index:08d}.json", _bind_unit(value, parents, f"extract-{index}"))
    state["unit_count"] = len(groups)


def test_assigned_uncertain_context_is_global_but_does_not_reclassify_unseen_file_ranges(tmp_path):
    root, state, files, methods, contexts = local_sources(tmp_path, uncertain="owned")
    routing = prepare_source_routing(root, enabled(state))
    shared = sorted({files[0][0], methods[0], contexts[0], files[0][2], files[0][-3], files[0][-1]})
    assert routing["shared_ordinals"] == routing["uncertain_context_ordinals"] == shared
    assert routing["uncertainty_scope_protocol"] == UNCERTAINTY_SCOPE_PROTOCOL
    assert routing["uncertain_files"] == [f"{PREFIX}/source-0.md"]
    first = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    second = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    assert set(shared) <= set(first["ordinals"]) & set(second["ordinals"])
    assert first["ordinals"] == sorted(set(shared) | {files[0][2]})
    assert set(second["ordinals"]) & set(files[0]) == set(shared)
    assert first["full_ordinals"] == files[0]
    assert second["full_ordinals"] == sorted(files[0] + files[1])
    assert second["uncertain_source_files"] == [f"{PREFIX}/source-0.md"]
    assert second["uncertainty_scope_sha256"] == routing["uncertainty_scope_sha256"]
    assert candidate_context_ordinals(root, [methods[1]], routing) == sorted(set(shared) | {contexts[1]})
    groups = _method_groups(root, second)
    assert {ordinal for group in groups for ordinal in group} == set(second["ordinals"])
    assert all(methods[1] in group for group in groups)
    for ordinal in shared:
        chunk = _read(root / "chunks" / f"{ordinal:08d}.json")
        assert _sha(chunk["content"]) == chunk["sha256"]


def test_cross_file_three_range_uncertainty_shares_only_exact_assignments_and_each_file_heading(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path)
    ranges = [files[0][2], files[0][-2], files[1][3]]
    accept_groups(root, state, [(ranges, [ranges[0]], [ranges[1]], True),
                                ([methods[1]], [methods[1]], [], False)])
    routing = prepare_source_routing(root, enabled(state))
    expected = sorted(set(ranges) | {
        files[0][0], files[1][0], files[0][1], files[0][3], files[0][-3], files[0][-1],
        files[1][2], files[1][4],
    })
    assert routing["uncertain_context_ordinals"] == expected
    routed = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    assert set(routed["shared_context_ordinals"]) == set(expected)
    assert routed["full_ordinals"] == sorted(files[0] + files[1])
    assert len(routed["ordinals"]) < len(routed["full_ordinals"])
    assert files[0][4] not in routing["shared_ordinals"], "Do not fill gaps between separate runs"


@pytest.mark.parametrize("location", ["file_head", "file_tail", "contiguous_run"])
def test_uncertain_run_guards_clip_at_file_boundaries_without_recursive_expansion(tmp_path, location):
    root, state, files, _, _ = local_sources(tmp_path)
    if location == "file_head":
        assigned, expected = [files[1][0]], set(files[1][:2])
    elif location == "file_tail":
        assigned, expected = [files[0][-1]], {files[0][0], files[0][-2], files[0][-1]}
    else:
        assigned, expected = files[0][2:5], {files[0][0], *files[0][1:6]}
    accept_groups(root, state, [(assigned, [], assigned, True)])
    routing = prepare_source_routing(root, enabled(state))
    assert set(routing["uncertain_context_ordinals"]) == expected
    assert set(routing["shared_ordinals"]) == expected


def test_explicit_unowned_global_constraint_and_related_method_context_are_not_lost(tmp_path):
    root, state, files, methods, contexts = local_sources(tmp_path, uncertain="owned", unowned=True)
    routing = prepare_source_routing(root, enabled(state))
    assert routing["unowned_context_ordinals"] == [files[1][-1]]
    routed = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    expected = {files[0][0], methods[0], contexts[0], contexts[1], files[1][-1],
                files[0][2], files[0][-3], files[0][-1]}
    assert set(routed["shared_context_ordinals"]) == expected
    assert files[0][3] not in routed["ordinals"]


def test_facts_only_uncertain_unit_keeps_its_original_constraint_globally(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="unowned")
    routing = prepare_source_routing(root, enabled(state))
    expected = {files[1][0], files[1][-2], files[1][-1]}
    assert routing["shared_ordinals"] == sorted(expected)
    assert routing["unowned_uncertain_files"] == [f"{PREFIX}/source-1.md"]
    routed = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    assert set(routed["ordinals"]) & set(files[1]) == expected
    assert routed["full_ordinals"] == sorted(files[0] + files[1])


@pytest.mark.parametrize("missing", ["unit", "classification", "old_schema", "all_units"])
def test_incomplete_classification_keeps_full_corpus_fallback_with_new_profile(tmp_path, missing):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="owned")
    state = enabled(state)
    if missing == "unit":
        state["unit_count"] -= 1
    elif missing == "all_units":
        state["unit_count"] = 0
    else:
        path = root / "units/0/00000000.json"
        unit = _read(path)
        if missing == "classification":
            unit["value"]["facts"] = []
        else:
            unit["value"]["schema_version"] = planning.INDEX_SCHEMA
        _save(path, unit)
    routing = prepare_source_routing(root, state)
    routed = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    assert not routing["coverage_complete"]
    assert routing["shared_ordinals"] == list(range(state["chunk_count"]))
    assert routed["ordinals"] == routed["full_ordinals"] == routing["shared_ordinals"]


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_late_profile_addition_or_removal_never_migrates_existing_frozen_routing(tmp_path, initially_enabled):
    root, state, _, _, _ = local_sources(tmp_path, uncertain="owned")
    old = enabled(state) if initially_enabled else state
    new = state if initially_enabled else enabled(state)
    routing = prepare_source_routing(root, old)
    path = root / "source-routing/context.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        prepare_source_routing(root, new)
    assert path.read_bytes() == before
    assert prepare_source_routing(root, old) == routing and path.read_bytes() == before


def test_no_marker_keeps_original_whole_file_behavior_and_manifest_scope_bytes(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="owned")
    routing = prepare_source_routing(root, state)
    routed = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    assert set(files[0]) <= set(routed["ordinals"])
    assert routing["shared_ordinals"] == files[0]
    assert "uncertainty_scope_protocol" not in routing and "uncertainty_scope_protocol" not in routed
    path = root / "source-routing/context.json"
    before = path.read_bytes()
    assert prepare_source_routing(root, state) == routing and path.read_bytes() == before
    assert route_candidate_scope(root, scope(files[1], methods[1]), routing) == routed


@pytest.mark.parametrize("tamper", ["sha", "range", "profile", "unit"])
def test_frozen_profile_and_exact_uncertain_associations_cannot_be_tampered(tmp_path, tamper):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="owned")
    routing = prepare_source_routing(root, enabled(state))
    forged = copy.deepcopy(routing)
    if tamper == "sha":
        forged["uncertainty_scope_sha256"] = "0" * 64
    elif tamper == "range":
        forged["uncertain_context_ordinals"].append(files[0][3])
        forged["uncertainty_scope_sha256"] = source_scope._uncertainty_scope_sha(
            forged, set(forged["uncertain_context_ordinals"])
        )
    elif tamper == "profile":
        forged["uncertainty_scope_protocol"] = "unknown"
    else:
        path = root / "units/0/00000000.json"
        unit = _read(path)
        unit["value"]["context_uncertain"] = unit["context_uncertain"] = False
        _save(path, unit)
    _save(root / "source-routing/context.json", forged)
    with pytest.raises(ValueError, match="scope|association|protocol"):
        route_candidate_scope(root, scope(files[1], methods[1]), forged)


def test_existing_scope_with_different_explicit_profile_is_rejected(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="owned")
    routing = prepare_source_routing(root, enabled(state))
    original = scope(files[1], methods[1])
    original["uncertainty_scope_protocol"] = "changed"
    with pytest.raises(ValueError, match="protocol changed"):
        route_candidate_scope(root, original, routing)


def test_final_scope_and_summary_preserve_profile_source_lineage_and_idempotence(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="owned")
    state = enabled(state)
    uri = f"{PREFIX}/source-1.md"
    _save(root / "plan.json", {"skills": [{"name": "generic-workflow", "source_refs": [uri]}]})
    _save(root / "scopes/generic-workflow.json", scope(files[1], methods[1]))
    _save(root / "catalog/manifest.json", {"method": 1})
    _save(root / "catalog/method/00000000.json", {
        "item": {"item_id": "method-0", "task": "Generic workflow", "boundary": "Original applicability"},
        "ordinals": [methods[1]],
    })
    _save(root / "method-decisions/method-0.json", {"target": "candidate", "name": "generic-workflow"})
    audit = route_final_candidates(root, state, PREFIX)
    path = root / "scopes/generic-workflow.json"
    before = path.read_bytes()
    routed = _read(path)
    assert audit["uncertainty_scope_protocol"] == routed["uncertainty_scope_protocol"] == UNCERTAINTY_SCOPE_PROTOCOL
    assert routed["method_lineage_verified"] and len(routed["method_lineage_sha256"]) == 64
    assert route_final_candidates(root, state, PREFIX) == audit and path.read_bytes() == before
    assert len(_read(root / "plan.json")["skills"][0]["source_refs"]) == 2


def test_twenty_megabyte_document_and_other_sources_are_archived_without_full_file_writer_expansion(
    tmp_path, monkeypatch
):
    root = tmp_path / "staged"
    entries, original_hashes = [], []
    for index, size in enumerate((20 * 1024 * 1024, 48000, 36000)):
        path = tmp_path / f"source-{index}.md"
        block = bytes([65 + index]) * 65536
        digest = hashlib.sha256()
        with path.open("wb") as handle:
            remaining = size
            while remaining:
                value = block[:remaining]
                handle.write(value)
                digest.update(value)
                remaining -= len(value)
        original_hashes.append(digest.hexdigest())
        entries.append({"kind": "source", "local_path": str(path), "relative_path": path.name,
                        "sha256": digest.hexdigest()})
    count = _freeze_chunks(root, entries, PREFIX)
    files = [_read(root / "source-files" / (_sha(f"{PREFIX}/source-{index}.md") + ".json"))["ordinals"]
             for index in range(3)]
    state = enabled({"chunk_count": count, "source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3})
    assigned = [files[0][100], files[0][101], files[1][2]]
    method = files[2][1]
    accept_groups(root, state, [(assigned, [assigned[0]], [assigned[1]], True),
                                ([method], [method], [], False)])
    read, save, sha = source_scope._io()

    def metadata_only(path):
        assert path.parent.name != "chunks", "Source routing must not read whole raw source bodies"
        return read(path)

    monkeypatch.setattr(source_scope, "_io", lambda: (metadata_only, save, sha))
    routing = prepare_source_routing(root, state)
    routed = route_candidate_scope(root, scope(files[2], method), routing)
    expected = set(assigned) | {files[0][0], files[1][0], files[0][99], files[0][102],
                                files[1][1], files[1][3]}
    assert set(routing["shared_ordinals"]) == expected
    assert len(routed["ordinals"]) <= 12 and count > 1700
    assert routed["full_ordinals"] == list(range(count))
    assert {ordinal for unit in routing["unit_contexts"] for ordinal in unit["ordinals"]} == set(range(count))
    for entry, expected_hash in zip(entries, original_hashes):
        with open(entry["local_path"], "rb") as handle:
            digest = hashlib.sha256()
            while block := handle.read(65536):
                digest.update(block)
        assert digest.hexdigest() == expected_hash
