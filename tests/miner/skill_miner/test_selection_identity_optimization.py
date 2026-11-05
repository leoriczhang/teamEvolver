"""Finite repairs and versioned bounded identity batches preserve methods and source scope."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import GROUP_SCHEMA, IdentityCatalog, identity_version
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.selection_conflicts import collect_selection_conflicts, repair_selection_controls
from tests.miner.skill_miner.support.candidate_identity import candidate, fixture, row

LANGUAGE = {"language": "zh-CN"}
JOB = {"job_id": "job", "current_round": 1}


def controls():
    return {
        "items": [{"item_id": "method-1", "kind": "method", "task": "收到退款后处理独立任务",
                   "boundary": "原文边界", "source_refs": ["scope-1"]}],
        "input_ids": ["unit-1"], "allowed_source_refs": ["scope-1"],
        "selection_schema": planning.SELECTION_SCHEMA_V2, "known_candidates": [],
        "related_facts": [{"text": "辅助摘要", "source_file_ids": ["file-1"]}],
        "provisional_gaps": [{"reason": "局部摘录不足", "source_file_ids": ["file-1"]}],
    }


def proposed(name, task="另一个独立退款工作流"):
    value = candidate(name, task)
    value.pop("source_refs")
    value["derived_from"] = ["method-1"]
    return value


def test_two_colliding_names_are_collected_together_and_do_not_regress_on_repair(tmp_path):
    first, second = candidate("account-gathering"), candidate("report-explanation")
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA,
                                  "skills": [first, second], "deferred": []})
    original = controls()
    constraints = collect_selection_conflicts(JOB, tmp_path, {
        "skills": [proposed(first["name"]), proposed(second["name"])],
    }, original)
    assert [item["name"] for item in constraints] == [first["name"], second["name"]]
    repaired = repair_selection_controls(JOB, tmp_path, original, {"selection_conflicts": constraints},
                                         language_policy=LANGUAGE)
    assert set(repaired["reserved_candidate_names"]) == {first["name"], second["name"]}
    known = {item["candidate_id"]: item for item in repaired["known_candidates"]}
    assert {item["name"] for item in known.values()} == {first["name"], second["name"]}
    assert repaired["items"] == original["items"]
    assert repaired["input_ids"] == original["input_ids"]
    assert repaired["allowed_source_refs"] == original["allowed_source_refs"]
    assert repaired["related_facts"] == [] and repaired["context_omitted"]
    assert original["related_facts"]  # The old frozen packet is untouched.
    attempt2 = collect_selection_conflicts(JOB, tmp_path, {"skills": [proposed(first["name"])]},
                                          repaired, previous=constraints)
    assert attempt2 == constraints
    value = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": ["unit-1"],
             "skills": [proposed(first["name"])], "knowledge": [], "deferred": [], "resolved": []}
    with pytest.raises(ValueError, match="canonical workflow"):
        planning.validate_selection(value, items=original["items"], input_ids=original["input_ids"],
                                    known_candidates=known)
    identity = next(key for key, item in known.items() if item["name"] == first["name"])
    value["skills"] = [{"derived_from": ["method-1"], "reuse_candidate_id": identity,
                        "reason": "原文支持相同完整方法，沿用身份"}]
    accepted = planning.validate_selection(value, items=original["items"], input_ids=original["input_ids"],
                                          known_candidates=known)
    assert accepted["skills"][0]["name"] == first["name"]
    assert accepted["skills"][0]["source_refs"] == ["scope-1"]


def test_cumulative_constraints_remain_bounded_when_full_authorities_do_not_fit(tmp_path):
    candidates = []
    for index in range(24):
        value = candidate(f"reserved-{index}")
        value["inputs"] = [str(i) + "入" * 240 for i in range(16)]
        value["outputs"] = [str(i) + "出" * 240 for i in range(16)]
        candidates.append(value)
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA,
                                  "skills": candidates, "deferred": []})
    constraints = []
    original = controls()
    for attempt in range(3):
        constraints = collect_selection_conflicts(JOB, tmp_path, {
            "skills": [proposed(item["name"]) for item in candidates[attempt * 8:(attempt + 1) * 8]],
        }, original, previous=constraints)
    result = repair_selection_controls(JOB, tmp_path, original, {"selection_conflicts": constraints},
                                      language_policy=LANGUAGE, max_packet_chars=24000)
    packet = planning.build_stage_packet("plan", "selection.json", controls=result,
                                         language_policy=LANGUAGE, max_chars=24000)
    assert len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) <= 24000
    assert len(result["reserved_candidate_names"]) == 24
    assert 0 < len(result["known_candidates"]) < 8
    assert result["known_candidates_omitted"]
    for authority in result["known_candidates"]:
        original_row = next(row for row in candidates if row["name"] == authority["name"])
        assert authority["inputs"] == original_row["inputs"]
        assert authority["outputs"] == original_row["outputs"]


def test_exact_lookup_no_fuzzy_match_and_changed_authority_is_not_reused(tmp_path):
    value = candidate("canonical")
    path = tmp_path / "plan.json"
    _save(path, {"schema_version": planning.PLAN_SCHEMA, "skills": [value], "deferred": []})
    assert collect_selection_conflicts(JOB, tmp_path, {"skills": [proposed("canonical-similar")]}, controls()) == []
    constraints = collect_selection_conflicts(JOB, tmp_path, {"skills": [proposed("canonical")]}, controls())
    value["boundary"] = "后来变更的完整方法边界"
    _save(path, {"schema_version": planning.PLAN_SCHEMA, "skills": [value], "deferred": []})
    with pytest.raises(ValueError, match="frozen canonical"):
        repair_selection_controls(JOB, tmp_path, controls(), {"selection_conflicts": constraints},
                                  language_policy=LANGUAGE)


def run_windows(root, state):
    calls, assignments = 0, []
    while (window := orchestration._identity_window(root, state)) is not None:
        rows, context = window
        assignments.append(copy.deepcopy(context))
        ids = context["identity_ids"]
        orchestration._accept_groups(root, {"child": f"review-{calls}", "context": context}, {
            "schema_version": GROUP_SCHEMA, "candidate_ids": ids,
            "groups": [{"candidate_ids": [identity], "reason": "保守保留独立方法"} for identity in ids],
        })
        state["identity_cursor"] = context["after"]
        calls += 1
        assert calls < 256
    return calls, assignments


def test_sixty_four_candidates_batch_multiple_anchors_and_skip_unchanged_second_pass(tmp_path):
    values = []
    # Eight source-backed retrieval cohorts, each with eight independent identities.
    for index in range(64):
        group = index // 8
        value = row(index, task=f"cluster-{group}-procedure")
        for field in ("trigger", "boundary", "rationale"):
            value["candidate"][field] = f"cluster-{group}-{field}"
        value["candidate"]["inputs"] = [f"cluster-{group}-input"]
        value["candidate"]["outputs"] = [f"cluster-{group}-output"]
        value["candidate"]["source_refs"] = [f"viking://source/cluster-{group}.md"]
        values.append(value)
    fixture(tmp_path, values)
    original_plan = _read(tmp_path / "plan.json")
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": LANGUAGE,
             "execution_protocol": "te-mining-optimized.v1"}
    calls, assignments = run_windows(tmp_path, state)
    assert calls <= 16  # One partition can review several complete anchor relation sets.
    assert all(context["identity_pass"] == 0 for context in assignments)
    assert max(len(context["coverage_receipts"]) for context in assignments) > 1
    assert _read(tmp_path / "plan.json") == original_plan
    catalog = IdentityCatalog(tmp_path / "identity" / "catalog.sqlite3")
    try:
        for value in values:
            assert all(catalog.reviewed(value, other) for other in
                       catalog.related(value["candidate_id"], include_reviewed=True))
            receipt = _read(tmp_path / "identity" / "coverage" / f"0-{value['ordinal']:08d}.json")
            assert receipt["candidate_version"] == identity_version(value)
            assert not receipt["semantic_pairs_exhaustive"]
    finally:
        catalog.close()


def test_scope_version_changes_invalidate_relation_receipts(tmp_path):
    values = [row(0), row(1)]
    fixture(tmp_path, values)
    catalog = IdentityCatalog(tmp_path / "identity" / "catalog.sqlite3")
    catalog.compared([value["candidate_id"] for value in values])
    assert catalog.reviewed(*values)
    updated = copy.deepcopy(values[0])
    updated["scope"]["method_ordinals"].append(2)
    updated["candidate"]["source_refs"].append("viking://source/another.md")
    catalog.put(updated)
    assert not catalog.reviewed(updated, values[1])
    catalog.close()
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": LANGUAGE,
             "execution_protocol": "te-mining-optimized.v1"}
    assigned, context = orchestration._identity_window(tmp_path, state)
    assert {value["candidate_id"] for value in assigned} == {value["candidate_id"] for value in values}
    assert context["identity_versions"][updated["candidate_id"]] == identity_version(updated)


def test_large_same_source_collection_does_not_expand_neighbours_on_second_pass(tmp_path):
    values = [row(index) for index in range(64)]
    fixture(tmp_path, values)
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": LANGUAGE,
             "execution_protocol": "te-mining-optimized.v1"}
    calls, assignments = run_windows(tmp_path, state)
    assert calls <= 64
    assert state["identity_passes_scheduled"] == 1
    assert all(context["identity_pass"] == 0 for context in assignments)
    catalog = IdentityCatalog(tmp_path / "identity" / "catalog.sqlite3")
    try:
        for value in values:
            assert all(catalog.reviewed(value, other) for other in
                       catalog.related(value["candidate_id"], include_reviewed=True))
    finally:
        catalog.close()


def test_merge_requires_changed_survivor_version_to_be_reviewed_again(tmp_path):
    values = [row(index) for index in range(3)]
    fixture(tmp_path, values)
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": LANGUAGE,
             "execution_protocol": "te-mining-optimized.v1"}
    rows, context = orchestration._identity_window(tmp_path, state)
    ids = context["identity_ids"]
    first, second, third = ids
    orchestration._accept_groups(tmp_path, {"child": "merge-first-two", "context": context}, {
        "schema_version": GROUP_SCHEMA, "candidate_ids": ids,
        "groups": [{"candidate_ids": [first, second], "reason": "同一完整方法"},
                   {"candidate_ids": [third], "reason": "独立方法"}],
    })
    state["identity_cursor"] = context["after"]
    # The first review compared old survivor metadata, not its newly unioned scope.
    second_window = orchestration._identity_window(tmp_path, state)
    assert second_window is not None
    rows, second_context = second_window
    assert len(rows) == 2
    assert second_context["identity_versions"][first] != context["identity_versions"][first]
    second_ids = second_context["identity_ids"]
    orchestration._accept_groups(tmp_path, {"child": "new-scope-review", "context": second_context}, {
        "schema_version": GROUP_SCHEMA, "candidate_ids": second_ids,
        "groups": [{"candidate_ids": [identity], "reason": "独立方法"} for identity in second_ids],
    })
    state["identity_cursor"] = second_context["after"]
    assert orchestration._identity_window(tmp_path, state) is None
    assert state["identity_passes_scheduled"] == 2


def test_versioned_receipt_journal_replay_preserves_premerge_versions(tmp_path, monkeypatch):
    values = [row(index) for index in range(2)]
    fixture(tmp_path, values)
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": LANGUAGE,
             "execution_protocol": "te-mining-optimized.v1"}
    _, context = orchestration._identity_window(tmp_path, state)
    current = {"child": "crash-after-survivor-write", "context": context}
    ids = context["identity_ids"]
    value = {"schema_version": GROUP_SCHEMA, "candidate_ids": ids,
             "groups": [{"candidate_ids": ids, "reason": "同一完整方法"}]}
    original_io = orchestration._io
    def crash(path, document):
        if path.name == "aliases.json":
            raise OSError("journal crash")
        return original_io()[2](path, document)
    monkeypatch.setattr(orchestration, "_io", lambda: (*original_io()[:2], crash, *original_io()[3:]))
    with pytest.raises(OSError, match="journal crash"):
        orchestration._accept_groups(tmp_path, current, value)
    monkeypatch.setattr(orchestration, "_io", original_io)
    orchestration._accept_groups(tmp_path, current, value)
    journal = _read(tmp_path / "identity/journals/crash-after-survivor-write.json")
    assert journal["identity_versions"] == context["identity_versions"]
    assert (tmp_path / "identity/journals/crash-after-survivor-write.committed").is_file()
    catalog = IdentityCatalog(tmp_path / "identity/catalog.sqlite3")
    try:
        survivor = catalog.get(ids[0])
        assert identity_version(survivor) != context["identity_versions"][ids[0]]
        assert not catalog.reviewed(survivor, values[1])
    finally:
        catalog.close()


def test_legacy_frozen_round_keeps_original_retrieval_context(tmp_path):
    fixture(tmp_path, [row(index) for index in range(16)])
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": LANGUAGE}
    _, context = orchestration._identity_window(tmp_path, state)
    assert "identity_versions" not in context
    assert "coverage_receipts" not in context
    assert "identity_pass_version" not in state
