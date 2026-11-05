"""Additive body contracts retain accepted conditions and exact source ownership."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner.skill_body_patch import (
    CAPACITY_SCHEMA,
    OUTPUT_SCHEMA,
    PROTOCOL,
    BodyPatchCapacityError,
    BodyPatchError,
    build_body_patch,
    new_body_state,
    reduce_body_patch,
    render_body_patch,
    validate_body_patch,
    validate_body_patch_manifest,
    validate_body_state,
)

CANDIDATE = {
    "name": "complete-meeting", "task": "完成一次客户沟通对练。", "trigger": "客户同意交流时。",
    "inputs": ["本次交流目标", "客户给出的时间范围"], "outputs": ["双方确认的下一步"],
    "boundary": "用于沟通训练；未知材料保持待核实。", "rationale": "完整方法而非人物示例。",
}


def unit(identity, text="先确认对方是否方便交流。", *, start=0, original=None, chunk="shared-chunk"):
    original = original if original is not None else text
    return {"id": identity, "source_uri": "viking://source/原始文档.md", "start_char": start,
            "end_char": start + len(text), "content": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_sha256": hashlib.sha256(original.encode()).hexdigest(), "original_chunk_id": chunk}


def assignment(state=None, evidence=None, operative=None, context=None, assignment_id="batch-1", **limits):
    state = new_body_state(CANDIDATE) if state is None else state
    evidence = [unit("u1")] if evidence is None else evidence
    operative = [row["id"] for row in evidence] if operative is None else operative
    context = [] if context is None else context
    built = build_body_patch(CANDIDATE, state, evidence, operative_ids=operative, context_ids=context,
                             assignment_id=assignment_id, **limits)
    assert built["status"] == "ready", built
    value = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": built["manifest"]["assignment_sha256"],
             "base_state_sha256": state["state_sha256"], "input_ids": [row["id"] for row in evidence],
             "title": state["title"] or "完整客户沟通方法", "add": [], "attach": [],
             "step_order": list(state["step_order"]),
             "no_change_reason": "本批没有新增适用条件。"}
    return state, built, value


def add_step(value, identity="a0001", text="确认客户愿意交流。", support="u1"):
    value["no_change_reason"] = ""
    value["add"].append({"id": identity, "kind": "step", "text": text, "support_ids": [support]})
    value["step_order"].append(identity)


def test_accepted_duration_consent_optional_and_conflict_survive_later_unrelated_patch():
    # Regression: successive whole-body revisions previously omitted these exact qualifications.
    evidence = [unit("u1", "首次说明约20分钟；进一步了解需另加40分钟，并征得客户同意。"),
                unit("u2", "可选择讲述过去工作中的高光经历。"),
                unit("u3", "退休以后费用不会立刻下降，而是缓慢下降。账户称谓的对应关系待核实。")]
    state, first, patch = assignment(evidence=evidence)
    add_step(patch, text="说明交流安排并确认对方愿意继续。")
    patch["add"].append({"id": "a0002", "kind": "conflict", "text": "原文账户称谓未映射，保持待核实。",
                         "support_ids": ["u3"]})
    patch["add"].append({"id": "a0003", "kind": "limit", "text": "退休后费用不会立刻下降，而是缓慢下降。",
                         "support_ids": ["u3"]})
    patch["attach"] = [
        {"id": "a0004", "parent_id": "a0001", "kind": "condition",
         "text": "首次说明约20分钟；另加40分钟必须再次征得同意。", "support_ids": ["u1"]},
        {"id": "a0005", "parent_id": "a0001", "kind": "example",
         "text": "过去工作的高光经历是可选示例，并非每次必须讲述。", "support_ids": ["u2"]},
    ]
    state = reduce_body_patch(state, patch, first["manifest"])
    original_atoms = copy.deepcopy(state["atoms"])
    state, second, patch2 = assignment(state, [unit("u4", "结束时确认下次交流安排。")], assignment_id="batch-2")
    add_step(patch2, "a0006", "确认下次交流安排。", "u4")
    final = reduce_body_patch(state, patch2, second["manifest"])
    assert final["atoms"][:5] == original_atoms
    rendered = render_body_patch(CANDIDATE, final)
    assert "另加40分钟必须再次征得同意" in rendered
    assert "可选示例，并非每次必须" in rendered
    assert "原文账户称谓未映射，保持待核实" in rendered
    assert "退休后费用不会立刻下降，而是缓慢下降" in rendered
    assert rendered.count("说明交流安排并确认对方愿意继续") == 1
    assert "## 未解决问题" in rendered and "## 来源" in rendered
    assert final["protocol"] == PROTOCOL


def test_later_context_condition_attaches_to_stable_existing_action_without_rewriting_it():
    state, built, value = assignment()
    add_step(value)
    state = reduce_body_patch(state, value, built["manifest"])
    state, second, patch = assignment(state, [unit("u2", "延长交流时间前必须再次询问同意。")],
                                       operative=[], context=["u2"], assignment_id="batch-2")
    assert second["controls"]["atom_directory"] == state["atoms"]
    assert second["controls"]["directory_text_complete"] is True
    assert second["controls"]["next_atom_id"] == "a0002"
    patch.update(no_change_reason="", attach=[{
        "id": "a0002", "parent_id": "a0001", "kind": "condition", "text": "延长前再次征得同意。",
        "support_ids": ["u2"],
    }])
    after = reduce_body_patch(state, patch, second["manifest"])
    assert after["atoms"][0] == state["atoms"][0]
    assert after["atoms"][1]["parent_id"] == "a0001"


def test_late_discovered_prerequisite_can_precede_existing_step_without_losing_conditions():
    state, first, patch = assignment(evidence=[unit("u1", "确认客户时间，并约定结束方式。")])
    add_step(patch, text="确认客户可用时间。")
    patch["attach"] = [{"id": "a0002", "parent_id": "a0001", "kind": "condition",
                        "text": "不得超过客户同意的时长。", "support_ids": ["u1"]}]
    accepted = reduce_body_patch(state, patch, first["manifest"])
    _, second, later = assignment(accepted, [unit("u2", "交流前先表明身份并取得同意。")],
                                   assignment_id="batch-2")
    add_step(later, "a0003", "先表明身份并取得同意。", "u2")
    later["step_order"] = ["a0003", "a0001"]
    final = reduce_body_patch(accepted, later, second["manifest"])
    rendered = render_body_patch(CANDIDATE, final)
    assert rendered.index("1. 先表明身份") < rendered.index("2. 确认客户可用时间")
    assert "条件与例外：不得超过客户同意的时长" in rendered
    assert final["atoms"][:2] == accepted["atoms"]
    assert reduce_body_patch(final, patch, first["manifest"]) == final


def test_order_only_patch_requires_reason_and_preserves_every_atom():
    state, first, patch = assignment()
    add_step(patch)
    add_step(patch, "a0002", "先确认身份。")
    accepted = reduce_body_patch(state, patch, first["manifest"])
    _, second, order = assignment(accepted, [unit("u2", "确认身份在征询意愿之前。")], assignment_id="batch-2")
    order.update(step_order=["a0002", "a0001"], no_change_reason="本批原文明确先确认身份，调整展示顺序。")
    final = reduce_body_patch(accepted, order, second["manifest"])
    assert final["atoms"] == accepted["atoms"]
    assert final["step_order"] == ["a0002", "a0001"]
    order["no_change_reason"] = ""
    with pytest.raises(BodyPatchError, match="no_change_reason"):
        validate_body_patch(order, second["manifest"])


@pytest.mark.parametrize("order", [[], ["a0001", "a0001"], ["unknown"], ["a0001", "a0002"]])
def test_step_order_is_an_exact_permutation(order):
    _, built, patch = assignment()
    add_step(patch)
    patch["step_order"] = order
    with pytest.raises(BodyPatchError, match="step_order"):
        validate_body_patch(patch, built["manifest"])


def test_adjacent_methods_in_same_transport_chunk_do_not_share_operative_authority():
    phone, opening = "电话中确认见面时间。", "首次面谈详细介绍工作经历。"
    raw = phone + opening
    evidence = [unit("phone-row", phone, original=raw),
                unit("opening-row", opening, start=len(phone), original=raw)]
    state, built, patch = assignment(evidence=evidence, operative=["phone-row"], context=["opening-row"])
    add_step(patch, text=opening, support="opening-row")
    with pytest.raises(BodyPatchError, match="Context alone"):
        reduce_body_patch(state, patch, built["manifest"])
    assert state["atoms"] == []
    patch["add"][0].update(text=phone, support_ids=["phone-row"])
    accepted = reduce_body_patch(state, patch, built["manifest"])
    assert accepted["atoms"][0]["support_ids"] == ["phone-row"]
    assert "首次面谈详细介绍" not in render_body_patch(CANDIDATE, accepted)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(input_ids=[]),
    lambda p: p.update(input_ids=["u1", "u1"]),
    lambda p: p.update(input_ids=["unknown"]),
    lambda p: p.update(assignment_sha256="0" * 64),
    lambda p: p.update(base_state_sha256="0" * 64),
    lambda p: p["add"][0].update(support_ids=["unknown"]),
    lambda p: p["add"][0].update(support_ids=[]),
    lambda p: p["add"][0].update(id="a0002"),
    lambda p: p["add"][0].update(kind="replace"),
    lambda p: p.update(replace=[]),
    lambda p: p.update(no_change_reason="同时新增不允许该原因。"),
])
def test_rejects_incomplete_coverage_wrong_authority_unknown_support_or_overwrite(mutate):
    state, built, patch = assignment()
    add_step(patch)
    before = copy.deepcopy((state, built))
    mutate(patch)
    with pytest.raises(BodyPatchError):
        reduce_body_patch(state, patch, built["manifest"])
    assert (state, built) == before


def test_empty_patch_records_every_seen_unit_without_creating_operational_content():
    state, built, patch = assignment(evidence=[unit("u1"), unit("u2", "仅为背景。")])
    after = reduce_body_patch(state, patch, built["manifest"])
    assert after["atoms"] == []
    assert after["receipts"][0]["input_ids"] == ["u1", "u2"]
    with pytest.raises(BodyPatchError, match="operative step"):
        render_body_patch(CANDIDATE, after)


def test_exact_duplicate_is_idempotent_before_and_after_later_patch_but_changed_receipt_rejected():
    state, built, patch = assignment()
    add_step(patch)
    first = reduce_body_patch(state, patch, built["manifest"])
    assert reduce_body_patch(first, patch, built["manifest"]) == first
    _, second, next_patch = assignment(first, [unit("u2", "确认结束。")], assignment_id="batch-2")
    add_step(next_patch, "a0002", "确认结束。", "u2")
    final = reduce_body_patch(first, next_patch, second["manifest"])
    assert reduce_body_patch(final, patch, built["manifest"]) == final
    changed = copy.deepcopy(patch)
    changed["add"][0]["text"] = "改写已经接受的动作。"
    with pytest.raises(BodyPatchError, match="different output"):
        reduce_body_patch(final, changed, built["manifest"])


def test_wrong_base_and_rebuilding_accepted_assignment_are_rejected():
    state, built, patch = assignment()
    add_step(patch)
    first = reduce_body_patch(state, patch, built["manifest"])
    with pytest.raises(BodyPatchError, match="rebuilt"):
        assignment(first)
    _, second, patch2 = assignment(first, [unit("u2")], assignment_id="batch-2")
    with pytest.raises(BodyPatchError, match="stale"):
        reduce_body_patch(state, patch2, second["manifest"])


@pytest.mark.parametrize("target", ["manifest", "source", "roles", "base", "controls"])
def test_exact_frozen_manifest_rejects_tampered_source_scope_or_previous_text(target):
    _, built, _ = assignment()
    manifest, controls = copy.deepcopy(built["manifest"]), copy.deepcopy(built["controls"])
    if target == "manifest":
        manifest["assignment_sha256"] = "0" * 64
    elif target == "source":
        manifest["evidence"][0]["content"] = "无依据替换。"
    elif target == "roles":
        manifest.update(operative_ids=[], context_ids=["u1"])
    elif target == "base":
        manifest["base_state"]["title"] = "无依据改题。"
    else:
        controls["next_atom_id"] = "a9999"
    with pytest.raises(BodyPatchError):
        validate_body_patch_manifest(manifest, controls)


@pytest.mark.parametrize("field,text", [("title", "Complete meeting"), ("atom", "Always request consent."),
                                        ("reason", "Nothing new."), ("title", ""),
                                        ("atom", "中文说明\n# English title")])
def test_chinese_title_atom_and_reason_contract(field, text):
    _, built, patch = assignment()
    if field != "reason":
        add_step(patch)
    if field == "atom":
        patch["add"][0]["text"] = text
    else:
        patch["title" if field == "title" else "no_change_reason"] = text
    with pytest.raises(ValueError):
        validate_body_patch(patch, built["manifest"])


def test_chinese_title_is_frozen_and_internal_tokens_are_not_translated():
    state, built, patch = assignment()
    add_step(patch, text="核对 `customer_id` 与 JSON 记录，再确认交流安排。")
    first = reduce_body_patch(state, patch, built["manifest"])
    _, second, next_patch = assignment(first, [unit("u2")], assignment_id="batch-2")
    next_patch["title"] = "不同的中文标题"
    with pytest.raises(BodyPatchError, match="title is frozen"):
        validate_body_patch(next_patch, second["manifest"])
    assert "`customer_id`" in render_body_patch(CANDIDATE, first)


def test_attach_only_to_steps_and_cannot_overwrite_old_id():
    state, built, patch = assignment()
    patch.update(no_change_reason="", add=[{"id": "a0001", "kind": "limit", "text": "保留边界。",
                                           "support_ids": ["u1"]}], attach=[{
        "id": "a0002", "parent_id": "a0001", "kind": "condition", "text": "必须征求同意。",
        "support_ids": ["u1"],
    }])
    with pytest.raises(BodyPatchError, match="parent_id"):
        reduce_body_patch(state, patch, built["manifest"])


def test_conflict_is_displayed_as_unresolved_and_has_no_resolve_operation():
    state, built, patch = assignment()
    add_step(patch)
    patch["attach"] = [{"id": "a0002", "parent_id": "a0001", "kind": "conflict",
                        "text": "两处原文时长不一致，保持待核实。", "support_ids": ["u1"]}]
    after = reduce_body_patch(state, patch, built["manifest"])
    assert "未决冲突：两处原文时长不一致，保持待核实" in render_body_patch(CANDIDATE, after)
    patch["attach"][0]["kind"] = "resolved"
    with pytest.raises(BodyPatchError, match="permitted kinds"):
        validate_body_patch(patch, built["manifest"])


def test_output_12k_limit_checks_raw_bytes_as_characters_and_complete_json():
    _, built, patch = assignment()
    raw = json.dumps(patch, ensure_ascii=False)
    with pytest.raises(BodyPatchCapacityError, match="raw"):
        validate_body_patch(" " * 12000 + raw, built["manifest"])
    patch.update(no_change_reason="", add=[{
        "id": f"a{i:04d}", "kind": "step", "text": "完整中文条件" * 180, "support_ids": ["u1"],
    } for i in range(1, 12)])
    with pytest.raises(BodyPatchCapacityError, match="output limit"):
        validate_body_patch(patch, built["manifest"])


def test_raw_json_cannot_shadow_fields_or_hide_invalid_first_value():
    _, built, patch = assignment()
    raw = json.dumps(patch, ensure_ascii=False)
    raw = raw.replace('"add": []', '"add": [{"kind":"replace"}], "add": []')
    with pytest.raises(BodyPatchError, match="complete JSON"):
        validate_body_patch(raw, built["manifest"])


def test_explicit_capacity_does_not_advance_body_or_coverage():
    state, built, patch = assignment()
    capacity = {key: value for key, value in patch.items()
                if key in {"assignment_sha256", "base_state_sha256", "input_ids"}}
    capacity.update(schema_version=CAPACITY_SCHEMA, reason="完整新增条件无法放入本批输出，需缩小当前来源批次。")
    before = copy.deepcopy(state)
    assert validate_body_patch(capacity, built["manifest"]) == capacity
    with pytest.raises(BodyPatchCapacityError):
        reduce_body_patch(state, capacity, built["manifest"])
    assert state == before


def test_controls_and_candidate_capacity_never_clip_existing_atoms():
    state, built, patch = assignment()
    add_step(patch)
    first = reduce_body_patch(state, patch, built["manifest"])
    before = copy.deepcopy(first)
    too_small = build_body_patch(CANDIDATE, first, [unit("u2")], operative_ids=["u2"], context_ids=[],
                                assignment_id="batch-2", max_controls_chars=100)
    assert too_small["status"] == "needs_batching"
    assert too_small["controls"] is None and too_small["manifest"] is None
    assert first == before
    _, second, patch2 = assignment(first, [unit("u2")], assignment_id="batch-2", max_atoms=1)
    add_step(patch2, "a0002", "新增动作。", "u2")
    with pytest.raises(BodyPatchCapacityError, match="max_atoms"):
        reduce_body_patch(first, patch2, second["manifest"])
    assert first == before


def test_complete_state_capacity_includes_sources_and_receipts():
    state, built, patch = assignment(max_state_chars=500)
    add_step(patch)
    with pytest.raises(BodyPatchCapacityError, match="state exceeds"):
        reduce_body_patch(state, patch, built["manifest"])
    assert not state["atoms"]


def test_inputs_and_outputs_are_not_mutated_and_rendering_is_deterministic():
    state, built, patch = assignment()
    add_step(patch)
    before = copy.deepcopy((CANDIDATE, state, built, patch))
    checked = validate_body_patch(patch, built["manifest"])
    after = reduce_body_patch(state, checked, built["manifest"])
    assert (CANDIDATE, state, built, patch) == before
    assert render_body_patch(CANDIDATE, after) == render_body_patch(CANDIDATE, after)
    checked["add"][0]["text"] = "外部修改副本"
    assert patch["add"][0]["text"] != checked["add"][0]["text"]
    after["atoms"][0]["text"] = "篡改持久化状态"
    with pytest.raises(BodyPatchError, match="state hash"):
        validate_body_state(after)


@pytest.mark.parametrize("roles", [(["u1"], ["u1"]), ([], []), (["other"], []), (["u1", "u1"], [])])
def test_source_role_partition_is_exact(roles):
    with pytest.raises(BodyPatchError):
        build_body_patch(CANDIDATE, new_body_state(CANDIDATE), [unit("u1")], operative_ids=roles[0],
                         context_ids=roles[1], assignment_id="batch-1")
