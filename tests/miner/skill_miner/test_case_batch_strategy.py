"""Case batches preserve the existing OV file-Map contract and frozen single-case compilers."""

import ast
import hashlib
import json

import pytest

from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY


def embedded_contract(body):
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])


@pytest.mark.parametrize("maximum", [1, 4])
def test_only_existing_file_map_and_finalize_is_used_for_independent_case_packets(maximum):
    body = strategy.render_case_batch_compiler(SKILL_BODY, maximum)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    contract = embedded_contract(body)
    assert program == strategy.MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
    assert set(contract) == {"extract", "synthesize", "output_format"}
    assert contract["synthesize"]["execution"] == "direct"
    assert contract["synthesize"]["input_unit"] == "file"
    assert contract["synthesize"]["output"] == contract["output_format"] == "files"
    instructions = contract["synthesize"]["instructions"]
    assert f"one to {maximum} distinct TE-owned bounded packet file URIs" in instructions
    assert "Each packet.stage is case" in instructions
    assert "only its own packet file" in instructions
    assert "exactly one FileResponse.files entry at its own packet.output_path" in instructions
    assert "native OV range IDs supplied to THAT file job" in instructions
    assert "Every packet has a different output_path" in instructions
    assert "never generate a complete Test Dataset or merge sibling cases" in instructions
    assert "completed task may have partial outputs" in instructions
    assert "TE validates each expected case independently" in instructions
    assert "case.json" not in body and "compiled_skill/" not in body
    assert "skill-miner.deferred.v1" in instructions
    assert instructions.count(strategy.DEFAULT_MINING_POLICY) == 1
    assert instructions.count(strategy.STAGE_RULES["case"]) == 1
    assert instructions.count(OUTPUT_LANGUAGE_GUIDANCE) == 1
    assert all(value not in instructions for stage, value in strategy.STAGE_RULES.items() if stage != "case")


def test_rendering_never_changes_parent_or_existing_single_stage_bodies_and_contract():
    before = {
        stage: hashlib.sha256(strategy.stage_compiler_body(SKILL_BODY, stage).encode()).hexdigest()
        for stage in strategy.STAGE_RULES
    }
    constants = (SKILL_BODY, LEGACY_SKILL_BODY, strategy.MULTI_EVIDENCE_GUIDANCE)
    contract = strategy.multi_compile_contract()
    strategy.render_case_batch_compiler(SKILL_BODY)
    assert (SKILL_BODY, LEGACY_SKILL_BODY, strategy.MULTI_EVIDENCE_GUIDANCE) == constants
    assert strategy.multi_compile_contract() == contract
    assert before == {
        stage: hashlib.sha256(strategy.stage_compiler_body(SKILL_BODY, stage).encode()).hexdigest()
        for stage in strategy.STAGE_RULES
    }


def test_custom_common_and_case_policy_are_complete_without_other_stage_business_rules():
    common = "  中文业务规则原样保留。\n单位、范围与例外不得改写。  "
    case = "\n 此案例需要保留所有已给业务条件。\n"
    batch = " 批内不同材料不得混作同一案例的来源。 "
    parent = (
        "---\nname: custom\ndescription: Explicit custom compiler.\n---\n"
        + strategy.TE_STAGED_COMPILE + "\nskill-miner.plan.v1\n"
        + strategy.mining_policy_block(common, {"case": case, "case_batch": batch, "index": "Index-only rule."})
    )
    before = hashlib.sha256(parent.encode()).hexdigest()
    instructions = embedded_contract(strategy.render_case_batch_compiler(parent))["synthesize"]["instructions"]
    for rule in (common, case, batch):
        assert instructions.count(rule) == 1
    assert "Index-only rule." not in instructions
    assert hashlib.sha256(parent.encode()).hexdigest() == before


def test_exact_owned_pre_policy_default_can_generate_a_new_batch_auxiliary():
    old = SKILL_BODY.split("\n## TE mining business policy\n", 1)[0]
    frozen = hashlib.sha256(old.encode()).hexdigest()
    instructions = embedded_contract(strategy.render_case_batch_compiler(old))["synthesize"]["instructions"]
    assert strategy.DEFAULT_MINING_POLICY in instructions
    assert hashlib.sha256(old.encode()).hexdigest() == frozen


@pytest.mark.parametrize("maximum", [0, 9, True, None, "4", 1.5])
def test_batch_count_is_a_bounded_integer(maximum):
    with pytest.raises(ValueError, match="one to eight"):
        strategy.render_case_batch_compiler(SKILL_BODY, maximum)
