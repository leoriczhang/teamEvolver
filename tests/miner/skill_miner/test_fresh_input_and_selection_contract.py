"""Fresh compiler hints retain the real schemas, public operations."""

import ast
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from team_miner.skill_miner.ov_staged_orchestration import _canonical_extract_references
from tests.miner.skill_miner.fixtures import build_document
from tests.miner.skill_miner.support.bounded_compile_plan import candidate, planning_catalog
from tests.miner.skill_miner.support.compile_strategy import embedded_contract


def _substitute(value, replacements):
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    return replacements.get(value, value)


def _selection_example(*, refinement=False):
    units, _ = planning_catalog(2)
    items = planning.planning_items([{
        "id": "shared-control-unit", "value": {
            "methods": [unit["value"]["methods"][0] for unit in units], "facts": [], "deferred": [],
        },
    }])
    authority = {key: item for key, item in candidate([]).items() if key != "source_refs"}
    authority["name"] = "existing-method"
    identity = "candidate-existing-method"
    replacements = {
        "<exact assigned control unit ID>": "shared-control-unit",
        "<new-safe-kebab-name>": "new-method",
        "<supported complete task>": items[0]["task"],
        "<supported applicability>": "A request invokes this supported method.",
        "<required runtime input>": "The request and its provided Evidence.",
        "<terminal result>": "A recorded result.",
        "<supported conditions and limits>": items[0]["boundary"],
        "<source-supported selection reason>": "The assigned source describes this independent method.",
        "<assigned item ID A>": items[0]["item_id"],
        "<assigned item ID B>": items[1]["item_id"],
        "<exact assigned known candidate ID>": identity,
        "<why this method belongs to that same complete workflow>": "The assigned detail belongs to this workflow.",
    }
    template = (strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE_V3 if refinement
                else strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE)
    value = _substitute(json.loads(template), replacements)
    if refinement:
        authority.update(refinement_allowed=True, refinement_item_ids=[items[1]["item_id"]])
    return items, value, {identity: authority}


@pytest.mark.parametrize("refinement", [False, True])
def test_selection_clarity_only_appends_to_the_actual_existing_synthesize_instructions(refinement):
    old = strategy.render_recovery_workflow_compiler(SKILL_BODY, refinement=refinement)
    fresh = strategy.render_local_recovery_workflow_compiler(SKILL_BODY, refinement=refinement)
    old_contract, contract = embedded_contract(old), embedded_contract(fresh)
    guidance = (strategy.LOCAL_SELECTION_CONTRACT_GUIDANCE_V3 if refinement
                else strategy.LOCAL_SELECTION_CONTRACT_GUIDANCE)
    example = (strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE_V3 if refinement
               else strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE)
    assert contract["synthesize"]["instructions"] == (
        old_contract["synthesize"]["instructions"] + "\n\n" + guidance
    )
    original_instructions = contract["synthesize"].pop("instructions")
    contract["synthesize"]["instructions"] = old_contract["synthesize"]["instructions"]
    assert contract == old_contract
    assert original_instructions.count(example) == 1
    if refinement:
        assert strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE not in original_instructions
        assert "Shape-only v3 example" in original_instructions
        assert "revise_candidate_id, base_metadata_sha256" in original_instructions
    else:
        assert "Shape-only v2 example" in original_instructions
    assert original_instructions.count(strategy.DIRECT_SELECTION_RECOVERY_RULES) == 1
    assert "exactly SIX top-level keys" in original_instructions
    assert "There is no top-level reuse array or kind/name reuse form" in original_instructions
    assert "explicitly authorized v3 refinement" in original_instructions
    assert "Shape-only" in original_instructions and "never copy its values" in original_instructions


@pytest.mark.parametrize("refinement", [False, True])
def test_selection_shape_example_passes_the_real_validator_and_restores_exact_authority(refinement):
    items, value, authority = _selection_example(refinement=refinement)
    assert set(value) == {"schema_version", "input_ids", "skills", "knowledge", "deferred", "resolved"}
    assert set(value["skills"][0]) == {
        "name", "task", "trigger", "inputs", "outputs", "boundary", "rationale", "derived_from",
    }
    assert set(value["skills"][1]) == {"reuse_candidate_id", "reason", "derived_from"}
    assert value["schema_version"] == (planning.SELECTION_SCHEMA_V3 if refinement else planning.SELECTION_SCHEMA_V2)
    if refinement:
        assert authority["candidate-existing-method"]["refinement_allowed"] is True
        assert authority["candidate-existing-method"]["refinement_item_ids"] == [items[1]["item_id"]]
    original, known = copy.deepcopy(value), copy.deepcopy(authority)
    checked = planning.validate_selection(
        value, items=items, input_ids=value["input_ids"], known_candidates=authority,
        refinement_enabled=refinement,
    )
    assert value == original and authority == known
    assert checked["skills"][1]["name"] == "existing-method"
    for key in ("task", "trigger", "inputs", "outputs", "boundary", "rationale"):
        assert checked["skills"][1][key] == authority["candidate-existing-method"][key]
    assert {identity for row in checked["skills"] for identity in row["derived_from"]} == {
        item["item_id"] for item in items
    }


@pytest.mark.parametrize("defect", ["top-level-reuse", "kind-name", "copied-authority", "missing-item"])
def test_examples_do_not_authorize_invalid_shapes_canonical_rewrites_or_missing_dispositions(defect):
    items, value, authority = _selection_example()
    if defect == "top-level-reuse":
        value["reuse"] = [value["skills"].pop()]
    elif defect == "kind-name":
        value["skills"][1] = {"kind": "reuse", "name": "existing-method",
                              "derived_from": value["skills"][1]["derived_from"]}
    elif defect == "copied-authority":
        value["skills"][1]["task"] = "A narrower task cannot rewrite its authority."
    else:
        value["skills"].pop()
    with pytest.raises(ValueError):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"], known_candidates=authority)


def test_extract_clarity_targets_supplied_text_and_distinguishes_native_envelope_from_index_content():
    old = embedded_contract(strategy.render_extract_batch_compiler_v3(SKILL_BODY))
    fresh = embedded_contract(strategy.render_input_clarity_extract_batch_compiler_v3(SKILL_BODY))
    instructions = fresh["synthesize"]["instructions"]
    assert instructions == old["synthesize"]["instructions"] + "\n\n" + strategy.INPUT_CLARITY_EXTRACT_GUIDANCE
    assert instructions.count(strategy.EXTRACT_ROUTING_RULES_V3) == 1
    assert instructions.count(strategy.INPUT_CLARITY_INDEX_EXAMPLE) == 1
    assert "ALREADY in supplied inputs[].payload.text" in instructions
    assert "same URI in start_char order" in instructions
    assert "Never call read_skill_resource for relative packet or output names" in instructions
    assert "schema/protocol labels, or guessed README/template files" in instructions
    assert "Explicit separate attachments remain governed by existing policy" in instructions
    assert "ONE FileResponse.files row" in instructions and "serialized as a JSON string" in instructions
    assert "THIS file Map" in instructions and "exact TE packet.input_ids, never native IDs" in instructions
    assert "empty arrays and false are not default decisions" in instructions


def test_complete_index_example_matches_exact_v2_fields_and_row_shapes():
    value = _substitute(json.loads(strategy.INPUT_CLARITY_INDEX_EXAMPLE), {
        "<exact TE packet.input_ids entry A>": "chunk-a",
        "<exact TE packet.input_ids entry B>": "chunk-b",
        "<exact TE packet.input_ids entry C>": "chunk-c",
        "<supported method>": "Carry out the provided procedure.",
        "<supported applicability and limits>": "Only within the supplied conditions.",
        "<exact supporting allowed handle A>": "alias-a",
        "<supported fact>": "The assigned text describes a reference entry.",
        "<exact supporting allowed handle B>": "alias-b",
        "<genuine execution gap in assigned Evidence>": "A named external attachment is absent in this fixture.",
        "<exact supporting allowed handle C>": "alias-c",
    })
    assert set(value) == {
        "schema_version", "input_ids", "methods", "facts", "deferred", "context_refs", "context_uncertain",
    }
    assert set(value["methods"][0]) == {"task", "boundary", "source_refs"}
    assert set(value["facts"][0]) == {"text", "source_refs"}
    assert set(value["deferred"][0]) == {"reason", "source_refs"}
    ids, refs = value["input_ids"], ["alias-a", "alias-b", "alias-c"]
    original = copy.deepcopy(value)
    assert planning.validate_index(value, input_ids=ids, source_refs=refs, routing=True) == original
    optional_header = copy.deepcopy(value)
    optional_header.pop("input_ids")
    canonical = _canonical_extract_references(
        optional_header, input_ids=ids, source_refs=refs, reference_map=dict(zip(refs, ids)),
    )
    assert planning.validate_index(canonical, input_ids=ids, source_refs=refs, routing=True) == original
    value["deferred"][0]["text"] = value["deferred"][0].pop("reason")
    with pytest.raises(ValueError, match="Invalid deferred"):
        planning.validate_index(value, input_ids=ids, source_refs=refs, routing=True)


@pytest.mark.parametrize("refinement", [False, True])
def test_reference_data_requires_source_taught_procedure_before_becoming_a_candidate(refinement):
    extract = embedded_contract(strategy.render_input_clarity_extract_batch_compiler_v3(SKILL_BODY))
    instructions = extract["synthesize"]["instructions"]
    assert "A method must be taught by the source" in instructions
    assert "do not invent a workflow to create, maintain or validate those records" in instructions
    assert "Preserve it in facts with its original scope" in instructions
    assert "procedures qualify only when the source actually supplies their steps or decision rules" in instructions
    plan = embedded_contract(strategy.render_compact_aliased_workflow_compiler(SKILL_BODY, refinement=refinement))
    instructions = plan["synthesize"]["instructions"]
    assert (
        "Static records or configuration fields without a source-taught procedure also become knowledge" in instructions
    )
    assert "never invent maintenance, validation or other actions to make them executable" in instructions


@pytest.mark.parametrize("refinement", [False, True])
def test_fresh_guidance_keeps_the_public_plan_and_actual_complete_packet_envelope_bounded(refinement):
    extract = strategy.render_input_clarity_extract_batch_compiler_v3(SKILL_BODY)
    plan = strategy.render_local_recovery_workflow_compiler(SKILL_BODY, refinement=refinement)
    text = ('Unicode 中文 “quotes” \\ and line breaks\n' * 1000)
    chunks = planning.source_chunks(text, "viking://resources/fixture/original.md", max_chars=4096)
    groups = planning.pack_chunks(chunks, max_chunks=3)
    for body in (extract, plan):
        program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
        assert program == strategy.MULTI_COMPILE_PLAN
        assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
        contract = embedded_contract(body)
        assert contract["synthesize"]["input_unit"] == "file"
        assert contract["synthesize"]["execution"] == "direct"
        assert contract["synthesize"]["output"] == contract["output_format"] == "files"
        for group in groups:
            packet = planning.build_stage_packet("extract", "index.json", evidence_chunks=group)
            packet_text = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
            assert len(_compiler_package(body)[1]) + len(packet_text) + 3072 + 2000 <= 48000
    # Larger selection windows must use the measured published-body budget, not assume 32K always fits.
    published_chars = len(_compiler_package(plan)[1])
    limit = min(32000, 48000 - published_chars - 3072)
    assert limit < 32000
    assert published_chars + limit + 3072 <= 48000
    with pytest.raises(ValueError, match="bounded input budget"):
        planning.build_stage_packet("plan", "plan.json", controls={"note": "x" * limit}, max_chars=limit)


def test_renderers_do_not_mutate_shared_policy_and_retain_twelve_case_gate():
    renderers = {
        "extract": strategy.render_extract_batch_compiler_v3,
        "plan": strategy.render_recovery_workflow_compiler,
        "refinement": lambda body: strategy.render_recovery_workflow_compiler(body, refinement=True),
    }
    before = {name: renderer(SKILL_BODY) for name, renderer in renderers.items()}
    old_cases = strategy.render_case_batch_compiler(SKILL_BODY)
    old_contract = copy.deepcopy(strategy.multi_compile_contract())
    strategy.render_input_clarity_extract_batch_compiler_v3(SKILL_BODY)
    strategy.render_local_recovery_workflow_compiler(SKILL_BODY)
    strategy.render_local_recovery_workflow_compiler(SKILL_BODY, refinement=True)
    assert {name: renderer(SKILL_BODY) for name, renderer in renderers.items()} == before
    assert strategy.multi_compile_contract() == old_contract
    assert strategy.render_case_batch_compiler(SKILL_BODY) == old_cases
    assert len(planning.CASE_DIMENSIONS) == 12
    cases = build_document("fixture-method", [
        {"case_id": f"fixture-method-case-{index:02d}", "query": f"Distinct fixture request {index}.",
         "source_session_ids": ["viking://resources/fixture/original.md"]}
        for index in range(1, 13)
    ])["cases"]
    document = planning.assemble_dataset(cases, skill_name="fixture-method", task="Fixture task", identity="fixture")
    assert len(document["cases"]) == 12
    with pytest.raises(ValueError, match="twelve complete"):
        planning.assemble_dataset(cases[:11], skill_name="fixture-method", task="Fixture task", identity="fixture")


def test_fresh_wrappers_preserve_custom_business_policy_once_and_do_not_inherit_other_stages():
    common = "Preserve exact Unicode “quotes” and literal backslash\\ in original citations."
    stages = {"plan": "Explicit plan policy.", "selection_refinement": "Explicit refinement policy.",
              "index": "Explicit index policy.", "extract": "Explicit extraction policy.",
              "extract_batch": "Explicit batch policy.", "case": "Unrelated case policy."}
    base = "---\nname: custom\ndescription: Explicit policy.\n---\n\n" + strategy.mining_policy_block(common, stages)
    for renderer, expected_stages in (
        (lambda body: strategy.render_local_recovery_workflow_compiler(body, refinement=True),
         {"plan", "selection_refinement"}),
        (strategy.render_input_clarity_extract_batch_compiler_v3, {"index", "extract", "extract_batch"}),
    ):
        instructions = embedded_contract(renderer(base))["synthesize"]["instructions"]
        assert instructions.count(common) == 1
        for key, rule in stages.items():
            assert instructions.count(rule) == (1 if key in expected_stages else 0)


@pytest.mark.parametrize("refinement", [0, 1, "true", None])
def test_local_wrapper_keeps_strict_refinement_type(refinement):
    with pytest.raises(ValueError, match="boolean"):
        strategy.render_local_recovery_workflow_compiler(SKILL_BODY, refinement=refinement)
