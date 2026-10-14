"""Future R2 metadata corrections retain identity through the original OV file Map."""

import ast
import hashlib
import json

import pytest

from team_miner import compile_strategy as strategy
from team_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE, output_language_instruction
from team_miner.ov_compile_mining import LEGACY_SKILL_BODY, SKILL_BODY


def embedded_contract(body):
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])


def instructions(body):
    return embedded_contract(body)["synthesize"]["instructions"]


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def test_refinement_is_one_bounded_selection_file_using_only_public_map_and_finalize():
    body = strategy.render_refinement_plan_compiler(SKILL_BODY)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    contract = embedded_contract(body)
    assert program == strategy.MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
    assert set(contract) == {"extract", "synthesize", "output_format"}
    assert contract["synthesize"]["input_unit"] == "file"
    assert contract["synthesize"]["execution"] == "direct"
    assert contract["synthesize"]["output"] == contract["output_format"] == "files"
    text = instructions(body)
    assert "packet.stage=plan" in text
    assert "derived_controls.selection_schema is skill-miner.selection.v3" in text
    assert "sole output is packet.output_path" in text
    assert "at most 12000 characters" in text
    assert "compiled_skill/" not in body
    assert "p.reduce" not in body and "p.expand" not in body
    assert text.count(strategy.DEFAULT_MINING_POLICY) == 1
    assert text.count(OUTPUT_LANGUAGE_GUIDANCE) == 1
    assert all(value not in text for value in strategy.STAGE_RULES.values())


def test_default_published_body_full_packet_language_and_repair_reserve_fit_input_envelope():
    from team_miner.ov_staged_orchestration import _package

    _, published = _package(strategy.render_refinement_plan_compiler(SKILL_BODY))
    path = "selection.json"
    request_instruction = (
        f"本次只执行 plan 阶段；唯一相对输出路径是 {path}，禁止生成其它文件。"
        f"required_paths 如使用只能包含实际路径 {path}；packet.output_path 是字段引用，不能作为字面文件名。"
        "原文仅 packet.evidence；derived_controls 和旧候选是非事实控制上下文。"
        + output_language_instruction({"version": 1, "basis": "frozen_original_source", "language": "zh-CN"})
    )
    # The runner still measures custom policy and revision projections dynamically;
    # this checks the default can accommodate a maximum packet before adding them.
    assert len(published) + 32000 + len(request_instruction) + 3072 <= 48000


def test_refinement_requires_exact_authorized_identity_and_base_hash_without_identity_changes():
    text = instructions(strategy.render_refinement_plan_compiler(SKILL_BODY))
    assert "exactly derived_from, revise_candidate_id, base_metadata_sha256, " in text
    assert "task, trigger, inputs, outputs, boundary, rationale and reason" in text
    assert "known candidate ID whose refinement_allowed is true" in text
    assert "same known candidate's exact base_metadata_sha256" in text
    assert "same candidate's exact refinement_item_ids" in text
    assert "never compute, guess or copy a different candidate's hash" in text
    assert "preserves the known candidate's name, identity and existing case IDs" in text
    assert "Do not mix these forms" in text
    assert "never silent fallback to v2" in text


def test_revision_metadata_is_not_evidence_and_only_same_currently_supported_workflow_can_change():
    text = instructions(strategy.render_refinement_plan_compiler(SKILL_BODY))
    assert "CURRENT assigned method proposals and their original-source lineage" in text
    assert "SAME complete independently executable workflow" in text
    assert "Previous business metadata, previous Skill text and review feedback are NON-Evidence" in text
    assert "do not use old metadata as proof of a correction" in text
    assert "final writers verify original Evidence" in text
    assert "different workflow, propose an independent new candidate" in text
    assert "Every new/reuse/refinement row must consume the exact current assigned method IDs" in text
    assert "every item must be disposed of exactly once" in text
    assert "related_facts and provisional_gaps are readonly context" in text
    assert "resolved must be empty" in text


def test_original_new_candidate_and_explicit_reuse_forms_remain_available():
    text = instructions(strategy.render_refinement_plan_compiler(SKILL_BODY))
    assert "exactly derived_from, name, task, trigger, inputs, outputs, boundary and rationale" in text
    assert "exactly derived_from, reuse_candidate_id and reason" in text
    assert "use reuse only when those fields remain appropriate" in text
    assert "at most eight independently executable workflows in this assignment" in text
    assert "Further method windows are handled by TE" in text
    assert "capacity is not an Evidence gap" in text


def test_custom_common_plan_and_refinement_policy_are_complete_and_present_exactly_once():
    common = "  通用业务规则原样保留。\n精确条件与原始来源必须核实。  "
    plan = "\n  自定义选取规则不得被修订协议吞掉。\n"
    refinement = "  修订仍需遵守此租户的独立业务限制。 "
    parent = (
        "---\nname: custom\ndescription: Explicit custom compiler.\n---\n"
        + strategy.TE_STAGED_COMPILE + "\nskill-miner.plan.v1\n"
        + strategy.mining_policy_block(common, {
            "plan": plan, "selection_refinement": refinement, "case": "Case-only business rule."
        })
    )
    before = digest(parent)
    text = instructions(strategy.render_refinement_plan_compiler(parent))
    for rule in (common, plan, refinement):
        assert text.count(rule) == 1
    assert "Case-only business rule." not in text
    assert digest(parent) == before


def test_rendering_does_not_change_any_existing_parent_stage_batch_or_shared_contract():
    before = {
        "parent": digest(SKILL_BODY),
        "legacy": digest(LEGACY_SKILL_BODY),
        "guidance": digest(strategy.MULTI_EVIDENCE_GUIDANCE),
        "rules": digest(json.dumps(strategy.STAGE_RULES, ensure_ascii=False, sort_keys=True)),
        "batch": digest(strategy.render_case_batch_compiler(SKILL_BODY)),
        **{stage: digest(strategy.stage_compiler_body(SKILL_BODY, stage)) for stage in strategy.STAGE_RULES},
    }
    old_contract = strategy.multi_compile_contract()
    assert strategy.render_refinement_plan_compiler(SKILL_BODY) == (
        strategy.render_refinement_plan_compiler(SKILL_BODY)
    )
    assert strategy.multi_compile_contract() == old_contract
    after = {
        "parent": digest(SKILL_BODY),
        "legacy": digest(LEGACY_SKILL_BODY),
        "guidance": digest(strategy.MULTI_EVIDENCE_GUIDANCE),
        "rules": digest(json.dumps(strategy.STAGE_RULES, ensure_ascii=False, sort_keys=True)),
        "batch": digest(strategy.render_case_batch_compiler(SKILL_BODY)),
        **{stage: digest(strategy.stage_compiler_body(SKILL_BODY, stage)) for stage in strategy.STAGE_RULES},
    }
    assert after == before


def test_exact_owned_pre_policy_default_can_render_future_auxiliary_without_modification():
    old = SKILL_BODY.split("\n## TE mining business policy\n", 1)[0]
    before = digest(old)
    assert strategy.DEFAULT_MINING_POLICY in instructions(strategy.render_refinement_plan_compiler(old))
    assert digest(old) == before


@pytest.mark.parametrize("body", ["", "Use these freeform custom business rules."])
def test_unknown_freeform_policy_cannot_silently_lose_custom_business_rules(body):
    with pytest.raises(ValueError):
        strategy.render_refinement_plan_compiler(body)
