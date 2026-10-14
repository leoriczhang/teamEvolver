"""Local accepted-unit context preserves original rules without globalizing unrelated workflows."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner import source_scope
from team_miner.ov_staged_orchestration import _alias, _bind_unit, _freeze_chunks, _read, _save, _sha
from team_miner.round_reuse import _profile, _stat
from team_miner.source_scope import (
    SOURCE_SCOPE_PROTOCOL,
    SOURCE_SCOPE_PROTOCOL_V3,
    candidate_context_ordinals,
    prepare_source_routing,
    route_candidate_scope,
    route_final_candidates,
)

PREFIX = "viking://resources/frozen"


def local_sources(tmp_path, *, unowned=False, uncertain=False, cross=False):
    root = tmp_path / "staged"
    entries = []
    for index, body in enumerate(("A" * 95000, "B" * 95000)):
        path = tmp_path / f"source-{index}.md"
        path.write_text(body)
        entries.append({"kind": "source", "local_path": str(path), "relative_path": path.name, "sha256": _sha(body)})
    count = _freeze_chunks(root, entries, PREFIX)
    files = [_read(root / "source-files" / (_sha(f"{PREFIX}/source-{index}.md") + ".json"))["ordinals"]
             for index in range(2)]
    first, second = files[0][1], files[1][1]
    context_a, context_b = files[0][-2], files[1][-2]
    if cross:
        context_a, context_b = context_b, context_a
    groups = [([first, context_a], [first], [context_a], uncertain == "owned"),
              ([second, context_b], [second], [context_b], False)]
    assigned = {ordinal for ranges, *_ in groups for ordinal in ranges}
    for ordinal in range(count):
        if ordinal not in assigned:
            marked = ordinal == files[1][-1] and (unowned or uncertain == "unowned")
            groups.append(([ordinal], [], [ordinal] if marked else [], bool(marked and uncertain == "unowned")))
    for index, (ordinals, methods, contexts, unsure) in enumerate(groups):
        chunks = [_read(root / "chunk-metadata" / f"{ordinal:08d}.json") for ordinal in ordinals]
        parents = {_alias(chunk["id"]): [ordinal] for chunk, ordinal in zip(chunks, ordinals)}
        refs = {ordinal: _alias(chunk["id"]) for chunk, ordinal in zip(chunks, ordinals)}
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"] for chunk in chunks],
            "methods": [{"task": "Review documented inputs", "boundary": "Use the exact original conditions",
                         "source_refs": [refs[ordinal]]} for ordinal in methods],
            "facts": [{"text": "Original reference or constraint", "source_refs": [refs[ordinal]]}
                      for ordinal in ordinals if ordinal not in methods],
            "deferred": [], "context_refs": [refs[ordinal] for ordinal in contexts], "context_uncertain": unsure,
        }
        planning.validate_index(value, input_ids=value["input_ids"], source_refs=list(parents), routing=True)
        _save(root / "units/0" / f"{index:08d}.json", _bind_unit(value, parents, f"extract-{index}"))
    state = {"chunk_count": count, "unit_count": len(groups), "source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3}
    return root, state, files, (first, second), (context_a, context_b)


def scope(files, method):
    return {"ordinals": list(files), "method_ordinals": [method]}


def test_method_bearing_context_is_local_and_does_not_flow_to_other_candidates_or_files(tmp_path):
    root, state, files, methods, contexts = local_sources(tmp_path)
    routing = prepare_source_routing(root, state)
    first = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    second = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    assert first["ordinals"] == sorted({files[0][0], methods[0], files[0][2], contexts[0]})
    assert second["ordinals"] == sorted({files[1][0], methods[1], files[1][2], contexts[1]})
    assert not set(first["ordinals"]) & set(files[1]) and not set(second["ordinals"]) & set(files[0])
    assert first["shared_context_ordinals"] == [contexts[0]]
    assert second["shared_context_ordinals"] == [contexts[1]]
    assert first["full_ordinals"] == files[0] and second["full_ordinals"] == files[1]
    assert routing["shared_ordinals"] == routing["unowned_context_ordinals"] == []


def test_explicit_same_unit_cross_range_or_cross_file_constraint_is_still_original_evidence(tmp_path):
    root, state, files, methods, contexts = local_sources(tmp_path, cross=True)
    routing = prepare_source_routing(root, state)
    routed = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    assert contexts[0] in routed["ordinals"] and contexts[1] not in routed["ordinals"]
    assert routed["full_ordinals"] == sorted(files[0] + files[1])
    assert candidate_context_ordinals(root, [methods[0]], routing) == [contexts[0]]
    assert _read(root / "chunks" / f"{contexts[0]:08d}.json")["content"] == "B" * len(
        _read(root / "chunks" / f"{contexts[0]:08d}.json")["content"]
    )
    overlapping = route_candidate_scope(root, {"ordinals": [methods[0], contexts[0]],
                                               "method_ordinals": [methods[0], contexts[0]]}, routing)
    assert contexts[0] in overlapping["context_ordinals"]
    assert contexts[0] in overlapping["method_ordinals"]


def test_uncertain_method_context_is_preserved_globally_when_cross_file_applicability_is_unknown(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="owned")
    routing = prepare_source_routing(root, state)
    first = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    second = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    assert first["ordinals"] == files[0]
    assert set(files[0]) <= set(second["ordinals"])
    assert set(files[0]) <= set(second["shared_context_ordinals"])
    assert first["uncertain_source_files"] == [f"{PREFIX}/source-0.md"]
    assert second["uncertain_source_files"] == [f"{PREFIX}/source-0.md"]
    assert routing["shared_ordinals"] == files[0]
    assert routing["unowned_uncertain_files"] == []


def test_unowned_fact_context_remains_available_to_every_candidate_as_raw_original(tmp_path):
    root, state, files, methods, contexts = local_sources(tmp_path, unowned=True)
    routing = prepare_source_routing(root, state)
    assert routing["unowned_context_ordinals"] == routing["shared_ordinals"] == [files[1][-1]]
    first = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    assert files[1][-1] in first["ordinals"] and contexts[1] not in first["ordinals"]
    assert candidate_context_ordinals(root, [methods[0]], routing) == sorted([contexts[0], files[1][-1]])


def test_unowned_uncertain_document_is_preserved_globally_without_globalizing_owned_method_context(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, uncertain="unowned")
    routing = prepare_source_routing(root, state)
    assert routing["unowned_uncertain_files"] == [f"{PREFIX}/source-1.md"]
    assert routing["shared_ordinals"] == files[1]
    first = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    assert set(files[1]) <= set(first["ordinals"])
    second = route_candidate_scope(root, scope(files[1], methods[1]), routing)
    assert second["ordinals"] == files[1] and not set(second["ordinals"]) & set(files[0])


@pytest.mark.parametrize("missing", ["unit", "classification", "old_schema", "all_units"])
def test_missing_global_classification_falls_back_to_all_original_ranges(tmp_path, missing):
    root, state, files, methods, _ = local_sources(tmp_path)
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
    assert routed["ordinals"] == routed["full_ordinals"] == list(range(state["chunk_count"]))


def test_v2_frozen_manifest_and_scope_bytes_keep_the_existing_global_context_semantics(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path)
    state.pop("source_scope_protocol")
    units = [_read(root / "units/0" / f"{index:08d}.json") for index in range(state["unit_count"])]
    digest = hashlib.sha256()
    for unit in units:
        digest.update(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode())
    shared = sorted({ordinal for unit in units for ordinal in unit["context_ordinals"]})
    expected = {"protocol": SOURCE_SCOPE_PROTOCOL, "chunk_count": state["chunk_count"],
                "accepted_units_sha256": digest.hexdigest(), "coverage_complete": True,
                "shared_ordinals": shared, "uncertain_files": [], "routing_is_evidence": False}
    path = root / "source-routing/context.json"
    _save(path, expected)
    before = path.read_bytes()
    routing = prepare_source_routing(root, state)
    assert routing == expected and path.read_bytes() == before
    result = route_candidate_scope(root, scope(files[0], methods[0]), routing)
    assert result["shared_context_ordinals"] == shared and shared[-1] in result["ordinals"]
    assert result["scope_protocol"] == SOURCE_SCOPE_PROTOCOL
    assert candidate_context_ordinals(root, [methods[0]], routing) == shared


def test_frozen_v2_routing_is_not_silently_migrated_when_a_new_marker_is_added(tmp_path):
    root, state, *_ = local_sources(tmp_path)
    prepare_source_routing(root, {**state, "source_scope_protocol": SOURCE_SCOPE_PROTOCOL})
    before = (root / "source-routing/context.json").read_bytes()
    with pytest.raises(ValueError, match="changed"):
        prepare_source_routing(root, state)
    assert (root / "source-routing/context.json").read_bytes() == before


@pytest.mark.parametrize("tamper", ["unit_context", "binding", "duplicate_range", "file_membership"])
def test_accepted_unit_or_original_file_mapping_tamper_cannot_route_foreign_context(tmp_path, tamper):
    root, state, files, methods, _ = local_sources(tmp_path)
    routing = prepare_source_routing(root, state)
    if tamper == "file_membership":
        path = root / "source-files" / (_sha(f"{PREFIX}/source-0.md") + ".json")
        value = _read(path)
        value["ordinals"].append(files[1][0])
    else:
        path = root / "units/0/00000000.json"
        value = _read(path)
        if tamper == "unit_context":
            value["context_ordinals"] = [files[1][0]]
        elif tamper == "binding":
            value["bindings"][next(iter(value["bindings"]))] = [files[1][0]]
        else:
            value["ordinals"].append(value["ordinals"][0])
    _save(path, value)
    with pytest.raises(ValueError):
        route_candidate_scope(root, scope(files[0], methods[0]), routing)


@pytest.mark.parametrize("tamper", ["association", "summary"])
def test_modified_persisted_context_manifest_is_checked_against_accepted_unit_authority(tmp_path, tamper):
    root, state, files, methods, _ = local_sources(tmp_path)
    routing = prepare_source_routing(root, state)
    forged = copy.deepcopy(routing)
    if tamper == "association":
        forged["unit_contexts"][0]["context_ordinals"] = [files[1][0]]
        forged["associations_sha256"] = _sha(json.dumps(forged["unit_contexts"], ensure_ascii=False, sort_keys=True))
    else:
        forged["coverage_complete"] = False
        forged["shared_ordinals"] = list(range(state["chunk_count"]))
    _save(root / "source-routing/context.json", forged)
    with pytest.raises(ValueError, match="association|summary"):
        route_candidate_scope(root, scope(files[0], methods[0]), forged)


def test_previous_verified_v3_context_is_preserved_after_current_unit_reclassification(tmp_path):
    root, state, files, methods, contexts = local_sources(tmp_path)
    original = {"scope_protocol": SOURCE_SCOPE_PROTOCOL_V3, "ordinals": [methods[0], contexts[1]],
                "method_ordinals": [methods[0]], "context_ordinals": [contexts[1]],
                "shared_context_ordinals": [contexts[1]], "full_ordinals": sorted(files[0] + files[1])}
    routed = route_candidate_scope(root, original, prepare_source_routing(root, state))
    assert {contexts[0], contexts[1]} <= set(routed["shared_context_ordinals"])
    assert routed["full_ordinals"] == original["full_ordinals"]


def test_consistently_rehashed_unowned_context_tamper_cannot_suppress_a_global_original_constraint(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path, unowned=True)
    routing = prepare_source_routing(root, state)
    forged = copy.deepcopy(routing)
    unit = next(unit for unit in forged["unit_contexts"] if not unit["method_ordinals"] and unit["context_ordinals"])
    unit["context_ordinals"] = []
    forged["unowned_context_ordinals"] = forged["shared_ordinals"] = []
    forged["associations_sha256"] = _sha(json.dumps(forged["unit_contexts"], ensure_ascii=False, sort_keys=True))
    _save(root / "source-routing/context.json", forged)
    with pytest.raises(ValueError, match="accepted original unit"):
        route_candidate_scope(root, scope(files[0], methods[0]), forged)


def test_routing_reads_only_metadata_and_keeps_raw_originals_unchanged(tmp_path, monkeypatch):
    root, state, files, methods, _ = local_sources(tmp_path)
    before = {path.name: path.read_bytes() for path in (root / "chunks").glob("*.json")}
    read, save, sha = source_scope._io()

    def metadata_only(path):
        assert path.parent.name != "chunks", "Routing must not load original bodies into memory"
        return read(path)

    monkeypatch.setattr(source_scope, "_io", lambda: (metadata_only, save, sha))
    route_candidate_scope(root, scope(files[0], methods[0]), prepare_source_routing(root, state))
    assert before == {path.name: path.read_bytes() for path in (root / "chunks").glob("*.json")}


def test_final_local_routing_preserves_lineage_source_refs_and_idempotent_summary(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path)
    candidates = [{"name": f"workflow-{index}", "source_refs": [f"{PREFIX}/source-{index}.md"]}
                  for index in range(2)]
    _save(root / "plan.json", {"skills": candidates})
    _save(root / "catalog/manifest.json", {"method": 2})
    for index, candidate in enumerate(candidates):
        _save(root / "scopes" / (candidate["name"] + ".json"), scope(files[index], methods[index]))
        item_id = f"method-{index}"
        _save(root / "catalog/method" / f"{index:08d}.json",
              {"item": {"item_id": item_id, "task": f"Generic workflow {index}", "boundary": "Original boundary"},
               "ordinals": [methods[index]]})
        _save(root / "method-decisions" / (item_id + ".json"), {"target": "candidate", "name": candidate["name"]})
    audit = route_final_candidates(root, state, PREFIX)
    paths = list((root / "scopes").glob("*.json"))
    before = {path.name: path.read_bytes() for path in paths}
    assert audit["protocol"] == SOURCE_SCOPE_PROTOCOL_V3 and not audit["global_context_union"]
    assert audit["routed_range_assignments"] < audit["full_range_assignments"]
    assert all(_read(path)["method_lineage_verified"] and len(_read(path)["method_lineage_sha256"]) == 64
               for path in paths)
    assert route_final_candidates(root, state, PREFIX) == audit
    assert before == {path.name: path.read_bytes() for path in paths}
    assert _read(root / "plan.json")["skills"] == candidates


def test_v3_protocol_and_actual_context_dependencies_remain_distinct_for_round_reuse(tmp_path):
    root, state, files, methods, _ = local_sources(tmp_path)
    routed = route_candidate_scope(root, scope(files[0], methods[0]), prepare_source_routing(root, state))
    sources = {}
    for index in range(2):
        path = tmp_path / f"source-{index}.md"
        sources[path.name] = {"kind": "source", "sha256": _sha(path.read_text()),
                              "local_path": str(path), "stat": _stat(path)}
    current = _profile(root, routed, PREFIX, sources)
    old = _profile(root, {**routed, "scope_protocol": SOURCE_SCOPE_PROTOCOL}, PREFIX, sources)
    assert current["scope_protocol"] != old["scope_protocol"]
    assert current["ordinals"] == old["ordinals"] and current["context_ordinals"] == old["context_ordinals"]
    assert current != old


def test_source_associations_do_not_follow_symbolic_links_to_other_rounds(tmp_path):
    root, state, *_ = local_sources(tmp_path)
    path = root / "units/0/00000000.json"
    outside = tmp_path / "outside-unit.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="own frozen round|symbolic"):
        prepare_source_routing(root, state)


def test_fresh_routing_manifest_cannot_be_written_through_a_foreign_directory_symlink(tmp_path):
    root, state, *_ = local_sources(tmp_path)
    outside = tmp_path / "other-round"
    outside.mkdir()
    (root / "source-routing").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="own frozen round|symbolic"):
        prepare_source_routing(root, state)
    assert not (outside / "context.json").exists()
