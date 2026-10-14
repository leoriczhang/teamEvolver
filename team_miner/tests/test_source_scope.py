"""Original constraints, boundaries and full source coverage survive precise writer routing."""

import copy

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.ov_staged_orchestration import (
    _alias,
    _bind_unit,
    _canonical_extract_references,
    _freeze_chunks,
    _method_groups,
    _read,
    _save,
    _sha,
)
from team_miner.source_scope import prepare_source_routing, route_candidate_scope, route_final_candidates


def sources(tmp_path, text=None):
    root = tmp_path / "staged"
    entries = []
    originals = text or ["ordinary unrelated material\n" * 12000, "Shared exception: obtain consent.\n"]
    for index, body in enumerate(originals):
        path = tmp_path / f"source-{index}.md"
        path.write_text(body)
        entries.append({"kind": "source", "local_path": str(path), "relative_path": path.name, "sha256": _sha(body)})
    count = _freeze_chunks(root, entries, "viking://resources/frozen")
    state = {"chunk_count": count, "unit_count": count}
    for ordinal in range(count):
        chunk = _read(root / "chunks" / f"{ordinal:08d}.json")
        ref = _alias(chunk["id"])
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"]],
            "methods": [], "facts": [{"text": "Original material", "source_refs": [ref]}], "deferred": [],
            "context_refs": [ref] if ordinal == count - 1 else [], "context_uncertain": False,
        }
        planning.validate_index(value, input_ids=[chunk["id"]], source_refs=[ref], routing=True)
        unit = _bind_unit(value, {ref: [ordinal]}, f"extract-{ordinal}")
        _save(root / "units/0" / f"{ordinal:08d}.json", unit)
    return root, state


def test_distant_cross_file_constraint_and_split_boundaries_are_actual_evidence(tmp_path):
    root, state = sources(tmp_path)
    count = state["chunk_count"]
    scope = {"ordinals": list(range(count - 1)), "method_ordinals": [4]}
    routed = route_candidate_scope(root, scope, prepare_source_routing(root, state))
    assert routed["ordinals"] == [0, 3, 4, 5, count - 1]
    assert routed["full_ordinals"] == list(range(count))
    assert len(routed["ordinals"]) < len(scope["ordinals"]) / 2
    groups = _method_groups(root, routed)
    assert {index for group in groups for index in group} == set(routed["ordinals"])
    constraint = _read(root / "chunks" / f"{count - 1:08d}.json")
    assert "obtain consent" in constraint["content"]
    assert planning._hash(constraint["content"]) == constraint["sha256"]
    # The complete representative Case rotation retains shared constraints as original Evidence.
    assert count - 1 in [index for group in groups for index in group]
    assert groups[0][:2] == [4, count - 1]
    assert all(4 in group for group in groups)


def test_uncertain_routing_reads_the_affected_original_file_in_full(tmp_path):
    root, state = sources(tmp_path)
    path = root / "units/0/00000007.json"
    unit = _read(path)
    unit["context_uncertain"] = unit["value"]["context_uncertain"] = True
    _save(path, unit)
    routed = route_candidate_scope(root, {"ordinals": [4], "method_ordinals": [4]},
                                   prepare_source_routing(root, state))
    assert routed["ordinals"] == list(range(state["chunk_count"]))


def test_previous_verified_distant_constraint_survives_current_index_reclassification(tmp_path):
    root, state = sources(tmp_path)
    routing = prepare_source_routing(root, state)
    old = {"scope_protocol": "te-source-scope.v2", "ordinals": [4, 20], "method_ordinals": [4],
           "context_ordinals": [20], "shared_context_ordinals": [20]}
    routed = route_candidate_scope(root, old, {**routing, "shared_ordinals": []})
    assert 20 in routed["ordinals"] and routed["shared_context_ordinals"] == [20]
    assert _method_groups(root, routed)[0][:2] == [4, 20]


def test_compact_cross_range_workflow_and_constraint_share_one_case_packet(tmp_path):
    root, _ = sources(tmp_path, ["Step one: collect the required inputs.",
                                 "Step two: confirm the collected inputs before proceeding.",
                                 "Shared constraint: obtain consent before collecting inputs."])
    scope = {"scope_protocol": "te-source-scope.v2", "method_ordinals": [0, 1],
             "ordinals": [0, 1, 2], "shared_context_ordinals": [2]}
    assert _method_groups(root, scope) == [[0, 1, 2]]


def test_large_cross_range_method_stays_intact_and_context_pairing_is_bounded(tmp_path):
    root, state = sources(tmp_path)
    scope = {"scope_protocol": "te-source-scope.v2", "method_ordinals": [3, 4],
             "ordinals": list(range(state["chunk_count"])), "shared_context_ordinals": [5]}
    groups = _method_groups(root, scope)
    assert groups[0] == [3, 4]
    assert {ordinal for group in groups for ordinal in group} == set(scope["ordinals"])
    assert len(groups) <= len(scope["ordinals"])
    for group in groups:
        chunks = [_read(root / "chunks" / f"{ordinal:08d}.json") for ordinal in group]
        assert len(group) <= 8
        assert len(planning._json({"evidence": chunks})) <= 26000


def test_many_method_ranges_never_load_the_entire_original_workflow_at_once(tmp_path, monkeypatch):
    from team_miner import ov_staged_orchestration as staged

    root, state = sources(tmp_path)
    selected = list(range(state["chunk_count"]))
    reads, original = [], staged._chunks

    def bounded_read(root, ordinals):
        reads.append(len(ordinals))
        assert len(ordinals) <= 8
        return original(root, ordinals)

    monkeypatch.setattr(staged, "_chunks", bounded_read)
    groups = _method_groups(root, {"scope_protocol": "te-source-scope.v2", "method_ordinals": selected,
                                  "ordinals": selected, "shared_context_ordinals": []})
    assert [ordinal for group in groups for ordinal in group] == selected
    assert len(reads) > 1


@pytest.mark.parametrize("old_index", [False, True])
def test_missing_or_old_source_classification_cannot_claim_no_constraints(tmp_path, old_index):
    root, state = sources(tmp_path)
    if old_index:
        path = root / "units/0/00000000.json"
        unit = _read(path)
        unit["value"]["schema_version"] = planning.INDEX_SCHEMA
        _save(path, unit)
    else:
        state["unit_count"] -= 1
    routing = prepare_source_routing(root, state)
    assert not routing["coverage_complete"]
    assert routing["shared_ordinals"] == list(range(state["chunk_count"]))


def test_routing_preserves_source_map_in_plan_and_is_idempotent(tmp_path):
    root, state = sources(tmp_path)
    first_uri = _read(root / "chunk-metadata/00000004.json")["source_uri"]
    _save(root / "plan.json", {"skills": [{"name": "generic-workflow", "source_refs": [first_uri]}]})
    _save(root / "scopes/generic-workflow.json", {"ordinals": list(range(state["chunk_count"] - 1)),
                                                "method_ordinals": [4]})
    audit = route_final_candidates(root, state)
    scope = _read(root / "scopes/generic-workflow.json")
    plan = _read(root / "plan.json")
    assert len(plan["skills"][0]["source_refs"]) == 2
    assert audit["routed_range_assignments"] < audit["full_range_assignments"]
    assert route_final_candidates(root, state) == audit
    assert _read(root / "scopes/generic-workflow.json") == scope


def test_routing_rejects_duplicate_source_scan_or_foreign_context(tmp_path):
    root, state = sources(tmp_path)
    unit = _read(root / "units/0/00000000.json")
    _save(root / "units/0/00000001.json", unit)
    with pytest.raises(ValueError, match="unique, exact"):
        prepare_source_routing(root, state)


@pytest.mark.parametrize("bad_refs", [["foreign"], [True], ["a", "a"]])
def test_context_references_are_strict_and_not_factual_summary_substitutes(bad_refs):
    value = {"schema_version": planning.INDEX_SCHEMA_V2, "input_ids": ["src"], "methods": [],
             "facts": [{"text": "original", "source_refs": ["a"]}], "deferred": [],
             "context_refs": bad_refs, "context_uncertain": False}
    with pytest.raises(ValueError, match="exact assigned"):
        planning.validate_index(value, input_ids=["src"], source_refs=["a"], routing=True)


def test_context_raw_ids_and_uri_normalize_only_to_frozen_assignment():
    chunks = planning.source_chunks("rule", "viking://resources/rule")
    chunk = chunks[0]
    value = {"schema_version": planning.INDEX_SCHEMA_V2, "methods": [], "deferred": [],
             "facts": [{"text": "original", "source_refs": [chunk["id"]]}],
             "context_refs": [chunk["source_uri"], chunk["id"]], "context_uncertain": False}
    canonical = _canonical_extract_references(value, input_ids=[chunk["id"]], source_refs=["a"],
                                              reference_map={"a": chunk["id"]}, evidence=chunks)
    planning.validate_index(canonical, input_ids=[chunk["id"]], source_refs=["a"], routing=True)
    assert canonical["context_refs"] == ["a"]
    unit = _bind_unit(copy.deepcopy(canonical), {"a": [0]}, "extract")
    assert unit["context_ordinals"] == [0]
    assert unit["value"]["context_refs"][0] in unit["bindings"]


def test_same_range_new_workflow_branch_changes_reuse_lineage(tmp_path):
    root, state = sources(tmp_path)
    uri = _read(root / "chunk-metadata/00000004.json")["source_uri"]
    _save(root / "plan.json", {"skills": [{"name": "workflow", "source_refs": [uri]}]})
    _save(root / "scopes/workflow.json", {"ordinals": list(range(state["chunk_count"] - 1)),
                                       "method_ordinals": [4]})
    _save(root / "catalog/manifest.json", {"method": 2})
    for index, task in enumerate(["Execute an ordinary request", "Execute the supported exception branch"]):
        identity = f"leaf-{index}"
        _save(root / "catalog/method" / f"{index:08d}.json", {
            "item": {"item_id": identity, "task": task, "boundary": "Respect the supplied original rules"},
            "ordinals": [4],
        })
        _save(root / "method-decisions" / (identity + ".json"), {
            "target": "candidate" if index == 0 else "knowledge", "name": "workflow",
        })
    route_final_candidates(root, state, "viking://resources/frozen")
    first = _read(root / "scopes/workflow.json")
    _save(root / "method-decisions/leaf-1.json", {"target": "candidate", "name": "workflow"})
    route_final_candidates(root, state, "viking://resources/frozen")
    second = _read(root / "scopes/workflow.json")
    assert first["method_lineage_verified"] and second["method_lineage_verified"]
    assert first["ordinals"] == second["ordinals"]
    assert first["method_lineage_sha256"] != second["method_lineage_sha256"]


def test_deferred_review_reuses_the_complete_source_routing_with_original_constraints(tmp_path):
    from team_miner.deferred_evidence_review import _method_descriptor

    root, state = sources(tmp_path)
    uri = _read(root / "chunk-metadata/00000004.json")["source_uri"]
    record = {"item": {"item_id": "method", "task": "Review request", "boundary": "Original policy",
                       "source_files": [uri]}, "leaf_items": ["leaf"], "ordinals": [4]}
    gap = {"reason": "Need to confirm an exception", "source_refs": [uri]}
    _save(root / "plan.json", {"deferred": [gap]})
    _save(root / "method-decisions/leaf.json", {"target": "deferred", "reason": gap["reason"]})
    original = _method_descriptor(root, 0, record)
    routed = _method_descriptor(root, 0, record, prepare_source_routing(root, state))
    assert len(routed["ordinals"]) < len(original["ordinals"])
    assert routed["ordinals"][0] == 4
    assert state["chunk_count"] - 1 in routed["ordinals"]
    assert routed["previous_gap"] == original["previous_gap"]
