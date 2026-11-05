"""Fresh workflow contracts retain public Compile, strict source indexes and canonical authority."""

import ast
import copy
import json

import pytest

from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.bounded_compile_plan import INDEX_SCHEMA_V2, validate_index
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from tests.miner.skill_miner.support.compile_strategy import embedded_contract


@pytest.mark.parametrize("renderer", [
    strategy.render_extract_batch_compiler_v3,
    strategy.render_workflow_identity_compiler,
])
def test_fresh_rules_use_existing_file_map_finalize_and_keep_the_complete_envelope_bounded(renderer):
    body = renderer(SKILL_BODY)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    assert program == strategy.MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
    contract = embedded_contract(body)
    assert contract["synthesize"]["input_unit"] == "file"
    assert contract["synthesize"]["execution"] == "direct"
    assert contract["output_format"] == contract["synthesize"]["output"] == "files"
    published = _compiler_package(body)[1]
    assert len(published) + 32000 + 3072 + 2000 <= 48000
    for unsupported in ("p.expand", "input_limits", "record_limits", "ov-compile-plan-v1", "evidence_ref"):
        assert unsupported not in body


def test_extraction_contract_places_full_workflow_and_local_constraint_rules_in_actual_map_instructions():
    instructions = embedded_contract(strategy.render_extract_batch_compiler_v3(SKILL_BODY))[
        "synthesize"
    ]["instructions"]
    assert instructions.count(strategy.EXTRACT_ROUTING_RULES_V3) == 1
    assert "skill-miner.index.v2" in instructions
    assert "skill-miner.index.v3" not in instructions
    assert "context_refs, context_uncertain" in instructions
    assert "emit one method describing that complete workflow" in instructions
    assert "ALL assigned branch ranges in method source_refs" in instructions
    assert "Do not invent a parent workflow or its missing steps across windows" in instructions
    assert "Capacity, schema mistakes" in instructions
    assert "does not declare a corpus-wide rule" in instructions
    assert "more than one supported method here" in instructions
    assert "with no actual shared constraint use context_refs=[]" in instructions
    assert "With no unresolved constraint use false" in instructions
    assert "same file Map only" in instructions
    assert "never silently choose one interpretation or fix an example's arithmetic" in instructions


def test_exact_index_v2_can_keep_all_parameter_branches_without_declaring_examples_shared():
    refs = ["scope-original", "scope-alternate", "scope-constraint"]
    value = {
        "schema_version": INDEX_SCHEMA_V2, "input_ids": ["chunk-1", "chunk-2", "chunk-3"],
        "methods": [
            {"task": "Process an eligible refund, selecting the source-supported payment branch.",
             "boundary": "Verify the request, determine eligibility and record the refund outcome.",
             "source_refs": refs},
        ],
        "facts": [{"text": "The example channels are invocation parameters, not required user identities.",
                   "source_refs": [refs[1]]}],
        "deferred": [], "context_refs": [], "context_uncertain": False,
    }
    raw = copy.deepcopy(value)
    assert validate_index(value, input_ids=value["input_ids"], source_refs=refs, routing=True) == raw
    assert value == raw


def test_resolved_shared_constraint_and_partial_method_do_not_require_unknown_scope_or_new_fields():
    refs = ["scope-refund", "scope-exchange", "scope-identity"]
    value = {
        "schema_version": INDEX_SCHEMA_V2, "input_ids": ["chunk-1", "chunk-2", "chunk-3"],
        "methods": [
            {"task": "Process an eligible refund.", "boundary": "Issue and record the refund result.",
             "source_refs": [refs[0], refs[2]]},
            {"task": "Process an eligible exchange.", "boundary": "Confirm and record the replacement.",
             "source_refs": [refs[1], refs[2]]},
        ],
        "facts": [{"text": "Verify the requester's receipt for either operation.", "source_refs": [refs[2]]}],
        "deferred": [], "context_refs": [refs[2]], "context_uncertain": False,
    }
    assert validate_index(value, input_ids=value["input_ids"], source_refs=refs, routing=True) == value
    value["context_refs"] = ["foreign-source"]
    with pytest.raises(ValueError, match="exact assigned context"):
        validate_index(value, input_ids=value["input_ids"], source_refs=refs, routing=True)


def test_separate_rule_document_can_mark_unowned_constraints_without_inventing_a_method():
    instructions = embedded_contract(strategy.render_extract_batch_compiler_v3(SKILL_BODY))[
        "synthesize"
    ]["instructions"]
    assert "For a facts-only unit, mark actual mandatory or overriding rules" in instructions
    assert "TE retains unowned rules for every candidate" in instructions
    value = {
        "schema_version": INDEX_SCHEMA_V2, "input_ids": ["chunk-rule"], "methods": [],
        "facts": [{"text": "Obtain authorization before either documented operation.", "source_refs": ["rule"]}],
        "deferred": [], "context_refs": ["rule"], "context_uncertain": False,
    }
    assert validate_index(value, input_ids=value["input_ids"], source_refs=["rule"], routing=True) == value


def test_identity_contract_uses_explicit_complete_existing_survivor_without_broadening_canonical_metadata():
    instructions = embedded_contract(strategy.render_workflow_identity_compiler(SKILL_BODY))[
        "synthesize"
    ]["instructions"]
    assert instructions.count(strategy.WORKFLOW_IDENTITY_RULES) == 1
    assert "skill-miner.identity-groups.v2" in instructions
    assert "skill-miner.identity-groups.v1" not in instructions
    assert '"survivor_candidate_id":"one grouped known ID"' in instructions
    assert "authority_priority=[0 for previous authority else 1, original ordinal]" in instructions
    assert "lexicographically smallest authority_priority" in instructions
    assert "even when it was created later" in instructions
    assert "Extra source coverage cannot widen a narrow canonical task" in instructions
    assert "Two complementary same-level steps are not equivalent" in instructions
    assert "literal differences in task/input/output text" in instructions
    assert "metadata rewrite" in instructions


def test_fresh_renderers_preserve_common_and_applicable_custom_policy_once():
    common = "Mandatory source policy: preserve Unicode – and literal backslash\\ without normalization."
    custom = {"index": "Index requirement.", "extract": "Source requirement.",
              "extract_batch": "Batch requirement.", "identity_group": "Identity requirement.",
              "case": "Unrelated case requirement."}
    base = "---\nname: explicit-policy\ndescription: Custom policy.\n---\n\n" + strategy.mining_policy_block(
        common, custom
    )
    for renderer, expected in (
        (strategy.render_extract_batch_compiler_v3, {"index", "extract", "extract_batch"}),
        (strategy.render_workflow_identity_compiler, {"identity_group"}),
    ):
        instructions = embedded_contract(renderer(base))["synthesize"]["instructions"]
        assert instructions.count(common) == 1
        for stage, rule in custom.items():
            assert instructions.count(rule) == (1 if stage in expected else 0)


def test_rendering_new_protocol_does_not_change_legacy_v2_or_shared_contracts():
    old = strategy.render_extract_batch_compiler(SKILL_BODY)
    legacy = {stage: strategy.stage_compiler_body(SKILL_BODY, stage) for stage in strategy.STAGE_RULES}
    contract = json.dumps(strategy.multi_compile_contract(), ensure_ascii=False, sort_keys=True)
    parent = SKILL_BODY.encode()
    strategy.render_extract_batch_compiler_v3(SKILL_BODY)
    strategy.render_workflow_identity_compiler(SKILL_BODY)
    assert strategy.render_extract_batch_compiler(SKILL_BODY) == old
    assert {stage: strategy.stage_compiler_body(SKILL_BODY, stage) for stage in strategy.STAGE_RULES} == legacy
    assert json.dumps(strategy.multi_compile_contract(), ensure_ascii=False, sort_keys=True) == contract
    assert SKILL_BODY.encode() == parent


@pytest.mark.parametrize("max_packets", [0, 9, True, "4", 2.0])
def test_source_batches_keep_the_explicit_public_four_packet_bound(max_packets):
    with pytest.raises(ValueError):
        strategy.render_extract_batch_compiler_v3(SKILL_BODY, max_packets)


@pytest.mark.parametrize("renderer", [
    strategy.render_extract_batch_compiler_v3,
    strategy.render_workflow_identity_compiler,
])
def test_custom_freeform_business_policy_cannot_silently_disappear(renderer):
    with pytest.raises(ValueError, match="explicit te-mining-policy"):
        renderer("---\nname: custom\n---\nBusiness rules must be classified explicitly.")
