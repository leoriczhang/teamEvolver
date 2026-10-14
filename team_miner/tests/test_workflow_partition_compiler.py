"""Contract regression checks for the pure grouping/metadata compiler split."""

from __future__ import annotations

import ast
import copy
import json

import pytest

from team_miner import compile_strategy as strategy
from team_miner.output_language import OUTPUT_LANGUAGE_GUIDANCE
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.workflow_partition_compiler import (
    METADATA_SCHEMA,
    PARTITION_CAPACITY_SCHEMA,
    PARTITION_SCHEMA,
    render_workflow_metadata_compiler,
    render_workflow_partition_compiler,
)


def _contract(body: str) -> dict:
    return json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])


def _instructions(renderer, base=SKILL_BODY):
    return _contract(renderer(base))["synthesize"]["instructions"]


@pytest.mark.parametrize("renderer", [render_workflow_partition_compiler, render_workflow_metadata_compiler])
def test_renderer_uses_public_bounded_file_map_and_does_not_mutate_shared_contract(renderer):
    before = copy.deepcopy(strategy.multi_compile_contract())
    before_rules = copy.deepcopy(strategy.STAGE_RULES)
    body = renderer(SKILL_BODY)
    program = body.split("```python\n", 1)[1].split("\n```", 1)[0]
    assert program == strategy.MULTI_COMPILE_PLAN
    assert [node.value.func.attr for node in ast.parse(program).body] == ["map", "finalize"]
    contract = _contract(body)
    assert set(contract) == {"extract", "synthesize", "output_format"}
    assert contract["synthesize"]["input_unit"] == "file"
    assert contract["synthesize"]["execution"] == "direct"
    assert contract["synthesize"]["output"] == contract["output_format"] == "files"
    text = contract["synthesize"]["instructions"]
    assert "sole output is packet.output_path" in text
    assert "12000 characters" in text
    assert "inputs[].payload.text" in text
    assert text.count(strategy.DEFAULT_MINING_POLICY) == text.count(OUTPUT_LANGUAGE_GUIDANCE) == 1
    assert all(rule not in text for rule in strategy.STAGE_RULES.values())
    assert strategy.multi_compile_contract() == before
    assert strategy.STAGE_RULES == before_rules
    assert renderer(SKILL_BODY) == body


@pytest.mark.parametrize("renderer", [render_workflow_partition_compiler, render_workflow_metadata_compiler])
def test_default_published_compiler_leaves_room_for_full_packet_and_request_reserve(renderer):
    from team_miner.ov_staged_orchestration import _package

    # Integration still measures each frozen custom policy and instruction; a
    # small default renderer must not consume the existing transport envelope.
    _, published = _package(renderer(SKILL_BODY))
    assert len(published) + 32000 + 3072 <= 48000


def test_partition_has_exact_normal_and_capacity_contracts_without_metadata_fields():
    text = _instructions(render_workflow_partition_compiler)
    assert PARTITION_SCHEMA in text and PARTITION_CAPACITY_SCHEMA in text
    for requirement in (
        "schema_version, assignment_sha256, rows, reasons",
        "[method_id,destination]两个字符串",
        "assigned_ids原顺序完整复制",
        "选为父方法也必须显式映射自己",
        "禁止父指向别的父的链或环",
        "单独映射自己的独立方法没有reason键",
        "恰好出现一次",
        "正常结果不含input_ids",
        "assignment_sha256原样复制derived_controls.assignment_sha256",
        "禁止计算或猜测",
        "不含任何部分rows/reasons或其它处置",
        "没有固定24方法或8候选上限",
        "derived_controls.limits.max_output_chars",
        "derived_controls.limits.reason_max_chars（200字符）",
        "derived_controls.method_columns",
        "method_rows每行严格按这些列读取",
        "source_relationships.scope_columns",
        "scope_rows按列保存每个scope_id的完整来源短ID集合",
    ):
        assert requirement in text
    assert "known_candidates" not in text and "reuse_candidate_id" not in text
    assert "derived_controls.methods" not in text


def test_partition_preserves_independent_phases_and_requires_supported_parent_containment():
    text = _instructions(render_workflow_partition_compiler)
    for requirement in (
        "包括后出现的方法",
        "不用目标数量",
        "强制适用边界已经覆盖它们",
        "父流程之外调用方式",
        "保留子方法为独立项",
        "不同完整阶段若有独立需要的终态结果",
        "不能因共享旅程、参与者或最终大目标吞并成超级流程",
        "不能因出现得早选狭窄父方法",
        "不能扩大父方法边界",
        "不能指望正文生成时修补错误包含关系",
        "静态描述、名称、配置字段、查找事实归knowledge",
        "不得发明维护、校验、决策过程把事实变成方法",
        "来源关系不是Evidence",
        "同主题不证明方法等价",
        "完整原记录、来源URI、范围、SHA与成员谱系",
        "所有成员来源通过精确ID保留",
        "容量不足不是Evidence缺口",
    ):
        assert requirement in text


def test_metadata_generates_one_candidate_or_explicit_deferral_without_model_lineage():
    text = _instructions(render_workflow_metadata_compiler)
    assert METADATA_SCHEMA in text
    for requirement in (
        "一个完整工作流生成一次canonical候选metadata",
        "schema_version,assignment_sha256,candidate",
        "name,task,trigger,inputs,outputs,boundary,rationale七个字段",
        "禁止derived_from、source_refs、member_ids、parent_id、title",
        "宿主恢复全部成员的完整谱系",
        "schema_version,assignment_sha256,deferred",
        "deferred严格只有reason",
        "candidate和deferred互斥",
        "现有parent无法不靠发明来覆盖组",
        "不得把静态事实改写成操作方法",
        "规划包无原文不证明来源缺Evidence",
        "格式错误或输出容量问题不是业务暂缓理由",
        "各非空且最多400字符",
        "各为1至16项非空字符串数组，每项最多240字符",
        "method_columns声明id,task,boundary,scope_id列",
        "method_rows保留全部成员完整task/boundary",
        "scope_id通过source_relationships.scopes映射files/refs短ID",
    ):
        assert requirement in text


def test_language_applies_to_human_text_and_titles_but_not_machine_slug_or_source_quotes():
    text = _instructions(render_workflow_metadata_compiler)
    assert "最多64字符的小写ASCII kebab slug机器标识" in text
    assert "不是展示标题" in text
    assert "zh-CN须简体中文，包括task及后续生成的展示标题和小节标题" in text
    assert "机器键、slug、schema、原文直接引述、数值和单位保持原样" in text
    assert "不能以英文task代替中文标题" in text
    assert "packet.output_language_policy；zh-CN用简体中文" in _instructions(render_workflow_partition_compiler)


def test_journey_or_audience_templates_do_not_add_a_duplicate_cross_stage_skill():
    partition = _instructions(render_workflow_partition_compiler)
    metadata = _instructions(render_workflow_metadata_compiler)
    assert "旅程、课纲或人群模板若只排列已独立支持的阶段" in partition
    assert "归knowledge组织上下文；不以人物或人群参数另建跨阶段候选" in partition
    assert "没有独立的跨阶段协调决策、交接约束及必要终态结果，应明确deferred" in metadata
    assert "不得以人物或人群参数额外生成跨阶段Skill" in metadata
    assert "来源确实支持上述协调方法时可独立保留" in partition
    assert "真正有来源支持的协调方法仍可保留" in metadata


@pytest.mark.parametrize(
    ("renderer", "stage", "inherited"),
    [
        (render_workflow_partition_compiler, "workflow_partition",
         ("merge_index", "merge_batch", "direct_workflow", "plan")),
        (render_workflow_metadata_compiler, "workflow_metadata", ("direct_workflow", "plan")),
    ],
)
def test_complete_custom_business_policy_survives_once_without_unrelated_stage_rules(renderer, stage, inherited):
    common = "  通用条件与来源边界必须原样核对。\n"
    all_stages = {
        key: f"  {key} 自定义业务约束。\n"
        for key in (
            "merge_index", "merge_batch", "direct_workflow", "plan", "workflow_partition", "workflow_metadata", "case",
        )
    }
    base = strategy.mining_policy_block(common, all_stages)
    text = _instructions(renderer, base)
    assert text.count(common) == 1
    for key, rule in all_stages.items():
        assert text.count(rule) == (1 if key == stage or key in inherited else 0)
    assert base == strategy.mining_policy_block(common, all_stages)


@pytest.mark.parametrize("renderer", [render_workflow_partition_compiler, render_workflow_metadata_compiler])
@pytest.mark.parametrize("base", ["", "Unclassified custom instructions must not silently disappear."])
def test_unknown_custom_policy_is_rejected_instead_of_silently_replaced(renderer, base):
    with pytest.raises(ValueError):
        renderer(base)
