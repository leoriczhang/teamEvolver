from __future__ import annotations

import copy
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.candidate_refinement import metadata_sha256
from team_miner.compile_strategy import (
    mining_policy_block,
    render_aliased_workflow_compiler,
    render_compact_aliased_workflow_compiler,
)
from team_miner.direct_workflow_planning import CAPACITY_SCHEMA, validate_capacity
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.planning_aliases import build_planning_alias_packet, decode_planning_alias_output
from team_miner.tests.dataset_fixtures import build_document
from team_miner.workflow_planning import COMPACT_PLANNING_PROTOCOL, coalesce_exact_authority_reuse


def _fresh_capacity(value, input_ids, source_refs):
    return validate_capacity(value, input_ids, source_refs, max_reason_chars=planning.MAX_OUTPUT_CHARS)


def authority(identity="candidate-exact", name="request-review"):
    return {
        "candidate_id": identity,
        "name": name,
        "task": "处理完整请求",
        "trigger": "收到请求",
        "inputs": ["原始条件和当前请求"],
        "outputs": ["处理结论"],
        "boundary": "只用所提供的原始条件",
        "rationale": "完整独立任务",
        "candidate_metadata_is_evidence": False,
    }


def controls(count=2, schema=planning.SELECTION_SCHEMA_V2):
    return {
        "selection_schema": schema,
        "input_ids": ["exact-container"],
        "allowed_source_refs": [f"original-source-{index}" for index in range(count)],
        "known_candidates": [authority()],
        "items": [
            {
                "item_id": f"method-{index}",
                "kind": "method",
                "task": "处理请求",
                "boundary": "仅提供原文范围",
                "source_refs": [f"original-source-{index}"],
            }
            for index in range(count)
        ],
    }


def selection(control):
    return {
        "schema_version": control["selection_schema"],
        "input_ids": control["input_ids"],
        "skills": [
            {
                "reuse_candidate_id": "candidate-exact",
                "reason": f"同完整流程的已支持分支-{index}",
                "derived_from": [item["item_id"]],
            }
            for index, item in enumerate(control["items"])
        ],
        "knowledge": [],
        "deferred": [],
        "resolved": [],
    }


@pytest.mark.parametrize("schema", [planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3])
def test_same_authority_coalesces_disjoint_methods_and_preserves_all_reasons_raw(schema):
    control = controls(24, schema)
    value = selection(control)
    original = copy.deepcopy(value)
    original_controls = copy.deepcopy(control)
    normalized, audit = coalesce_exact_authority_reuse(value, control)
    assert len(normalized["skills"]) == 1
    assert normalized["skills"][0]["derived_from"] == [item["item_id"] for item in control["items"]]
    assert audit["merged_rows"][0]["original_rows"] == [
        {"row_index": index, "reason": row["reason"], "derived_from": row["derived_from"]}
        for index, row in enumerate(value["skills"])
    ]
    assert value == original and control == original_controls
    accepted = planning.validate_selection(
        normalized,
        items=control["items"],
        input_ids=control["input_ids"],
        known_candidates={row["candidate_id"]: row for row in control["known_candidates"]},
        refinement_enabled=schema == planning.SELECTION_SCHEMA_V3,
        max_input_items=24,
    )
    assert accepted["skills"][0]["source_refs"] == sorted(control["allowed_source_refs"])
    assert all(
        accepted["skills"][0][key] == authority()[key]
        for key in ("name", "task", "trigger", "inputs", "outputs", "boundary", "rationale")
    )


def test_twenty_four_methods_preserve_twenty_eight_distinct_assigned_source_scopes():
    control = controls(24)
    extras = [f"original-source-{index}" for index in range(24, 28)]
    control["items"][0]["source_refs"].extend(extras)
    control["allowed_source_refs"].extend(extras)
    original = selection(control)
    normalized, audit = coalesce_exact_authority_reuse(original, control)
    accepted = planning.validate_selection(
        normalized, items=control["items"], input_ids=control["input_ids"],
        known_candidates={"candidate-exact": control["known_candidates"][0]}, max_input_items=24,
    )
    assert len(accepted["skills"][0]["source_refs"]) == 28
    assert accepted["skills"][0]["source_refs"] == sorted(control["allowed_source_refs"])
    assert len(audit["merged_rows"][0]["original_rows"]) == 24
    assert len(normalized["skills"][0]["derived_from"]) == 24
    assert COMPACT_PLANNING_PROTOCOL == "te-compact-planning.v1"


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "missing_union"])
def test_projection_cannot_hide_corrupt_frozen_source_assignments(fault):
    control = controls()
    if fault == "unknown":
        control["items"][0]["source_refs"] = ["foreign-source"]
    elif fault == "duplicate":
        control["items"][0]["source_refs"] *= 2
    else:
        control["allowed_source_refs"].append("unassigned-source")
    with pytest.raises(ValueError):
        coalesce_exact_authority_reuse(selection(control), control)


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "unknown-method", "unknown-authority", "metadata", "header", "reason", "extra-top"],
)
def test_projection_does_not_fill_coverage_or_rewrite_business_authority(fault):
    control = controls()
    value = selection(control)
    if fault == "missing":
        value["skills"].pop()
    elif fault == "duplicate":
        value["skills"][1]["derived_from"] = ["method-0"]
    elif fault == "unknown-method":
        value["skills"][1]["derived_from"] = ["outside-method"]
    elif fault == "unknown-authority":
        value["skills"][1]["reuse_candidate_id"] = "outside-authority"
    elif fault == "metadata":
        value["skills"][1]["boundary"] = "扩大成无条件"
    elif fault == "header":
        value["input_ids"] = []
    elif fault == "reason":
        value["skills"][1]["reason"] = "x" * 1001
    else:
        value["extra-top"] = []
    with pytest.raises(ValueError):
        coalesce_exact_authority_reuse(value, control)


def test_nine_different_authorities_still_exceed_eight_candidate_gate():
    control = controls(9)
    control["known_candidates"] = [authority(f"candidate-{index}", f"workflow-{index}") for index in range(9)]
    value = selection(control)
    for index, row in enumerate(value["skills"]):
        row["reuse_candidate_id"] = f"candidate-{index}"
    with pytest.raises(ValueError, match="eight candidates"):
        coalesce_exact_authority_reuse(value, control)


def test_refinement_hash_and_reuse_revision_collision_still_fail():
    control = controls(schema=planning.SELECTION_SCHEMA_V3)
    known = control["known_candidates"][0]
    known.update(
        base_metadata_sha256=metadata_sha256(known),
        refinement_allowed=True,
        refinement_item_ids=["method-0", "method-1"],
    )
    value = selection(control)
    row = {key: copy.deepcopy(known[key]) for key in ("task", "trigger", "inputs", "outputs", "boundary", "rationale")}
    row.update(
        revise_candidate_id=known["candidate_id"],
        base_metadata_sha256="bad-hash",
        reason="原文修订",
        derived_from=["method-0"],
    )
    value["skills"][0] = row
    with pytest.raises(ValueError, match="hash"):
        coalesce_exact_authority_reuse(value, control)
    row["base_metadata_sha256"] = known["base_metadata_sha256"]
    with pytest.raises(ValueError, match="unique"):
        coalesce_exact_authority_reuse(value, control)  # reuse and refinement are different forms, never auto-merged.


def test_authorized_v3_refinement_preserves_immutable_base_and_twelve_custom_case_identities():
    control = controls(schema=planning.SELECTION_SCHEMA_V3)
    known = control["known_candidates"][0]
    base_hash = metadata_sha256(known)
    known.update(base_metadata_sha256=base_hash, refinement_allowed=True,
                 refinement_item_ids=["method-0", "method-1"])
    row = {key: copy.deepcopy(known[key]) for key in ("task", "trigger", "inputs", "outputs", "boundary", "rationale")}
    row["task"] = "先核验原文条件再处理完整请求"
    row.update(revise_candidate_id=known["candidate_id"], base_metadata_sha256=base_hash,
               reason="当前原文支持明确核验步骤", derived_from=["method-0", "method-1"])
    value = selection(control)
    value["skills"] = [row]
    original_known = copy.deepcopy(known)
    normalized, audit = coalesce_exact_authority_reuse(value, control)
    assert normalized == value and not audit["merged_rows"]
    accepted = planning.validate_selection(
        normalized, items=control["items"], input_ids=control["input_ids"],
        known_candidates={known["candidate_id"]: known},
        refinement_enabled=True, max_input_items=24,
    )
    assert accepted["skills"][0]["name"] == known["name"]
    assert accepted["skills"][0]["task"] == row["task"]
    assert known == original_known and metadata_sha256(known) == base_hash
    ids = [f"prior-custom-opaque-{index}" for index in range(12)]
    cases = []
    for index, identity in enumerate(ids):
        case = build_document(known["name"], [{"query": "Fixture"}])["cases"][0]
        case.update(case_id=identity, query=f"Source-backed distinct request variant {index}")
        case["provenance"]["session_ids"] = ["viking://resources/source/original.md"]
        cases.append(case)
    dataset = planning.assemble_dataset(
        cases, skill_name=known["name"], task=accepted["skills"][0]["task"], identity="refined-case-set",
        expected_case_ids=ids,
    )
    assert [case["case_id"] for case in dataset["cases"]] == ids


def test_actual_alias_decode_before_projection_preserves_machine_roles_and_exact_sources():
    control = controls()
    packet = planning.build_stage_packet("plan", "selection.json", controls=control)
    aliased, manifest = build_planning_alias_packet(packet)
    value = {
        "schema_version": planning.SELECTION_SCHEMA_V2,
        "input_ids": aliased["derived_controls"]["input_ids"],
        "skills": [
            {"reuse_candidate_id": "c001", "reason": "完整相同流程", "derived_from": [f"m{index:03d}"]}
            for index in (1, 2)
        ],
        "knowledge": [],
        "deferred": [],
        "resolved": [],
    }
    decoded = decode_planning_alias_output(value, manifest)
    normalized, _ = coalesce_exact_authority_reuse(decoded, control)
    assert normalized["skills"][0]["derived_from"] == ["method-0", "method-1"]
    value["skills"][1]["derived_from"] = [aliased["derived_controls"]["input_ids"][0]]
    with pytest.raises(ValueError):
        decode_planning_alias_output(value, manifest)


def long_identity_assignment():
    refs = ["scope-" + f"{index:024d}" for index in range(28)]
    control = {
        "selection_schema": planning.SELECTION_SCHEMA_V2,
        "input_ids": ["unit-" + f"{index:024d}" for index in range(12)],
        "allowed_source_refs": refs,
        "known_candidates": [],
        "items": [{"item_id": "item-" + f"{index:024d}", "kind": "method",
                   "task": "Complete assigned method", "boundary": "Original boundary",
                   "source_refs": refs[index:index + 1] if index < 23 else refs[23:]}
                  for index in range(24)],
    }
    packet = planning.build_stage_packet("plan", "selection.json", controls=control)
    aliased, manifest = build_planning_alias_packet(packet)
    return control, aliased["derived_controls"], manifest


def test_host_id_expansion_preserves_valid_business_fields_without_enlarging_model_output_budget():
    control, aliased, manifest = long_identity_assignment()
    value = {
        "schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": aliased["input_ids"],
        "skills": [{"name": f"workflow-{index}", "task": "T" * 325, "trigger": "G" * 325,
                    "inputs": ["Original request"], "outputs": ["Verified result"],
                    "boundary": "B" * 400, "rationale": "R" * 150,
                    "derived_from": [f"m{ordinal + 1:03d}" for ordinal in range(index * 3, (index + 1) * 3)]}
                   for index in range(8)],
        "knowledge": [], "deferred": [], "resolved": [],
    }
    raw = copy.deepcopy(value)
    # Prove this is valid business output, rather than only a length calculation.
    planning.validate_selection(value, items=aliased["items"], input_ids=aliased["input_ids"],
                                known_candidates={}, max_input_items=24)
    decoded = decode_planning_alias_output(value, manifest)
    raw_chars, canonical_chars = len(planning._json(value)), len(planning._json(decoded))
    assert raw_chars < 12000 < canonical_chars
    with pytest.raises(ValueError, match="12000"):
        coalesce_exact_authority_reuse(decoded, control)
    normalized, audit = coalesce_exact_authority_reuse(
        decoded, control, _max_output_chars=12000 + canonical_chars - raw_chars,
    )
    assert normalized == decoded and value == raw and not audit["merged_rows"]
    invalid_business = copy.deepcopy(decoded)
    invalid_business["skills"][0]["boundary"] += "B"
    with pytest.raises(ValueError, match="400"):
        coalesce_exact_authority_reuse(invalid_business, control, _max_output_chars=13000)
    oversized_raw = copy.deepcopy(value)
    oversized_raw["skills"][0]["rationale"] = "R" * 14000
    with pytest.raises(ValueError, match="12000"):
        decode_planning_alias_output(oversized_raw, manifest)


def test_capacity_exact_host_id_expansion_keeps_all_assigned_sources_and_the_old_default_limit():
    control, aliased, manifest = long_identity_assignment()
    value = {"schema_version": CAPACITY_SCHEMA, "input_ids": aliased["input_ids"],
             "source_refs": aliased["allowed_source_refs"], "reason": "D" * 10600}
    decoded = decode_planning_alias_output(value, manifest, max_capacity_reason_chars=12000)
    raw_chars, canonical_chars = len(planning._json(value)), len(planning._json(decoded))
    assert raw_chars < 12000 < canonical_chars
    with pytest.raises(ValueError, match="12000"):
        validate_capacity(decoded, control["input_ids"], control["allowed_source_refs"], max_reason_chars=12000)
    limit = 12000 + canonical_chars - raw_chars
    assert validate_capacity(decoded, control["input_ids"], control["allowed_source_refs"],
                             max_reason_chars=12000, _max_output_chars=limit)
    decoded["source_refs"].append("unassigned-canonical-source")
    with pytest.raises(ValueError, match="exact assigned"):
        validate_capacity(decoded, control["input_ids"], control["allowed_source_refs"],
                          max_reason_chars=12000, _max_output_chars=limit)


def test_fresh_alias_decode_allows_exact_repeated_authority_rows_only_before_coalescing():
    control = controls(24)
    packet = planning.build_stage_packet("plan", "selection.json", controls=control)
    aliased, manifest = build_planning_alias_packet(packet)
    value = {"schema_version": planning.SELECTION_SCHEMA_V2,
             "input_ids": aliased["derived_controls"]["input_ids"],
             "skills": [{"reuse_candidate_id": "c001", "reason": f"Assigned independent subset {index}",
                         "derived_from": [identity]}
                        for index, identity in enumerate(manifest["methods"])],
             "knowledge": [], "deferred": [], "resolved": []}
    with pytest.raises(ValueError, match="eight|8 Skill"):
        decode_planning_alias_output(value, manifest)
    decoded = decode_planning_alias_output(value, manifest, max_skill_rows=24)
    normalized, audit = coalesce_exact_authority_reuse(decoded, control)
    assert len(normalized["skills"]) == 1
    assert normalized["skills"][0]["derived_from"] == [item["item_id"] for item in control["items"]]
    assert len(audit["merged_rows"][0]["original_rows"]) == 24
    value["skills"][1]["derived_from"] = value["skills"][0]["derived_from"]
    with pytest.raises(ValueError, match="exactly once"):
        decode_planning_alias_output(value, manifest, max_skill_rows=24)


def capacity(reason):
    return {
        "schema_version": CAPACITY_SCHEMA,
        "input_ids": ["container"],
        "source_refs": ["first", "second"],
        "reason": reason,
    }


def test_capacity_1151_is_only_diagnostics_and_whole_12k_gate_stays_strict():
    value = capacity("x" * 1151)
    original = copy.deepcopy(value)
    with pytest.raises(ValueError):
        validate_capacity(value, ["container"], ["first", "second"])
    assert _fresh_capacity(value, ["container"], ["first", "second"])
    assert value == original
    with pytest.raises(ValueError):
        _fresh_capacity(capacity("x" * 12000), ["container"], ["first", "second"])


@pytest.mark.parametrize("fault", ["foreign", "missing", "duplicate", "header", "empty-reason", "extra"])
def test_long_capacity_never_loosens_reference_or_shape_guards(fault):
    value = capacity("x" * 1151)
    if fault == "foreign":
        value["source_refs"][0] = "outside"
    elif fault == "missing":
        value["source_refs"].pop()
    elif fault == "duplicate":
        value["source_refs"] = ["first", "first"]
    elif fault == "header":
        value["input_ids"] = []
    elif fault == "empty-reason":
        value["reason"] = " "
    else:
        value["skills"] = []
    with pytest.raises(ValueError):
        _fresh_capacity(value, ["container"], ["first", "second"])


@pytest.mark.parametrize("limit", [True, False, 0, 12001, "12000"])
def test_capacity_diagnostic_keyword_is_bounded_and_explicit(limit):
    with pytest.raises(ValueError, match="diagnostic limit"):
        validate_capacity(capacity("Diagnostic only"), ["container"], ["first", "second"], max_reason_chars=limit)


@pytest.mark.parametrize("refinement", [False, True])
def test_compact_contract_preserves_every_business_policy_and_public_pipeline(refinement):
    policy = "业务原文逐字保留：never generate a complete Test Dataset or merge sibling cases.\nDo not rewrite this."
    stages = {
        key: key + "\n" + policy
        for key in ("plan", "merge_index", "merge_batch", "direct_workflow", "selection_refinement")
    }
    base = "---\nname: policy-fixture\ndescription: Shape-only policy test.\n---\n" + mining_policy_block(
        policy, stages
    )
    old = render_aliased_workflow_compiler(base, refinement=refinement)
    body = render_compact_aliased_workflow_compiler(base, refinement=refinement)
    contract = json.loads(body.split("```json\n")[-1].split("\n```", 1)[0])
    instructions = contract["synthesize"]["instructions"]
    for key in ("plan", "merge_index", "merge_batch", "direct_workflow"):
        assert stages[key] in instructions
    assert (stages["selection_refinement"] in instructions) == refinement
    assert "p.map(sources" in body and "p.finalize(files" in body
    assert "<=200 chars" in instructions and "EXACTLY ONCE" in instructions
    assert "Every derived_from is a nonempty array of unique exact assigned m alias strings" in instructions
    assert '["m001","m002"]' in instructions
    assert 'never objects such as [{"item_id":"m001"}] or a single string' in instructions
    assert "name is a machine identifier: unique lowercase ASCII kebab slug <=64 chars" in instructions
    assert "for zh-CN these and later Skill titles must remain Chinese" in instructions
    assert "This selection has no title field" in instructions
    assert render_aliased_workflow_compiler(base, refinement=refinement) == old
    assert len(render_compact_aliased_workflow_compiler(SKILL_BODY, refinement=refinement)) < len(
        render_aliased_workflow_compiler(SKILL_BODY, refinement=refinement)
    )


def test_compact_plan_requires_independent_triggers_and_pairwise_overlap_merge():
    body = render_compact_aliased_workflow_compiler(SKILL_BODY)
    contract = json.loads(body.split("```json\n")[-1].split("\n```", 1)[0])
    instructions = contract["synthesize"]["instructions"]
    for requirement in (
        "smallest complete set of independently reusable workflows",
        "Every new destination must have",
        "terminal result a user needs independently",
        "procedure reusable without completing its neighboring phase",
        "audit every proposed pair for same-journey overlap",
        "do not create one Candidate per document, heading or extraction row",
    ):
        assert requirement in instructions


def test_no_twelve_case_or_custom_identity_gate_is_changed_by_plan_projection():
    ids = [f"opaque-replay-{index}" for index in range(12)]
    cases = []
    for index, identity in enumerate(ids):
        value = build_document("workflow", [{"query": "Fixture"}])["cases"][0]
        value["case_id"] = identity
        value["query"] = f"Complete source-backed distinct task {index}"
        value["provenance"]["session_ids"] = ["viking://resources/source/original.md"]
        cases.append(value)
    dataset = planning.assemble_dataset(
        cases, skill_name="workflow", task="Complete task", identity="set", expected_case_ids=ids
    )
    assert [case["case_id"] for case in dataset["cases"]] == ids
    assert all(len([check for check in case["checks"] if check["kind"] == "output"]) >= 12 for case in cases)
    with pytest.raises(ValueError):
        planning.assemble_dataset(
            cases[:11], skill_name="workflow", task="Complete task", identity="set", expected_case_ids=ids
        )
