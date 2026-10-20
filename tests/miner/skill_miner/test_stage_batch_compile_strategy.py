"""Independent artifact batches preserve public Compile and custom business policy."""

import ast
import json

import pytest

from team_miner.skill_miner.compile_strategy import (
    MULTI_COMPILE_PLAN,
    STAGE_RULES,
    mining_policy_block,
    render_stage_batch_compiler,
    stage_compiler_body,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY


@pytest.mark.parametrize("stage", ["skill_section", "evaluation"])
def test_batch_uses_independent_public_file_assignments_with_the_actual_stage_policy(stage):
    single_before = stage_compiler_body(SKILL_BODY, stage)
    common = "Do not infer insurance decisions from communication training."
    specific = "Keep incomplete business facts as explicitly unresolved Evidence."
    base = "---\nname: custom\ndescription: Custom policy.\n---\n\n" + mining_policy_block(common, {stage: specific})
    body = render_stage_batch_compiler(base, stage, 2)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    contract = json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert program == MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
    assert contract["synthesize"]["input_unit"] == "file"
    instructions = contract["synthesize"]["instructions"]
    assert instructions.count(common) == instructions.count(specific) == 1
    assert STAGE_RULES[stage] in instructions
    assert "one to 2 independent" in instructions
    assert "Never read, merge, cite or emit a sibling packet" in instructions
    assert "12000 characters per output file" in instructions
    assert "only missing or invalid files" in instructions
    assert "packet.output_path" in instructions
    assert stage_compiler_body(SKILL_BODY, stage) == single_before


@pytest.mark.parametrize("size", [0, 9, True, "2"])
def test_batch_rejects_invalid_parallel_work_bounds(size):
    with pytest.raises(ValueError):
        render_stage_batch_compiler(SKILL_BODY, "skill_section", size)


@pytest.mark.parametrize("stage", ["plan", "case", "unknown", None, []])
def test_batch_does_not_accept_dependent_planning_or_case_assignments(stage):
    with pytest.raises(ValueError):
        render_stage_batch_compiler(SKILL_BODY, stage)
