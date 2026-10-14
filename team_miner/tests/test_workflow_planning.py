"""Metadata retrieval may reorder methods, never decide semantic equivalence or alter Evidence."""

from __future__ import annotations

import copy
import sqlite3

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner import candidate_identity_orchestration as identities
from team_miner import candidate_refinement as refinement
from team_miner import workflow_planning as workflow
from team_miner.ov_compile_mining import RUNS_ROOT
from team_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL
from team_miner.tests.test_candidate_identity import candidate, fixture, group_current, row


def method(root, index, task, *, boundary="Use supplied runtime inputs and preserve the original constraints"):
    value = {
        "item": {"item_id": f"method-{index}", "kind": "method", "task": task, "boundary": boundary,
                 "source_refs": [f"scope-{index}"], "source_files": [f"viking://original/{index}.md"]},
        "ordinals": [index], "leaf_items": [f"leaf-{index}"], "unit_id": f"unit-{index}",
        "bindings": {f"scope-{index}": [index]},
    }
    _save(root / "catalog/method" / f"{index:08d}.json", value)
    return value


def test_global_order_offers_interleaved_persona_variants_together_without_changing_method_lineage(tmp_path):
    originals = []
    for index in range(32):
        topic = ("Schedule an appointment using two available times and confirm attendance"
                 if index % 2 == 0 else "Inspect a refund request and return an eligibility decision")
        originals.append(method(tmp_path, index, topic + f" for persona-{index} age {20 + index}"))
    frozen = {path: path.read_bytes() for path in (tmp_path / "catalog/method").iterdir()}
    workflow.prepare_workflow_order(tmp_path, 32)
    order = [workflow.workflow_method_index(tmp_path, cursor, 32) for cursor in range(32)]
    assert set(order) == set(range(32)) and len(set(order)) == 32
    # The existing eight-method model window now receives related variants from different files.
    assert all(index % 2 == 0 for index in order[:8])
    assert [originals[index]["leaf_items"] for index in order[:8]] == [[f"leaf-{index}"] for index in order[:8]]
    assert all(path.read_bytes() == raw for path, raw in frozen.items())
    workflow.prepare_workflow_order(tmp_path, 32)
    assert order == [workflow.workflow_method_index(tmp_path, cursor, 32) for cursor in range(32)]
    # Scheduling itself has produced no candidate or semantic disposition.
    assert not (tmp_path / "plan.json").exists()


def test_similar_trigger_different_goal_output_and_prohibition_are_kept_for_model_judgment(tmp_path):
    originals = [
        method(tmp_path, 0, "On a request review, decide refund eligibility", boundary="Never execute payment"),
        method(tmp_path, 1, "On a request review, execute approved payment", boundary="Requires payment approval"),
    ]
    workflow.prepare_workflow_order(tmp_path, 2)
    order = [workflow.workflow_method_index(tmp_path, i, 2) for i in range(2)]
    offered = [_read(tmp_path / "catalog/method" / f"{i:08d}.json") for i in order]
    assert offered == originals
    assert "keep separate only when the pair differs" in workflow.WORKFLOW_PLANNING_RULE
    assert "mandatory applicability boundary" in workflow.WORKFLOW_PLANNING_RULE
    assert "pairwise" in workflow.WORKFLOW_PLANNING_RULE
    assert "subset steps" in workflow.WORKFLOW_PLANNING_RULE
    assert "When equivalence" not in workflow.WORKFLOW_PLANNING_RULE


def test_coherent_roleplay_corpus_targets_phase_level_candidates_without_hard_capping():
    rule = workflow.WORKFLOW_PLANNING_RULE
    assert "not one candidate per scene, table row, customer" in rule
    assert "Use no target candidate count" in rule
    assert "source-supported use outside the parent workflow" in rule
    assert "mine the communication/training method" in rule
    assert "independently invokable" in rule


def test_large_catalog_uses_fixed_posting_probes_and_exact_once_order(tmp_path, monkeypatch):
    for index in range(1000):
        method(tmp_path, index, "Apply an independently reusable procedure with supplied variant " + str(index))
    connection = sqlite3.connect
    queries = []
    def traced(*args, **kwargs):
        db = connection(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(workflow.sqlite3, "connect", traced)
    workflow.prepare_workflow_order(tmp_path, 1000)
    order = [workflow.workflow_method_index(tmp_path, cursor, 1000) for cursor in range(1000)]
    assert sorted(order) == list(range(1000))
    probes = [query for query in queries if query.startswith("SELECT ordinal FROM postings")]
    assert probes and len(probes) <= 1000 * 16
    assert all(query.endswith("LIMIT 8") for query in probes)
    db = connection(tmp_path / "workflow-order/catalog.sqlite3")
    assert "posting_ordinal" in {item[1] for item in db.execute("PRAGMA index_list(postings)")}
    assert db.execute("SELECT COUNT(*) FROM schedule").fetchone()[0] == 1000
    db.close()


def test_interrupted_preparation_replays_same_assignment_and_rejects_changed_lineage(tmp_path, monkeypatch):
    for index in range(17):
        method(tmp_path, index, "Check a refund request and return a decision " + str(index))
    original_save = workflow._save
    monkeypatch.setattr(workflow, "_save", lambda *args: (_ for _ in ()).throw(OSError("late ready failure")))
    with pytest.raises(OSError, match="late ready"):
        workflow.prepare_workflow_order(tmp_path, 17)
    db = sqlite3.connect(tmp_path / "workflow-order/catalog.sqlite3")
    before = list(db.execute("SELECT position,ordinal,digest FROM schedule ORDER BY position"))
    # A process can also stop with just a durable prefix, before the next bounded window commits.
    with db:
        db.execute("DELETE FROM schedule WHERE position>=8")
    db.close()
    monkeypatch.setattr(workflow, "_save", original_save)
    workflow.prepare_workflow_order(tmp_path, 17)
    assert [workflow.workflow_method_index(tmp_path, i, 17) for i in range(17)] == [row[1] for row in before]
    path = tmp_path / "catalog/method/00000000.json"
    value = _read(path)
    value["bindings"]["scope-0"] = [999]
    _save(path, value)
    with pytest.raises(ValueError, match="lineage changed"):
        workflow.workflow_method_index(tmp_path, 0, 17)
    (tmp_path / "workflow-order/ready.json").unlink()
    with pytest.raises(ValueError, match="lineage changed"):
        workflow.prepare_workflow_order(tmp_path, 17)


def test_empty_catalog_is_valid_and_wrong_count_or_cursor_is_rejected(tmp_path):
    workflow.prepare_workflow_order(tmp_path, 0)
    assert _read(tmp_path / "workflow-order/ready.json")["count"] == 0
    with pytest.raises(ValueError, match="cursor"):
        workflow.workflow_method_index(tmp_path, 0, 0)
    with pytest.raises(ValueError, match="different method assignment"):
        workflow.prepare_workflow_order(tmp_path, 1)


def previous_scope(tmp_path):
    root = tmp_path / "round_inputs/round-2/staged"
    previous = tmp_path / "round_inputs/round-1/staged"
    relative = "original/requests.md"
    snapshot = f"{RUNS_ROOT}/job/rounds/round-1/source"
    job = {"job_id": "job", "current_round": 2,
           "source_snapshot": f"{RUNS_ROOT}/job/rounds/round-2/source",
           "staged_efficiency_protocol": EFFICIENCY_PROTOCOL}
    _save(tmp_path / "rounds/round-1/input.json", {"source_snapshot": snapshot})
    scope = {"scope_protocol": SOURCE_SCOPE_PROTOCOL, "ordinals": [1, 2], "method_ordinals": [2],
             "context_ordinals": [1], "full_ordinals": [0, 1, 2, 3]}
    _save(previous / "scopes/refund-review.json", scope)
    for ordinal in range(4):
        _save(previous / "chunk-metadata" / f"{ordinal:08d}.json", {
            "source_uri": snapshot + "/" + relative, "source_sha256": "exact-source-sha",
            "start_char": ordinal * 50, "end_char": (ordinal + 1) * 50,
        })
    current_uri = job["source_snapshot"] + "/" + relative
    _save(root / "source-files" / (_sha(current_uri) + ".json"), {"uri": current_uri, "ordinals": [3, 4, 5, 6, 7]})
    for ordinal, start in enumerate(range(0, 200, 40), 3):
        _save(root / "chunk-metadata" / f"{ordinal:08d}.json", {
            "source_uri": current_uri, "source_sha256": "exact-source-sha",
            "start_char": start, "end_char": start + 40,
        })
    return root, previous, job, relative


def test_prior_exact_scope_maps_changed_chunk_boundaries_preserving_shared_context(tmp_path):
    root, _, job, relative = previous_scope(tmp_path)
    mapped = refinement.mapped_previous_source_scope(job, root, "refund-review", [relative])
    assert mapped == {
        "scope_protocol": SOURCE_SCOPE_PROTOCOL, "ordinals": [4, 5, 6], "method_ordinals": [5, 6],
        "context_ordinals": [4], "shared_context_ordinals": [],
        "full_ordinals": [3, 4, 5, 6, 7], "refinement_ordinals": [5, 6],
    }


def test_prior_shared_constraints_keep_exact_scope_and_priority_when_new_scan_does_not_label_them(tmp_path):
    from team_miner.source_scope import route_candidate_scope

    root, previous, job, relative = previous_scope(tmp_path)
    path = previous / "scopes/refund-review.json"
    scope = _read(path)
    scope["shared_context_ordinals"] = [1]
    _save(path, scope)
    mapped = refinement.mapped_previous_source_scope(job, root, "refund-review", [relative])
    assert mapped["shared_context_ordinals"] == [4, 5]
    assert mapped["context_ordinals"] == [4, 5]
    # Different chunk boundaries can make an original shared interval overlap the method anchor.
    assert set(mapped["shared_context_ordinals"]) <= set(mapped["context_ordinals"]) <= set(mapped["ordinals"])
    merged = refinement.merge_candidate_scopes(mapped, {
        "scope_protocol": SOURCE_SCOPE_PROTOCOL, "ordinals": [7], "method_ordinals": [7],
        "context_ordinals": [], "shared_context_ordinals": [], "full_ordinals": [3, 4, 5, 6, 7],
    })
    assert merged["shared_context_ordinals"] == [4, 5]
    assert merged["context_ordinals"] == [4, 5]
    routing = {"chunk_count": 8, "shared_ordinals": [], "accepted_units_sha256": "new-scan", "coverage_complete": True}
    routed = route_candidate_scope(root, merged, routing)
    assert set(mapped["shared_context_ordinals"]) <= set(routed["shared_context_ordinals"])
    assert set(mapped["context_ordinals"]) <= set(routed["context_ordinals"])
    assert set(mapped["ordinals"]) <= set(routed["ordinals"])


@pytest.mark.parametrize("problem", ["changed_hash", "incomplete_range", "unknown_source"])
def test_unverified_previous_scope_cannot_claim_precision(tmp_path, problem):
    root, previous, job, relative = previous_scope(tmp_path)
    path = root / "chunk-metadata/00000005.json"
    value = _read(path)
    if problem == "changed_hash":
        value["source_sha256"] = "new-source-sha"
    elif problem == "incomplete_range":
        value["start_char"] = 105  # Leaves a gap inside the old method interval.
    else:
        path = previous / "chunk-metadata/00000002.json"
        value = _read(path)
        value["source_uri"] += "-unassigned"
    _save(path, value)
    assert refinement.mapped_previous_source_scope(job, root, "refund-review", [relative]) is None


def test_fresh_prior_authority_uses_exact_scope_but_frozen_legacy_keeps_full_files(tmp_path):
    root, _, job, relative = previous_scope(tmp_path)
    reviewed = tmp_path / "rounds/round-1/reviewed_output/semantic_reports/capability-plan.json"
    previous_candidate = candidate("refund-review", uri=job["source_snapshot"] + "/" + relative)
    _save(reviewed, {"skills": [previous_candidate]})
    prior = {"name": "refund-review", "scope_relative_paths": [relative],
             "case_ids": [f"stable:{i}" for i in range(12)]}
    prior_path = root.parent / "previous-candidates/refund-review.json"
    _save(prior_path, prior)
    _save(prior_path.parent / "manifest.json", [
        {"name": prior["name"], "local_path": str(prior_path), "sha256": _sha(prior_path.read_text())},
    ])
    _, precise = next(identities._prior_authorities(job, root))
    assert precise["method_ordinals"] == [5, 6] and precise["ordinals"] == [4, 5, 6]
    legacy = {**job, "staged_efficiency_protocol": ""}
    _, full = next(identities._prior_authorities(legacy, root))
    assert full["ordinals"] == full["method_ordinals"] == [3, 4, 5, 6, 7]
    assert full["refinement_ordinals"] == [5, 6]
    assert _read(prior_path)["case_ids"] == prior["case_ids"]
    path = root / "chunk-metadata/00000005.json"
    changed = _read(path)
    changed["source_sha256"] = "changed-source-sha"
    _save(path, changed)
    _, fallback = next(identities._prior_authorities(job, root))
    assert fallback["ordinals"] == fallback["method_ordinals"] == [3, 4, 5, 6, 7]
    assert "scope_protocol" not in fallback


def test_model_reviewed_persona_merge_preserves_method_context_and_independent_output(tmp_path):
    rows = [row(0, task="Schedule an appointment for supplied persona"),
            row(1, task="Schedule an appointment for another supplied persona"),
            row(2, task="Execute an approved refund payment")]
    for index, value in enumerate(rows):
        value["scope"] = {
            "scope_protocol": SOURCE_SCOPE_PROTOCOL,
            "ordinals": [index, 10 + index], "method_ordinals": [index],
            "context_ordinals": [10 + index], "full_ordinals": [index, 10 + index, 20 + index],
            "refinement_ordinals": [index],
        }
    fixture(tmp_path, rows)
    current, answer = group_current(rows)
    answer["groups"] = [
        {"candidate_ids": [item["candidate_id"] for item in rows[:2]],
         "reason": "One task with supplied persona variants"},
        {"candidate_ids": [rows[2]["candidate_id"]], "reason": "Independent purpose and output: execute payment"},
    ]
    original = copy.deepcopy(rows[0]["candidate"])
    identities._accept_groups(tmp_path, current, answer)
    assert _read(tmp_path / "plan.json")["skills"] == [original, rows[2]["candidate"]]
    scope = _read(tmp_path / "scopes/candidate-0.json")
    assert scope["method_ordinals"] == [0, 1]
    assert scope["context_ordinals"] == [10, 11]
    assert scope["ordinals"] == [0, 1, 10, 11]
    assert scope["full_ordinals"] == [0, 1, 10, 11, 20, 21]
    assert scope["refinement_ordinals"] == [0, 1]
    assert scope["scope_protocol"] == SOURCE_SCOPE_PROTOCOL
    assert _read(tmp_path / "identity/aliases.json")["candidate-1"]["name"] == "candidate-0"


def reuse_response():
    authority = candidate("refund-review")
    authority["candidate_id"] = "known-candidate"
    item = {"item_id": "method-1", "kind": "method", "task": "Review refund request",
            "boundary": "Respect original constraints", "source_refs": ["scope-1"]}
    row = {"reuse_candidate_id": authority["candidate_id"], "derived_from": [item["item_id"]],
           "reason": "Same independently reusable task"}
    value = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": ["unit-1"], "skills": [row],
             "knowledge": [], "deferred": [], "resolved": []}
    return value, authority, item


def validate_reuse(value, authority, item):
    return planning.validate_selection(value, items=[item], input_ids=["unit-1"],
                                       known_candidates={authority["candidate_id"]: authority})


def test_exact_redundant_reuse_fields_are_canonicalized_without_changing_raw_or_authority():
    value, authority, item = reuse_response()
    value["skills"][0].update({key: copy.deepcopy(authority[key]) for key in identities.IDENTITY_FIELDS})
    raw, known = copy.deepcopy(value), copy.deepcopy(authority)
    with pytest.raises(ValueError, match="rewrite"):
        validate_reuse(value, authority, item)
    checked = workflow.canonical_selection_reuse(value, {"known_candidates": [authority]})
    result = validate_reuse(checked, authority, item)
    assert result["skills"][0]["name"] == authority["name"]
    assert result["skills"][0]["inputs"] == authority["inputs"]
    assert result["skills"][0]["source_refs"] == item["source_refs"]
    assert set(checked["skills"][0]) == {"reuse_candidate_id", "derived_from", "reason"}
    assert value == raw and authority == known


@pytest.mark.parametrize("failure", ["changed", "unknown", "unknown_id", "ambiguous_authority"])
def test_reuse_normalization_never_authorizes_changed_unknown_or_ambiguous_business_fields(failure):
    value, authority, item = reuse_response()
    value["skills"][0]["task"] = authority["task"]
    known = [authority]
    if failure == "changed":
        value["skills"][0]["task"] = "Execute payment instead of judging eligibility"
    elif failure == "unknown":
        value["skills"][0]["unexpected"] = "model-added metadata"
    elif failure == "unknown_id":
        value["skills"][0]["reuse_candidate_id"] = "unassigned"
    else:
        known.append(copy.deepcopy(authority))
    checked = workflow.canonical_selection_reuse(value, {"known_candidates": known})
    assert checked == value
    with pytest.raises(ValueError):
        validate_reuse(checked, authority, item)


def test_reuse_normalization_keeps_v3_refinement_and_required_control_fields_strict():
    value, authority, item = reuse_response()
    value["schema_version"] = planning.SELECTION_SCHEMA_V3
    value["skills"][0]["task"] = authority["task"]
    assert workflow.canonical_selection_reuse(value, {"known_candidates": [authority]}) == value
    value["schema_version"] = planning.SELECTION_SCHEMA_V2
    value["skills"][0]["derived_from"] = ["unassigned-method"]
    checked = workflow.canonical_selection_reuse(value, {"known_candidates": [authority]})
    with pytest.raises(ValueError):
        validate_reuse(checked, authority, item)
