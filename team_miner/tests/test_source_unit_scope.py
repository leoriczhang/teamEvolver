"""Exact routing preserves structural context without turning it into method instructions."""

from __future__ import annotations

import copy

import pytest

from team_miner.ov_case_batch_orchestration import case_source_roles
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.source_unit_extraction import (
    bind_source_unit_index,
    build_source_unit_packet,
    next_source_unit_assignment,
)
from team_miner.source_unit_scope import (
    exact_candidate_scope,
    exact_case_groups,
    exact_method_groups,
    route_exact_source_candidates,
)
from team_miner.source_units import load_source_unit, load_source_units
from team_miner.tests.test_source_unit_extraction import POLICY, output_for
from team_miner.tests.test_source_units import frozen


def route_fixture(root, *, row_count=2, max_units=16, uncertain=False, empty_file=False):
    text = "# 完整复核方法\n\n| 条件 | 方法 |\n| --- | --- |\n" + "".join(
        f"| 条件 {index} | 先核实输入，再按该条件确认结果。 |\n" for index in range(row_count)
    ) + "\n"
    state, manifest = frozen(root, [text, ""] if empty_file else [text])
    cursor, count, method_count = 0, 0, 0
    while cursor < manifest["unit_count"]:
        context = next_source_unit_assignment(root, cursor=cursor, count=manifest["unit_count"],
                                              unit_index=count, language_policy=POLICY, max_units=max_units)
        packet = build_source_unit_packet(root, context, POLICY)
        value = output_for(packet)
        value["context_uncertain"] = uncertain
        value["context_refs"] = [ref for ref, unit in zip(context["source_refs"], packet["evidence"], strict=True)
                                 if unit["kind"] in {"heading", "table_header", "separator", "empty"}]
        accepted = bind_source_unit_index(root, value, packet, context, f"child::{count}")
        _save(root / "units/0" / f"{count:08d}.json", accepted)
        for method in accepted["value"]["methods"]:
            item_id = f"method-{method_count}"
            _save(root / "catalog/method" / f"{method_count:08d}.json", {
                "item": {**method, "item_id": item_id},
                "source_unit_bindings": accepted["source_unit_bindings"],
            })
            _save(root / "method-decisions" / (item_id + ".json"), {"target": "candidate", "name": "review"})
            method_count += 1
        count += 1
        cursor = context["after"]
    _save(root / "catalog/manifest.json", {"method": method_count})
    _save(root / "plan.json", {"skills": [{"name": "review", "task": "复核方法", "boundary": "按原文条件"}]})
    _save(root / "scopes/review.json", {"ordinals": list(range(state["chunk_count"]))})
    state.update(unit_count=count, source_unit_count=manifest["unit_count"])
    audit = route_exact_source_candidates(root, state, "viking://resources")
    return _read(root / "scopes/review.json"), audit, state


def test_repeated_structural_context_is_allowed_primary_coverage_is_exact_and_blank_only_archived(tmp_path):
    scope, audit, state = route_fixture(tmp_path, row_count=10, max_units=4, empty_file=True)
    precise = exact_candidate_scope(tmp_path, scope)
    assert audit["coverage_complete"] and audit["source_unit_count"] == state["source_unit_count"]
    inputs = [identity for index in range(state["unit_count"])
              for identity in _read(tmp_path / "units/0" / f"{index:08d}.json")["assigned_unit_ids"]]
    assert len(inputs) == len(set(inputs)) == state["source_unit_count"]
    classified = [identity for index in range(state["unit_count"])
                  for values in _read(tmp_path / "units/0" / f"{index:08d}.json")["source_unit_bindings"].values()
                  for identity in values]
    assert len(set(classified)) < len(classified)  # Repeated headers are expected supplemental context.
    rows = load_source_units(tmp_path, precise["unit_ids"])
    assert {row["kind"] for row in rows} == {"heading", "table_header", "table_row"}
    assert all(load_source_unit(tmp_path, identity)["kind"] == "table_row" for identity in precise["operative_ids"])
    assert {load_source_unit(tmp_path, identity)["kind"] for identity in precise["context_ids"]} == {
        "heading", "table_header",
    }
    archived = load_source_units(tmp_path, list(range(state["source_unit_count"])))
    assert {row["kind"] for row in archived} >= {"separator", "empty"}


def test_case_roles_are_per_method_even_when_same_packet_contains_other_candidate_method_as_context(tmp_path):
    scope, _, _ = route_fixture(tmp_path, uncertain=True)
    groups = exact_case_groups(tmp_path, scope)
    assert len(groups) == 2
    assert groups[0]["unit_ids"] == groups[1]["unit_ids"]
    assert groups[0]["operative_ids"] != groups[1]["operative_ids"]
    assert exact_method_groups(tmp_path, scope) == [group["unit_ids"] for group in groups]
    chunks = load_source_units(tmp_path, groups[0]["unit_ids"])
    for index, group in enumerate(groups):
        roles = case_source_roles(chunks, scope, root=tmp_path, group_index=index)
        assert roles["method_input_ids"] == group["operative_ids"]
        other_method = groups[1 - index]["operative_ids"][0]
        assert other_method in roles["context_input_ids"]
        assert other_method not in roles["method_input_ids"]
    with pytest.raises(ValueError, match="unambiguous"):
        case_source_roles(chunks, scope, root=tmp_path)


def test_unique_group_can_be_matched_but_changed_assignment_or_scope_cannot(tmp_path):
    scope, _, _ = route_fixture(tmp_path)
    groups = exact_case_groups(tmp_path, scope)
    chunks = load_source_units(tmp_path, groups[0]["unit_ids"])
    assert case_source_roles(chunks, scope, root=tmp_path)["method_input_ids"] == groups[0]["operative_ids"]
    with pytest.raises(ValueError, match="do not match"):
        case_source_roles(chunks, scope, root=tmp_path, group_index=1)
    with pytest.raises(ValueError, match="archive"):
        case_source_roles(chunks, scope)
    for index in (True, -1, 2):
        with pytest.raises(ValueError, match="valid frozen group index"):
            case_source_roles(chunks, scope, root=tmp_path, group_index=index)
    changed = copy.deepcopy(scope)
    changed["source_units"]["method_groups"][0]["unit_ids"] = groups[1]["operative_ids"]
    with pytest.raises(ValueError, match="scope changed"):
        exact_case_groups(tmp_path, changed)


def test_duplicate_primary_classification_is_rejected_even_if_context_can_repeat(tmp_path):
    _, _, state = route_fixture(tmp_path, row_count=10, max_units=4)
    first = _read(tmp_path / "units/0/00000000.json")
    _save(tmp_path / "units/0/00000001.json", first)
    with pytest.raises(ValueError, match="more than one accepted extraction"):
        route_exact_source_candidates(tmp_path, state, "viking://resources")
