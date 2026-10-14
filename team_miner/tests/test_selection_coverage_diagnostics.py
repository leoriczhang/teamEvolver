"""Exact planning diagnostics repair protocol defects without assigning missing methods."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.selection_conflicts import collect_selection_conflicts, repair_selection_controls
from team_miner.tests.test_bounded_compile_plan import candidate

JOB = {"job_id": "coverage-test", "current_round": 1}
LANGUAGE = {"language": "en", "version": 1, "basis": "frozen_original_source"}


def assignment(count=24):
    items = [{"item_id": f"item-{index:024d}", "kind": "method", "task": f"Original procedure {index}",
              "boundary": "Preserve supplied conditions", "source_refs": [f"scope-{index}"]}
             for index in range(count)]
    controls = {"items": items, "input_ids": ["unit-original"],
                "allowed_source_refs": [item["source_refs"][0] for item in items],
                "selection_schema": planning.SELECTION_SCHEMA_V2, "known_candidates": []}
    value = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": controls["input_ids"],
             "skills": [{**{key: value for key, value in candidate([]).items() if key != "source_refs"},
                         "derived_from": [item["item_id"] for item in items]}],
             "knowledge": [], "deferred": [], "resolved": []}
    return controls, value


def validate(controls, value):
    return planning.validate_selection(value, items=controls["items"], input_ids=controls["input_ids"],
                                       known_candidates={row["candidate_id"]: row
                                                         for row in controls["known_candidates"]},
                                       max_input_items=24)


def diagnostics(controls, value):
    return planning.planning_coverage_diagnostics(value, [item["item_id"] for item in controls["items"]])


def packet_chars(controls):
    packet = planning.build_stage_packet("plan", "selection.json", controls=controls, language_policy=LANGUAGE)
    return len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))


def test_twenty_four_methods_with_two_missing_dispositions_have_correct_header_and_exact_diagnostics():
    controls, value = assignment()
    missing = value["skills"][0]["derived_from"][-2:]
    value["skills"][0]["derived_from"] = value["skills"][0]["derived_from"][:-2]
    original = copy.deepcopy(value)
    assert value["input_ids"] == controls["input_ids"]
    report = diagnostics(controls, value)
    assert report["missing_item_ids"] == missing
    assert not report["duplicate_item_ids"] and not report["unknown_item_ids"]
    assert report["assigned_item_count"] == 24 and report["observed_reference_count"] == 22
    with pytest.raises(ValueError, match="Planning dispositions") as error:
        validate(controls, value)
    assert all(identity in str(error.value) for identity in missing)
    assert "input_ids" not in str(error.value)
    assert value == original  # Diagnostics never fill the missing rows.


@pytest.mark.parametrize("within_one_row", [False, True])
def test_duplicate_disposition_names_the_exact_item_instead_of_claiming_header_is_wrong(within_one_row):
    controls, value = assignment(3)
    duplicate = value["skills"][0]["derived_from"][1]
    if within_one_row:
        value["skills"][0]["derived_from"].append(duplicate)
    else:
        value["knowledge"] = [{"derived_from": [duplicate], "reason": "Already assigned in another row"}]
    assert diagnostics(controls, value)["duplicate_item_ids"] == [duplicate]
    with pytest.raises(ValueError, match="duplicate=") as error:
        validate(controls, value)
    assert duplicate in str(error.value)


def test_unknown_identity_remains_rejected_and_is_explicitly_named():
    controls, value = assignment(2)
    value["skills"][0]["derived_from"].append("item-not-assigned")
    assert diagnostics(controls, value)["unknown_item_ids"] == ["item-not-assigned"]
    with pytest.raises(ValueError, match=r'unknown=\["item-not-assigned"\]'):
        validate(controls, value)


def test_invalid_header_has_a_different_error_from_complete_method_dispositions():
    controls, value = assignment(2)
    value["input_ids"] = ["unknown-container"]
    assert diagnostics(controls, value) is None
    with pytest.raises(ValueError, match="Planning input_ids.*container"):
        validate(controls, value)


def test_valid_selection_stays_unchanged_and_host_provenance_is_complete():
    controls, value = assignment()
    original = copy.deepcopy(value)
    assert diagnostics(controls, value) is None
    checked = validate(controls, value)
    assert value == original
    assert checked["skills"][0] == {**original["skills"][0], "source_refs": sorted(controls["allowed_source_refs"])}


def test_malformed_rows_are_not_auto_assigned_or_accepted():
    controls, value = assignment(2)
    value["skills"][0]["derived_from"] = "item-0"
    assert diagnostics(controls, value)["malformed_derived_from_rows"] == 1
    with pytest.raises(ValueError):
        validate(controls, value)


def test_diagnostic_ids_are_bounded_without_shortening_an_unknown_identity_into_another_id():
    controls, value = assignment(40)
    value["skills"][0]["derived_from"] = [f"unknown-{index}" for index in range(40)] + ["x" * 5000]
    report = diagnostics(controls, value)
    assert len(report["missing_item_ids"]) == 24 and report["omitted_missing_count"] == 16
    assert len(report["unknown_item_ids"]) == 24 and report["omitted_unknown_count"] == 17
    assert all(identity in value["skills"][0]["derived_from"] for identity in report["unknown_item_ids"])
    assert len(json.dumps(report)) < 2500


def test_exact_coverage_repair_notes_preserve_entire_frozen_assignment_and_do_not_mutate_packet(tmp_path):
    controls, value = assignment()
    value["skills"][0]["derived_from"] = value["skills"][0]["derived_from"][:-2]
    original = copy.deepcopy(controls)
    report = diagnostics(controls, value)
    repaired = repair_selection_controls(JOB, tmp_path, controls, {"planning_coverage": report},
                                         language_policy=LANGUAGE)
    assert repaired["planning_coverage"] == report
    assert "missing_item_id" in repaired["planning_coverage_repair_rule"]
    assert "do not invent" in repaired["planning_coverage_repair_rule"].lower()
    for field in ("items", "input_ids", "allowed_source_refs"):
        assert repaired[field] == controls[field]
    assert controls == original
    assert packet_chars(repaired) <= 32000


def test_coverage_hints_fit_actual_packet_by_omitting_only_readonly_notes(tmp_path):
    controls, value = assignment()
    value["skills"][0]["derived_from"].pop()
    report = diagnostics(controls, value)
    controls["related_facts"] = [{"text": "readonly " * 1200}]
    controls["provisional_gaps"] = [{"reason": "readonly " * 500}]
    budget = packet_chars(controls) + 100  # Original fits, adding diagnostic text does not.
    repaired = repair_selection_controls(JOB, tmp_path, controls, {"planning_coverage": report},
                                         language_policy=LANGUAGE, max_packet_chars=budget)
    assert repaired["related_facts"] == [] and repaired["provisional_gaps"] == []
    assert repaired["context_omitted"]
    assert packet_chars(repaired) <= budget
    for field in ("items", "input_ids", "allowed_source_refs"):
        assert repaired[field] == controls[field]
    assert repaired["planning_coverage"]["missing_item_ids"] == report["missing_item_ids"]


def test_oversize_core_assignment_is_rejected_rather_than_truncated_for_repair_notes(tmp_path):
    controls, value = assignment()
    value["skills"][0]["derived_from"].pop()
    report = diagnostics(controls, value)
    original = copy.deepcopy(controls)
    with pytest.raises(ValueError, match="exact coverage repair notes exceed"):
        repair_selection_controls(JOB, tmp_path, controls, {"planning_coverage": report},
                                  language_policy=LANGUAGE, max_packet_chars=packet_chars(controls))
    assert controls == original


@pytest.mark.parametrize("mutation", ["assignment", "missing", "unknown", "count"])
def test_stale_or_forged_coverage_hints_cannot_route_another_assignment(tmp_path, mutation):
    controls, value = assignment(2)
    value["skills"][0]["derived_from"].pop()
    report = diagnostics(controls, value)
    if mutation == "assignment":
        report["assigned_item_ids_sha256"] = "0" * 64
    elif mutation == "missing":
        report["missing_item_ids"] = ["not-assigned"]
    elif mutation == "unknown":
        report["unknown_item_ids"] = [controls["items"][0]["item_id"]]
    else:
        report["observed_reference_count"] = True
    with pytest.raises(ValueError, match="Planning coverage repair"):
        repair_selection_controls(JOB, tmp_path, controls, {"planning_coverage": report},
                                  language_policy=LANGUAGE)


def test_following_attempt_does_not_keep_stale_coverage_notes(tmp_path):
    controls, value = assignment(2)
    value["skills"][0]["derived_from"].pop()
    repaired = repair_selection_controls(JOB, tmp_path, controls, {"planning_coverage": diagnostics(controls, value)},
                                         language_policy=LANGUAGE)
    again = repair_selection_controls(JOB, tmp_path, repaired, {}, language_policy=LANGUAGE)
    assert "planning_coverage" not in again and "planning_coverage_repair_rule" not in again


def test_coverage_diagnostics_do_not_allow_same_name_business_rewrites_or_bypass_explicit_reuse(tmp_path):
    controls, value = assignment(3)
    authority = {**candidate([]), "candidate_id": "candidate-known", "candidate_metadata_is_evidence": False}
    authority.pop("source_refs")
    controls["known_candidates"] = [authority]
    value["skills"][0]["boundary"] = "A changed independent execution boundary"
    assert diagnostics(controls, value) is None
    with pytest.raises(ValueError, match="canonical workflow"):
        validate(controls, value)
    conflicts = collect_selection_conflicts(JOB, tmp_path, value, controls)
    value["skills"][0]["derived_from"].pop()
    report = diagnostics(controls, value)
    repaired = repair_selection_controls(JOB, tmp_path, controls,
                                         {"planning_coverage": report, "selection_conflicts": conflicts},
                                         language_policy=LANGUAGE, max_packet_chars=12000)
    assert repaired["known_candidates"][0]["boundary"] == authority["boundary"]
    assert repaired["planning_coverage"] == report
    assert packet_chars(repaired) <= 12000
    value["skills"] = [{"derived_from": [item["item_id"] for item in controls["items"]],
                        "reuse_candidate_id": authority["candidate_id"], "reason": "Same supported workflow"}]
    accepted = validate(controls, value)
    assert accepted["skills"][0]["boundary"] == authority["boundary"]
