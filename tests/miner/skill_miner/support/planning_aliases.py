# ruff: noqa: F401
"""Exact packet-local bookkeeping; no model, network or runtime data calls."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_refinement import metadata_sha256
from team_miner.skill_miner.direct_workflow_planning import validate_capacity
from team_miner.skill_miner.planning_aliases import (
    CAPACITY_SCHEMA,
    PROTOCOL,
    PlanningAliasManifestError,
    PlanningAliasOutputError,
    build_planning_alias_packet,
    decode_planning_alias_output,
    validate_planning_alias_manifest,
)


def packet(schema=planning.SELECTION_SCHEMA_V2, *, suffix="a", methods=3):
    items = [{
        "item_id": f"item-{suffix}-{index}", "kind": "method", "task": f"执行流程{index}",
        "boundary": "仅据原文", "source_refs": [f"scope-{suffix}-{index}"],
        "source_file_ids": [f"file-{suffix}"],
    } for index in range(methods)]
    authority = {
        "candidate_id": f"candidate-{suffix}", "name": "existing-workflow", "task": "既有流程",
        "trigger": "用户请求", "inputs": ["用户材料"], "outputs": ["完成结果"],
        "boundary": "原文约束", "rationale": "具备方法", "candidate_metadata_is_evidence": False,
    }
    if schema == planning.SELECTION_SCHEMA_V3:
        authority.update(refinement_allowed=True, refinement_item_ids=[items[0]["item_id"]],
                         base_metadata_sha256=metadata_sha256(authority))
    controls = {
        "selection_schema": schema, "items": items, "input_ids": [f"unit-{suffix}"],
        "allowed_source_refs": [row["source_refs"][0] for row in items],
        "related_facts": [{"text": "ID m001 是原文中的文字，不替换", "readonly": True,
                           "source_fragment_ids": [f"src-{suffix}"]}],
        "provisional_gaps": [], "known_candidates": [] if schema == planning.SELECTION_SCHEMA else [authority],
    }
    return planning.build_stage_packet("plan", "selection.json", controls=controls)

def new_skill(ids):
    return {"derived_from": ids, "name": "new-workflow", "task": "执行完整方法 m001",
            "trigger": "请求中包含 c001 字面文本", "inputs": ["材料 u001"], "outputs": ["结果"],
            "boundary": "只按原文", "rationale": "原文支持"}

def selection(manifest):
    ids = list(manifest["methods"])
    return {
        "schema_version": manifest["selection_schema"], "input_ids": list(manifest["container_ids"]),
        "skills": [new_skill(ids[:1])], "knowledge": [
            {"derived_from": [identity], "reason": "仅作事实查询"} for identity in ids[1:]
        ], "deferred": [], "resolved": [],
    }

def resign(manifest):
    data = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
