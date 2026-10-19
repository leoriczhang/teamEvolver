"""Compiler prompt contract regressions, not a simulation of model compliance.

OV reuses three transforms across five duties and shares the original Skill and
read attachments with its agents. These tests protect the published dispatch and
resource boundaries; a live run must still verify the planner and child records.
"""

import ast
import json
import re

from team_miner.native_compiler import (
    AUXILIARY_FILES,
    EVIDENCE_REVIEW,
    EXTRACT_DISPATCH,
    FILE_SUBMISSION,
    METHOD_PLANNING,
    PLANNING,
    REDUCE_DISPATCH,
    SKILL_BODY,
    SOURCE_OBSERVATION,
    SYNTHESIZE_DISPATCH,
    UNIT_DRAFT,
    UNIT_REVIEW,
    freeze_assets,
    frozen_assets,
    record_format_guidance,
)


def test_planner_receives_both_branches_of_each_reused_record_transform():
    for transform, dispatch in (("extract", EXTRACT_DISPATCH), ("reduce", REDUCE_DISPATCH)):
        heading = f"### contract.{transform}.instructions（完整复制）\n"
        published = SKILL_BODY.split(heading, 1)[1].split("\n### contract.", 1)[0].strip()
        assert published == dispatch
        assert "分支A：" in dispatch and "分支B：" in dispatch
        assert "records" in dispatch and "禁止ready_path/ready_content/ready_content_ref" in dispatch
    assert "逐字完整写入对应Transform.instructions" in SKILL_BODY
    assert "不能只保留第一分支" in PLANNING
    assert "不把全部附件全文或用户整轮文件交付目标复制" in SKILL_BODY
    # Deployed plan is a non-agent call: no attachment reader or shared snapshots.
    assert PLANNING in SKILL_BODY
    assert "Planner不能调用附件读取工具" in SKILL_BODY
    assert "references/planning.md" not in SKILL_BODY


def test_source_and_generated_records_use_actual_assignment_shapes():
    # Actual OV source assignments can be inline or handle-based. There is no
    # assignment.task and no payload.kind; generated record fields are nested.
    assert "payload有uri且有text或source_ranges" in EXTRACT_DISPATCH
    assert "有text而没有source_ranges" in EXTRACT_DISPATCH
    assert "只有source_ranges而没有text" in EXTRACT_DISPATCH
    assert "inputs没有payload.fields" in EXTRACT_DISPATCH
    assert "inputs[].payload.fields.kind=work-unit" in EXTRACT_DISPATCH
    assert "不解析原文/字符串中的kind" in EXTRACT_DISPATCH
    assert "不假设assignment内存在task字段" in SKILL_BODY
    assert "source_ranges在后代仍存在，不能单独用于区分原文" in SKILL_BODY
    assert '观察payload显式含kind="observation"' in SOURCE_OBSERVATION
    assert "未规定不从相邻配置补齐" in SOURCE_OBSERVATION
    assert "顶层有group且输入为observation/notes" in REDUCE_DISPATCH
    assert "无group且为单条method（payload.fields.kind=method）" in REDUCE_DISPATCH
    assert "首次files Map也可能有group" in SYNTHESIZE_DISPATCH
    assert "不能因group误进入方法身份归并" in SYNTHESIZE_DISPATCH
    examples = json.loads(re.search(r"```json\n(.*?)\n```", SOURCE_OBSERVATION, re.S).group(1))
    inline, handle = (item["payload"] for item in examples)
    assert "text" in inline and "source_ranges" not in inline and "fields" not in inline
    assert "kind=work-unit" in inline["text"]  # This is still source content, not a generated record.
    assert "source_ranges" in handle and "text" not in handle and "fields" not in handle


def test_final_delivery_does_not_become_a_global_child_duty():
    assert FILE_SUBMISSION not in SKILL_BODY and EVIDENCE_REVIEW not in SKILL_BODY
    assert "draft_files=[{path,content}]" not in SKILL_BODY
    assert "每Case的output必须12至24条" not in SKILL_BODY
    assert "output_format=files只适用于Planner的最终集合" in SKILL_BODY
    assert "完成当前职责即提交，不顺手执行下一阶段" in SKILL_BODY
    assert "不亲自完成条目中的文件或Case" in METHOD_PLANNING
    assert "只为该单元制作draft-unit" in UNIT_DRAFT
    assert "不为其他单元补文件" in UNIT_DRAFT
    assert FILE_SUBMISSION in UNIT_REVIEW
    assert record_format_guidance() in SOURCE_OBSERVATION


def test_native_combine_does_not_become_raw_source_or_erase_unit_boundaries():
    assert "payload.fields.text且无kind" in REDUCE_DISPATCH
    assert "当前reduce+group+RecordResponse仍选同一身份分支" in REDUCE_DISPATCH
    assert "不从text中的kind字样切换职责" in REDUCE_DISPATCH
    assert "保留合并记录全部实际相关父链" in METHOD_PLANNING
    assert "payload.fields.text" in SYNTHESIZE_DISPATCH
    assert "无法恢复则报告运行分配不足，不能从原文猜分工" in SYNTHESIZE_DISPATCH
    assert "唯一name、unit_id、unit_type" in UNIT_REVIEW
    assert "targets及完整设计" in UNIT_REVIEW
    assert "不从原文重造控制信息" in UNIT_REVIEW
    for instruction in (SYNTHESIZE_DISPATCH, UNIT_REVIEW, FILE_SUBMISSION):
        assert "经过combine时改用本次合并记录顶层ID" in instruction
        assert "不回填旧记录ID" in instruction
    # A notes-only run cannot depend on a previous draft agent loading the schema.
    assert "draft-unit或notes均读取references/unit-review.md" in SYNTHESIZE_DISPATCH
    assert "读取references/output-contract.md" in UNIT_REVIEW


def test_every_shared_attachment_declares_applicability_and_is_frozen(tmp_path):
    for content in AUXILIARY_FILES.values():
        assert "适用范围：" in content
        assert "本附件即使被共享注入也只在上述职责生效" in content
        assert "完成当前职责即提交，不顺手执行下一阶段" in content
    # Catch typos in literal resource references without interpreting placeholders
    # such as output references/<resource>.md as compiler attachment paths.
    literal_paths = set(re.findall(r"references/[a-z][a-z-]*\.md", SKILL_BODY))
    for content in AUXILIARY_FILES.values():
        literal_paths.update(re.findall(r"references/[a-z][a-z-]*\.md", content))
    assert literal_paths <= set(AUXILIARY_FILES)
    freeze_assets(tmp_path, SKILL_BODY)
    assert frozen_assets(tmp_path, SKILL_BODY) == AUXILIARY_FILES


def test_frozen_unit_planner_can_read_component_interface_before_assigning_targets(tmp_path):
    # The live failure allocated paths before the path-defining attachment was
    # available to that duty. Check the published resource dependency, not model
    # compliance or an imagined host-side path repair.
    freeze_assets(tmp_path, SKILL_BODY)
    resources = frozen_assets(tmp_path, SKILL_BODY)
    planning = resources["references/method-planning.md"]
    unit_planning = planning.split("### 单方法分工分支：", 1)[1]
    dependencies = set(re.findall(r"references/[a-z][a-z-]*\.md", unit_planning))
    assert "references/output-contract.md" in dependencies
    interface = resources["references/output-contract.md"]
    applicability = interface.split("适用范围：", 1)[1].split("。", 1)[0]
    assert "单method分工" in applicability
    # All target families must be defined in that reachable, frozen interface.
    for path in (
        "compiled_skill/<name>/METHOD.json",
        "compiled_skill/<name>/SKILL.md",
        "compiled_skill/<name>/EVALUATION.md",
        "analysis/components/<name>/manifest.json",
        "compiled_skill/<name>/references/<resource>.md",
        "analysis/components/<name>/cases/<case_id>.json",
    ):
        assert path in interface
    # Reading the shared schema must not activate final submission in planning.
    assert FILE_SUBMISSION not in planning
    assert FILE_SUBMISSION not in interface


def test_source_parent_and_configuration_accounting_survive_phase_handoffs():
    assert "逐条核对本次分配的观察ID" in METHOD_PLANNING
    assert "不能仅选代表记录" in METHOD_PLANNING
    assert "原始配置键全集" in METHOD_PLANNING
    assert "不会恢复被丢弃观察的读取权限" in METHOD_PLANNING
    assert "将完整键去向清单交给core" in METHOD_PLANNING
    assert "不能以观察摘要代替实现" in METHOD_PLANNING
    assert "草稿record.inputs引用r1，按s1回读原文" in SKILL_BODY
    assert "只引用本次work-unit顶层ID为inputs" in UNIT_DRAFT
    assert "observation→method→work-unit→draft-unit→file" in UNIT_REVIEW


def test_collection_plan_reuses_three_transforms_without_outer_recompile():
    code = re.search(r"```python\n(.*?)\n```", PLANNING, re.S).group(1)
    tree = ast.parse(code)
    calls = [node.value for node in tree.body]
    operations = [call.func.attr for call in calls]
    assert operations == ["map", "shuffle", "reduce", "map", "map", "map", "finalize"]
    transforms = [
        keyword.value.attr for call in calls for keyword in call.keywords if keyword.arg == "task"
    ]
    assert transforms == ["extract", "reduce", "reduce", "extract", "synthesize"]
    assert "不请求宿主重编译" in PLANNING
    assert "p.reduce(owned, task=contract.synthesize)" in PLANNING
    assert "contract.final_routing.mode=semantic" in PLANNING
