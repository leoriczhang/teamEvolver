"""Fresh efficient compilers remain ordinary public Map/Finalize Skills, preserving frozen old bodies."""

import ast
import hashlib
import json

import pytest

from team_miner import compile_strategy as strategy
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_round_orchestration import _compiler_package
from team_miner.tests.test_compile_strategy import embedded_contract
from team_miner.workflow_planning import WORKFLOW_PLANNING_RULE


def test_extract_batch_is_existing_per_file_map_finalize_with_routing_and_workflow_policy():
    body = strategy.render_extract_batch_compiler(SKILL_BODY)
    contract = embedded_contract(body)
    code = body.split("```python\n")[1].split("\n```")[0]
    nodes = ast.parse(code).body
    assert [node.value.func.attr for node in nodes] == ["map", "finalize"]
    assert nodes[0].value.args[0].id == "sources"
    assert nodes[0].value.keywords[0].value.attr == "synthesize"
    assert contract["synthesize"]["input_unit"] == "file"
    assert contract["synthesize"]["execution"] == "direct"
    assert contract["synthesize"]["output"] == contract["output_format"] == "files"
    instructions = contract["synthesize"]["instructions"]
    assert "at most 8 independent" in instructions
    assert "skill-miner.index.v2" in instructions
    assert "context_refs" in instructions and "context_uncertain" in instructions
    assert "precise supporting assigned ranges" in instructions
    assert "not the entire packet by default" in instructions
    assert "Each Map produces one complete index at THAT packet.output_path" in instructions
    assert "same file Map only" in instructions
    assert "Partial native output is not full batch success" in instructions
    assert "one TE-owned bounded packet file" not in instructions
    assert strategy.DEFAULT_MINING_POLICY in instructions
    # Extraction must preserve source-supported methods; only merge/selection chooses candidate granularity.
    assert WORKFLOW_PLANNING_RULE not in instructions
    for extension in ("p.expand", "input_limits", "record_limits", "ov-compile-plan-v1", "evidence_ref"):
        assert extension not in body


@pytest.mark.parametrize("stage", ["extract", "merge_index", "plan", "skill_section", "evaluation", "case"])
def test_new_stage_policy_does_not_mutate_original_compiler_or_legacy_renderers(stage):
    frozen = {name: strategy.stage_compiler_body(SKILL_BODY, name) for name in strategy.STAGE_RULES}
    parent = SKILL_BODY.encode()
    rendered = strategy.render_efficient_stage_compiler(SKILL_BODY, stage)
    instructions = embedded_contract(rendered)["synthesize"]["instructions"]
    if stage in {"merge_index", "plan"}:
        assert WORKFLOW_PLANNING_RULE in instructions
    else:
        assert WORKFLOW_PLANNING_RULE not in instructions
    assert {name: strategy.stage_compiler_body(SKILL_BODY, name) for name in strategy.STAGE_RULES} == frozen
    assert SKILL_BODY.encode() == parent
    published = _compiler_package(rendered)[1]
    assert len(published) + 32000 + 3072 + 2000 <= 48000
    legacy = frozen["index" if stage == "extract" else stage]
    if stage in {"merge_index", "plan"}:
        assert hashlib.sha256(rendered.encode()).hexdigest() != hashlib.sha256(legacy.encode()).hexdigest()
    elif stage == "extract":
        assert rendered == strategy.render_extract_batch_compiler(SKILL_BODY)
    else:
        # Candidate-size guidance is intentionally not repeated in file-generation prompts.
        assert rendered == legacy


def test_custom_common_and_current_extract_rules_survive_once_without_unrelated_stage_rules():
    common = "规则原文：请求变量按文档判断；反斜线\\和换行\n逐字保留。"
    rules = {"index": "索引保留局部限制。", "extract": "抽取保留跨段例外。",
             "extract_batch": "批次仍按包隔离。", "case": "只有案例才需要这一条。"}
    base = "---\nname: custom-extraction\ndescription: Custom policy\n---\n\n" + strategy.mining_policy_block(
        common, rules
    )
    instructions = embedded_contract(strategy.render_extract_batch_compiler(base))["synthesize"]["instructions"]
    assert instructions.count(common) == 1
    for name in ("index", "extract", "extract_batch"):
        assert instructions.count(rules[name]) == 1
    assert rules["case"] not in instructions


def test_refinement_keeps_source_authority_and_explicit_revision_protocol():
    body = strategy.render_efficient_stage_compiler(SKILL_BODY, "plan", refinement=True)
    instructions = embedded_contract(body)["synthesize"]["instructions"]
    assert "skill-miner.selection.v3" in instructions
    assert "base_metadata_sha256" in instructions
    assert WORKFLOW_PLANNING_RULE in instructions
    assert "Previous Skill Candidates" in instructions


@pytest.mark.parametrize("count", [0, 9, True, 2.0])
def test_batch_count_must_remain_an_explicit_bounded_integer(count):
    with pytest.raises(ValueError):
        strategy.render_extract_batch_compiler(SKILL_BODY, count)


def test_freeform_custom_policy_is_rejected_instead_of_silently_deleting_business_rules():
    with pytest.raises(ValueError, match="explicit te-mining-policy"):
        strategy.render_extract_batch_compiler("---\nname: custom\n---\nBusiness rules cannot disappear.")
    with pytest.raises(ValueError):
        strategy.render_efficient_stage_compiler(SKILL_BODY, "unknown")
    with pytest.raises(ValueError):
        strategy.render_efficient_stage_compiler(SKILL_BODY, "case", refinement=True)


def test_fresh_renderer_does_not_rewrite_shared_contract_dictionary():
    original = json.dumps(strategy.multi_compile_contract(), sort_keys=True)
    strategy.render_extract_batch_compiler(SKILL_BODY)
    strategy.render_efficient_stage_compiler(SKILL_BODY, "plan")
    assert json.dumps(strategy.multi_compile_contract(), sort_keys=True) == original
