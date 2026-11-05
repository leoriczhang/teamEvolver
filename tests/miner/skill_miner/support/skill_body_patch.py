# ruff: noqa: F401
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
