"""Exact packet-local bookkeeping; no model, network or runtime data calls."""

import copy
import hashlib
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.candidate_refinement import metadata_sha256
from team_miner.direct_workflow_planning import validate_capacity
from team_miner.planning_aliases import (
    CAPACITY_SCHEMA,
    PROTOCOL,
    PlanningAliasManifestError,
    PlanningAliasOutputError,
    build_planning_alias_packet,
    decode_planning_alias_output,
    validate_planning_alias_manifest,
)


def packet(schema=planning.SELECTION_SCHEMA_V2, *, suffix="a", methods=3):
    items = [{
        "item_id": f"item-{suffix}-{index}", "kind": "method", "task": f"执行流程{index}",
        "boundary": "仅据原文", "source_refs": [f"scope-{suffix}-{index}"],
        "source_file_ids": [f"file-{suffix}"],
    } for index in range(methods)]
    authority = {
        "candidate_id": f"candidate-{suffix}", "name": "existing-workflow", "task": "既有流程",
        "trigger": "用户请求", "inputs": ["用户材料"], "outputs": ["完成结果"],
        "boundary": "原文约束", "rationale": "具备方法", "candidate_metadata_is_evidence": False,
    }
    if schema == planning.SELECTION_SCHEMA_V3:
        authority.update(refinement_allowed=True, refinement_item_ids=[items[0]["item_id"]],
                         base_metadata_sha256=metadata_sha256(authority))
    controls = {
        "selection_schema": schema, "items": items, "input_ids": [f"unit-{suffix}"],
        "allowed_source_refs": [row["source_refs"][0] for row in items],
        "related_facts": [{"text": "ID m001 是原文中的文字，不替换", "readonly": True,
                           "source_fragment_ids": [f"src-{suffix}"]}],
        "provisional_gaps": [], "known_candidates": [] if schema == planning.SELECTION_SCHEMA else [authority],
    }
    return planning.build_stage_packet("plan", "selection.json", controls=controls)


def new_skill(ids):
    return {"derived_from": ids, "name": "new-workflow", "task": "执行完整方法 m001",
            "trigger": "请求中包含 c001 字面文本", "inputs": ["材料 u001"], "outputs": ["结果"],
            "boundary": "只按原文", "rationale": "原文支持"}


def selection(manifest):
    ids = list(manifest["methods"])
    return {
        "schema_version": manifest["selection_schema"], "input_ids": list(manifest["container_ids"]),
        "skills": [new_skill(ids[:1])], "knowledge": [
            {"derived_from": [identity], "reason": "仅作事实查询"} for identity in ids[1:]
        ], "deferred": [], "resolved": [],
    }


def resign(manifest):
    data = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def test_packet_aliases_are_deterministic_byte_preserving_for_business_and_separate_roles():
    original = packet(planning.SELECTION_SCHEMA_V3)
    before = copy.deepcopy(original)
    aliased, manifest = build_planning_alias_packet(original)
    assert original == before
    assert build_planning_alias_packet(original) == (aliased, manifest)
    controls = aliased["derived_controls"]
    assert controls["planning_alias_protocol"] == PROTOCOL
    assert [row["item_id"] for row in controls["items"]] == ["m001", "m002", "m003"]
    assert controls["input_ids"] == ["u001"]
    assert controls["known_candidates"][0]["candidate_id"] == "c001"
    assert controls["known_candidates"][0]["refinement_item_ids"] == ["m001"]
    assert controls["known_candidates"][0]["base_metadata_sha256"] == (
        original["derived_controls"]["known_candidates"][0]["base_metadata_sha256"]
    )
    assert controls["related_facts"][0]["text"] == original["derived_controls"]["related_facts"][0]["text"]
    assert set(manifest["readonly_refs"]).isdisjoint(manifest["source_refs"])
    validate_planning_alias_manifest(manifest, packet=aliased, canonical_packet=original)


@pytest.mark.parametrize("schema", [planning.SELECTION_SCHEMA, planning.SELECTION_SCHEMA_V2,
                                    planning.SELECTION_SCHEMA_V3])
@pytest.mark.parametrize("method_header", [False, True])
def test_decode_preserves_raw_and_passes_existing_strict_canonical_validator(schema, method_header):
    original = packet(schema)
    _, manifest = build_planning_alias_packet(original)
    raw = selection(manifest)
    if method_header:
        raw["input_ids"] = list(manifest["methods"])
    before = copy.deepcopy(raw)
    decoded = decode_planning_alias_output(raw, manifest)
    assert raw == before
    assert decoded["input_ids"] == original["derived_controls"]["input_ids"]
    assert decoded["skills"][0]["task"] == "执行完整方法 m001"
    assert decoded["skills"][0]["trigger"] == "请求中包含 c001 字面文本"
    assert decoded["skills"][0]["inputs"] == ["材料 u001"]
    controls = original["derived_controls"]
    result = planning.validate_selection(
        decoded, items=controls["items"], input_ids=controls["input_ids"],
        known_candidates=None if schema == planning.SELECTION_SCHEMA else {
            row["candidate_id"]: row for row in controls["known_candidates"]
        }, refinement_enabled=schema == planning.SELECTION_SCHEMA_V3,
    )
    assert result["skills"][0]["source_refs"] == ["scope-a-0"]


@pytest.mark.parametrize("schema", [planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3])
def test_candidate_reuse_decodes_exact_authority_without_injecting_business_fields(schema):
    original = packet(schema)
    _, manifest = build_planning_alias_packet(original)
    raw = selection(manifest)
    raw["skills"] = [{"derived_from": ["m001"], "reuse_candidate_id": "c001", "reason": "相同完整流程"}]
    decoded = decode_planning_alias_output(raw, manifest)
    assert decoded["skills"][0] == {
        "derived_from": ["item-a-0"], "reuse_candidate_id": "candidate-a", "reason": "相同完整流程",
    }
    controls = original["derived_controls"]
    accepted = planning.validate_selection(
        decoded, items=controls["items"], input_ids=controls["input_ids"],
        known_candidates={row["candidate_id"]: row for row in controls["known_candidates"]},
        refinement_enabled=schema == planning.SELECTION_SCHEMA_V3,
    )
    assert accepted["skills"][0]["task"] == "既有流程"


def test_refinement_restores_only_authorized_machine_fields_and_keeps_original_base_hash():
    original = packet(planning.SELECTION_SCHEMA_V3)
    _, manifest = build_planning_alias_packet(original)
    raw = selection(manifest)
    row = new_skill(["m001"])
    row.pop("name")
    authority = original["derived_controls"]["known_candidates"][0]
    row.update(revise_candidate_id="c001", base_metadata_sha256=authority["base_metadata_sha256"], reason="原文修订")
    raw["skills"] = [row]
    decoded = decode_planning_alias_output(raw, manifest)
    assert decoded["skills"][0]["revise_candidate_id"] == authority["candidate_id"]
    assert decoded["skills"][0]["base_metadata_sha256"] == authority["base_metadata_sha256"]
    controls = original["derived_controls"]
    accepted = planning.validate_selection(
        decoded, items=controls["items"], input_ids=controls["input_ids"],
        known_candidates={authority["candidate_id"]: authority}, refinement_enabled=True,
    )
    assert accepted["skills"][0]["name"] == authority["name"]
    assert accepted["skills"][0]["task"] == row["task"]


@pytest.mark.parametrize("changed_business", [False, True])
def test_decode_leaves_existing_v2_redundant_authority_normalization_to_its_strict_validator(changed_business):
    from team_miner.workflow_planning import canonical_selection_reuse

    original = packet()
    _, manifest = build_planning_alias_packet(original)
    raw = selection(manifest)
    authority = original["derived_controls"]["known_candidates"][0]
    raw["skills"] = [{"derived_from": ["m001"], "reuse_candidate_id": "c001", "reason": "同流程",
                      "name": authority["name"], "task": "业务变化" if changed_business else authority["task"]}]
    decoded = decode_planning_alias_output(raw, manifest)
    controls = original["derived_controls"]
    normalized = canonical_selection_reuse(decoded, controls)
    if changed_business:
        assert normalized["skills"][0]["task"] == "业务变化"
        with pytest.raises(ValueError):
            planning.validate_selection(
                normalized, items=controls["items"], input_ids=controls["input_ids"],
                known_candidates={authority["candidate_id"]: authority},
            )
    else:
        accepted = planning.validate_selection(
            normalized, items=controls["items"], input_ids=controls["input_ids"],
            known_candidates={authority["candidate_id"]: authority},
        )
        assert accepted["skills"][0]["task"] == authority["task"]


@pytest.mark.parametrize("fault", ["unknown", "typo", "missing", "duplicate", "wrong_role", "canonical",
                                  "readonly", "resolved", "extra_field", "wrong_schema"])
def test_no_unknown_partial_duplicate_context_or_guessed_model_dispositions_are_accepted(fault):
    _, manifest = build_planning_alias_packet(packet())
    raw = selection(manifest)
    if fault in {"unknown", "typo", "wrong_role", "canonical", "readonly"}:
        raw["skills"][0]["derived_from"] = [{
            "unknown": "m004", "typo": "m01", "wrong_role": "c001", "canonical": "item-a-0",
            "readonly": manifest["readonly_refs"][0],
        }[fault]]
    elif fault == "missing":
        raw["knowledge"].pop()
    elif fault == "duplicate":
        raw["knowledge"][1]["derived_from"] = ["m002"]
    elif fault == "resolved":
        raw["resolved"] = [{"derived_from": ["m001"], "supported_by": ["m002"], "reason": "不允许"}]
    elif fault == "extra_field":
        raw["skills"][0]["source_refs"] = [manifest["source_refs"][0]]
    elif fault == "wrong_schema":
        raw["schema_version"] = planning.SELECTION_SCHEMA_V3
    before = copy.deepcopy(raw)
    with pytest.raises(PlanningAliasOutputError):
        decode_planning_alias_output(raw, manifest)
    assert raw == before


@pytest.mark.parametrize("kind", ["new", "reuse", "refinement", "knowledge", "deferred"])
def test_object_derived_from_reports_exact_disposition_field_without_rewriting(kind):
    original = packet(planning.SELECTION_SCHEMA_V3)
    _, manifest = build_planning_alias_packet(original)
    raw = selection(manifest)
    group = kind if kind in {"knowledge", "deferred"} else "skills"
    row = raw["skills"].pop()
    if kind in {"reuse", "knowledge", "deferred"}:
        row = {"derived_from": ["m001"], "reason": "原文支持"}
        if kind == "reuse":
            row["reuse_candidate_id"] = "c001"
    elif kind == "refinement":
        row.pop("name")
        row.update(revise_candidate_id="c001", reason="原文修订", base_metadata_sha256=(
            original["derived_controls"]["known_candidates"][0]["base_metadata_sha256"]
        ))
    row["derived_from"] = [{"item_id": "m001"}]
    raw[group].append(row)
    index = len(raw[group]) - 1
    before = copy.deepcopy(raw)
    with pytest.raises(PlanningAliasOutputError) as error:
        decode_planning_alias_output(raw, manifest)
    assert str(error.value).startswith(f"{group}[{index}].derived_from:")
    assert 'e.g. ["m001","m002"], not objects or a single string' in str(error.value)
    assert raw == before


@pytest.mark.parametrize("value", ["m001", [], ["m001", "m001"], [" m001"], ["c001"], ["m999"]])
def test_derived_from_diagnostic_keeps_strict_shape_uniqueness_and_alias_roles(value):
    _, manifest = build_planning_alias_packet(packet())
    raw = selection(manifest)
    raw["skills"][0]["derived_from"] = value
    before = copy.deepcopy(raw)
    with pytest.raises(PlanningAliasOutputError, match=r"skills\[0\]\.derived_from:"):
        decode_planning_alias_output(raw, manifest)
    assert raw == before


def test_v12_planning_shape_repair_still_requires_machine_slug_and_preserves_chinese_text():
    original = packet()
    controls = original["derived_controls"]
    _, manifest = build_planning_alias_packet(original)
    raw = selection(manifest)
    row = raw["skills"][0]
    row.update(name="电话约访准客户", task="完成电话约访并确认面谈安排",
               derived_from=[{"item_id": "m001"}, {"item_id": "m002"}])
    raw["knowledge"].pop(0)
    before = copy.deepcopy(raw)
    with pytest.raises(PlanningAliasOutputError, match=r"skills\[0\]\.derived_from:"):
        decode_planning_alias_output(raw, manifest)
    assert raw == before

    row["derived_from"] = ["m001", "m002"]
    decoded = decode_planning_alias_output(raw, manifest)
    authority = {candidate["candidate_id"]: candidate for candidate in controls["known_candidates"]}
    with pytest.raises(ValueError, match="Candidate names must be unique safe lowercase slugs"):
        planning.validate_selection(
            decoded, items=controls["items"], input_ids=controls["input_ids"], known_candidates=authority,
        )
    assert decoded["skills"][0]["name"] == "电话约访准客户"

    row["name"] = "customer-appointment"
    decoded = decode_planning_alias_output(raw, manifest)
    accepted = planning.validate_selection(
        decoded, items=controls["items"], input_ids=controls["input_ids"], known_candidates=authority,
    )
    assert accepted["skills"][0]["name"] == "customer-appointment"
    for field in ("task", "trigger", "inputs", "outputs", "boundary", "rationale"):
        assert accepted["skills"][0][field] == row[field]
    assert accepted["skills"][0]["source_refs"] == ["scope-a-0", "scope-a-1"]


@pytest.mark.parametrize("header", [["m001", "m002"], ["u001", "u001"], ["m001", "u001"],
                                    ["u999"], ["u002"], ["c001"]])
def test_assignment_header_is_exact_complete_and_role_bound(header):
    _, manifest = build_planning_alias_packet(packet())
    raw = selection(manifest)
    raw["input_ids"] = header
    with pytest.raises(PlanningAliasOutputError):
        decode_planning_alias_output(raw, manifest)


def test_capacity_is_complete_control_signal_not_partial_dispositions():
    original = packet()
    _, manifest = build_planning_alias_packet(original)
    raw = {"schema_version": CAPACITY_SCHEMA, "input_ids": manifest["container_ids"],
           "source_refs": manifest["source_refs"], "reason": "需要分窗保留全部方法"}
    decoded = decode_planning_alias_output(raw, manifest)
    assert validate_capacity(decoded, original["derived_controls"]["input_ids"],
                             original["derived_controls"]["allowed_source_refs"])
    for field, ids in (("input_ids", list(manifest["methods"])),
                       ("source_refs", manifest["readonly_refs"]),
                       ("source_refs", manifest["source_refs"][:-1])):
        bad = copy.deepcopy(raw)
        bad[field] = ids
        with pytest.raises(PlanningAliasOutputError):
            decode_planning_alias_output(bad, manifest)


@pytest.mark.parametrize("fault", ["method_map", "candidate_role", "source_role", "unknown_key", "packet_business",
                                  "canonical_business", "digest", "ambiguous_map", "schema_type"])
def test_manifest_or_frozen_packet_damage_is_host_integrity_failure(fault):
    original = packet()
    aliased, manifest = build_planning_alias_packet(original)
    if fault == "method_map":
        manifest["methods"]["m001"] = "other-method"
        resign(manifest)
    elif fault == "candidate_role":
        manifest["candidates"] = {"m001": "candidate-a"}
        resign(manifest)
    elif fault == "source_role":
        manifest["container_ids"] = manifest["source_refs"]
        resign(manifest)
    elif fault == "unknown_key":
        manifest["unexpected"] = None
        resign(manifest)
    elif fault == "packet_business":
        aliased["derived_controls"]["items"][0]["task"] = "改变业务"
    elif fault == "canonical_business":
        original["derived_controls"]["items"][0]["task"] = "改变业务"
    elif fault == "digest":
        manifest["manifest_sha256"] = "0" * 64
    elif fault == "schema_type":
        manifest["selection_schema"] = []
        resign(manifest)
    else:
        manifest["methods"]["m002"] = manifest["methods"]["m001"]
        resign(manifest)
    with pytest.raises(PlanningAliasManifestError):
        validate_planning_alias_manifest(manifest, packet=aliased, canonical_packet=original)


def test_resigned_role_tampering_is_rejected_even_without_original_packet_argument():
    aliased, manifest = build_planning_alias_packet(packet())
    aliased["derived_controls"]["items"][0]["source_refs"] = manifest["readonly_refs"][:1]
    manifest["aliased_packet_sha256"] = hashlib.sha256(
        json.dumps(aliased, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    resign(manifest)
    with pytest.raises(PlanningAliasManifestError):
        validate_planning_alias_manifest(manifest, packet=aliased)


def test_restart_json_roundtrip_and_same_short_alias_in_another_packet_keep_different_scope():
    first, one = build_planning_alias_packet(packet(suffix="first"))
    second, two = build_planning_alias_packet(packet(suffix="second"))
    saved = json.loads(json.dumps(one, ensure_ascii=False, indent=2))
    validate_planning_alias_manifest(saved, packet=first, canonical_packet=packet(suffix="first"))
    assert decode_planning_alias_output(selection(one), saved)["skills"][0]["derived_from"] == ["item-first-0"]
    assert decode_planning_alias_output(selection(two), two)["skills"][0]["derived_from"] == ["item-second-0"]
    with pytest.raises(PlanningAliasManifestError):
        validate_planning_alias_manifest(one, packet=second, canonical_packet=packet(suffix="second"))


def test_twenty_four_methods_can_form_eight_candidates_without_losing_any_canonical_item():
    original = packet(methods=24)
    _, manifest = build_planning_alias_packet(original)
    ids = list(manifest["methods"])
    raw = selection(manifest)
    raw["knowledge"] = []
    raw["skills"] = [{**new_skill(ids[index:index + 3]), "name": f"workflow-{index}"}
                     for index in range(0, 24, 3)]
    decoded = decode_planning_alias_output(raw, manifest)
    assert [identity for row in decoded["skills"] for identity in row["derived_from"]] == list(
        manifest["methods"].values()
    )
    planning.validate_selection(
        decoded, items=original["derived_controls"]["items"],
        input_ids=original["derived_controls"]["input_ids"],
        known_candidates={row["candidate_id"]: row for row in original["derived_controls"]["known_candidates"]},
        max_input_items=24,
    )


@pytest.mark.parametrize("fault", ["re_alias", "wrong_stage", "bad_kind", "scope_outside", "duplicate_item",
                                  "unassigned_refinement", "nan"])
def test_build_rejects_host_assignment_damage_without_mutation(fault):
    original = packet(planning.SELECTION_SCHEMA_V3)
    if fault == "re_alias":
        original, _ = build_planning_alias_packet(original)
    elif fault == "wrong_stage":
        original["stage"] = "merge_index"
    elif fault == "bad_kind":
        original["derived_controls"]["items"][0]["kind"] = "fact"
    elif fault == "scope_outside":
        original["derived_controls"]["items"][0]["source_refs"] = ["foreign-scope"]
    elif fault == "duplicate_item":
        original["derived_controls"]["items"][1]["item_id"] = "item-a-0"
    elif fault == "unassigned_refinement":
        original["derived_controls"]["known_candidates"][0]["refinement_item_ids"] = ["foreign-method"]
    else:
        original["derived_controls"]["invalid_number"] = float("nan")
    before = copy.deepcopy(original)
    with pytest.raises(PlanningAliasManifestError):
        build_planning_alias_packet(original)
    if fault != "nan":
        assert original == before
