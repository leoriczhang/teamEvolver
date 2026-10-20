# ruff: noqa: F401
from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_refinement import metadata_sha256
from team_miner.skill_miner.compile_strategy import (
    mining_policy_block,
    render_aliased_workflow_compiler,
    render_compact_aliased_workflow_compiler,
)
from team_miner.skill_miner.direct_workflow_planning import CAPACITY_SCHEMA, validate_capacity
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.planning_aliases import build_planning_alias_packet, decode_planning_alias_output
from team_miner.skill_miner.workflow_planning import COMPACT_PLANNING_PROTOCOL, coalesce_exact_authority_reuse
from tests.miner.skill_miner.fixtures import build_document


def _fresh_capacity(value, input_ids, source_refs):
    return validate_capacity(value, input_ids, source_refs, max_reason_chars=planning.MAX_OUTPUT_CHARS)

def authority(identity="candidate-exact", name="request-review"):
    return {
        "candidate_id": identity,
        "name": name,
        "task": "处理完整请求",
        "trigger": "收到请求",
        "inputs": ["原始条件和当前请求"],
        "outputs": ["处理结论"],
        "boundary": "只用所提供的原始条件",
        "rationale": "完整独立任务",
        "candidate_metadata_is_evidence": False,
    }

def controls(count=2, schema=planning.SELECTION_SCHEMA_V2):
    return {
        "selection_schema": schema,
        "input_ids": ["exact-container"],
        "allowed_source_refs": [f"original-source-{index}" for index in range(count)],
        "known_candidates": [authority()],
        "items": [
            {
                "item_id": f"method-{index}",
                "kind": "method",
                "task": "处理请求",
                "boundary": "仅提供原文范围",
                "source_refs": [f"original-source-{index}"],
            }
            for index in range(count)
        ],
    }

def selection(control):
    return {
        "schema_version": control["selection_schema"],
        "input_ids": control["input_ids"],
        "skills": [
            {
                "reuse_candidate_id": "candidate-exact",
                "reason": f"同完整流程的已支持分支-{index}",
                "derived_from": [item["item_id"]],
            }
            for index, item in enumerate(control["items"])
        ],
        "knowledge": [],
        "deferred": [],
        "resolved": [],
    }

def long_identity_assignment():
    refs = ["scope-" + f"{index:024d}" for index in range(28)]
    control = {
        "selection_schema": planning.SELECTION_SCHEMA_V2,
        "input_ids": ["unit-" + f"{index:024d}" for index in range(12)],
        "allowed_source_refs": refs,
        "known_candidates": [],
        "items": [{"item_id": "item-" + f"{index:024d}", "kind": "method",
                   "task": "Complete assigned method", "boundary": "Original boundary",
                   "source_refs": refs[index:index + 1] if index < 23 else refs[23:]}
                  for index in range(24)],
    }
    packet = planning.build_stage_packet("plan", "selection.json", controls=control)
    aliased, manifest = build_planning_alias_packet(packet)
    return control, aliased["derived_controls"], manifest

def capacity(reason):
    return {
        "schema_version": CAPACITY_SCHEMA,
        "input_ids": ["container"],
        "source_refs": ["first", "second"],
        "reason": reason,
    }
