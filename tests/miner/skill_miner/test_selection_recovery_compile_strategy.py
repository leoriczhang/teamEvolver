"""Fresh recovery instructions clarify ledgers without loosening public Compile or canonical authority."""

import ast
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from tests.miner.skill_miner.support.bounded_compile_plan import candidate, planning_catalog
from tests.miner.skill_miner.support.compile_strategy import embedded_contract


@pytest.mark.parametrize("refinement", [False, True])
def test_recovery_is_one_existing_file_map_assignment_with_the_same_direct_schema_limits(refinement):
    legacy = strategy.render_direct_workflow_compiler(SKILL_BODY, refinement=refinement)
    legacy_contract = embedded_contract(legacy)
    body = strategy.render_recovery_workflow_compiler(SKILL_BODY, refinement=refinement)
    contract = embedded_contract(body)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    assert program == strategy.MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
    assert contract["synthesize"]["input_unit"] == "file"
    assert contract["synthesize"]["execution"] == "direct"
    instructions = contract["synthesize"]["instructions"]
    assert instructions == legacy_contract["synthesize"]["instructions"] + "\n\n" + (
        strategy.DIRECT_SELECTION_RECOVERY_RULES
    )
    assert instructions.count(strategy.DIRECT_WORKFLOW_RULES) == 1
    assert instructions.count(strategy.DIRECT_SELECTION_RECOVERY_RULES) == 1
    assert "at most 24 assigned" in instructions and "Choose 0-8" in instructions
    assert "skill-miner.selection.capacity.v1" in instructions
    assert len(_compiler_package(body)[1]) + 28000 + 3072 + 2000 <= 48000
    assert strategy.render_direct_workflow_compiler(SKILL_BODY, refinement=refinement) == legacy
    for unsupported in ("p.expand", "input_limits", "record_limits", "ov-compile-plan-v1"):
        assert unsupported not in body


def test_actual_prompt_distinguishes_method_dispositions_from_header_and_preserves_source_conditions():
    instructions = embedded_contract(strategy.render_recovery_workflow_compiler(SKILL_BODY))[
        "synthesize"
    ]["instructions"]
    assert "combined derived_from lists" in instructions
    assert "EXACTLY ONCE" in instructions
    assert "input_ids is separate container bookkeeping" in instructions
    assert "{derived_from, reuse_candidate_id, reason}" in instructions
    assert "NO copied name, task, trigger, inputs, outputs, boundary or rationale" in instructions
    assert "narrower same-name task or duplicate child" in instructions
    assert "product/contract applicability and as-of dates" in instructions
    assert "Unknown runtime facts stay unknown" in instructions
    assert "never fixed results for every user" in instructions
    assert "Personal experience is optional and truthful" in instructions


def selection_with_reuse(count=3):
    units, items = planning_catalog(count)
    authority = {k: v for k, v in candidate([]).items() if k != "source_refs"}
    authority["task"] = "Inspect the request, investigate the relevant conditions and record a complete decision."
    identity = "candidate-complete-method"
    value = {
        "schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": [u["id"] for u in units],
        "skills": [{"derived_from": [i["item_id"] for i in items], "reuse_candidate_id": identity,
                    "reason": "The current inspection detail belongs to the assigned complete method."}],
        "knowledge": [], "deferred": [], "resolved": [],
    }
    return items, value, {identity: authority}


def test_nested_steps_reuse_complete_authority_and_keep_all_method_provenance():
    items, raw, known = selection_with_reuse()
    original, authority = copy.deepcopy(raw), copy.deepcopy(known)
    result = planning.validate_selection(raw, items=items, input_ids=raw["input_ids"], known_candidates=known)
    assert raw == original and known == authority
    row = result["skills"][0]
    assert row["task"] == known["candidate-complete-method"]["task"]
    assert row["derived_from"] == [i["item_id"] for i in items]
    assert row["source_refs"] == sorted({ref for i in items for ref in i["source_refs"]})


@pytest.mark.parametrize("failure", ["omitted", "duplicate", "metadata-rewrite"])
def test_complete_header_cannot_hide_item_loss_or_authorize_a_narrow_canonical_rewrite(failure):
    items, value, known = selection_with_reuse()
    if failure == "omitted":
        value["skills"][0]["derived_from"].pop()
    elif failure == "duplicate":
        value["skills"][0]["derived_from"].append(items[0]["item_id"])
    else:
        value["skills"][0]["task"] = "Only inspect the request."
    assert value["input_ids"]
    with pytest.raises(ValueError):
        planning.validate_selection(value, items=items, input_ids=value["input_ids"], known_candidates=known)


def test_applicable_explicit_policy_survives_once_and_shared_contract_stays_immutable():
    common = "Keep source quotations exact; Unicode “quotes” and literal backslash\\ remain unchanged."
    stages = {"plan": "Plan constraint.", "merge_index": "Former merge constraint.",
              "merge_batch": "Former batch constraint.", "direct_workflow": "Direct constraint.",
              "selection_refinement": "Authorized refinement constraint.", "case": "Unrelated case constraint."}
    base = "---\nname: custom-direct\ndescription: Explicit policy.\n---\n\n" + strategy.mining_policy_block(
        common, stages
    )
    shared = json.dumps(strategy.multi_compile_contract(), sort_keys=True)
    instructions = embedded_contract(strategy.render_recovery_workflow_compiler(base, refinement=True))[
        "synthesize"
    ]["instructions"]
    assert instructions.count(common) == 1
    for stage, rule in stages.items():
        assert instructions.count(rule) == (0 if stage == "case" else 1)
    assert json.dumps(strategy.multi_compile_contract(), sort_keys=True) == shared


@pytest.mark.parametrize("refinement", [0, 1, "true", None])
def test_new_wrapper_retains_strict_explicit_refinement_flag(refinement):
    with pytest.raises(ValueError, match="boolean"):
        strategy.render_recovery_workflow_compiler(SKILL_BODY, refinement=refinement)
