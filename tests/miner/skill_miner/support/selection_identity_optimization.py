# ruff: noqa: F401
"""Finite repairs and versioned bounded identity batches preserve methods and source scope."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import GROUP_SCHEMA, IdentityCatalog, identity_version
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.selection_conflicts import collect_selection_conflicts, repair_selection_controls
from tests.miner.skill_miner.support.candidate_identity import candidate, fixture, row

LANGUAGE = {"language": "zh-CN"}

JOB = {"job_id": "job", "current_round": 1}

def controls():
    return {
        "items": [{"item_id": "method-1", "kind": "method", "task": "收到退款后处理独立任务",
                   "boundary": "原文边界", "source_refs": ["scope-1"]}],
        "input_ids": ["unit-1"], "allowed_source_refs": ["scope-1"],
        "selection_schema": planning.SELECTION_SCHEMA_V2, "known_candidates": [],
        "related_facts": [{"text": "辅助摘要", "source_file_ids": ["file-1"]}],
        "provisional_gaps": [{"reason": "局部摘录不足", "source_file_ids": ["file-1"]}],
    }

def proposed(name, task="另一个独立退款工作流"):
    value = candidate(name, task)
    value.pop("source_refs")
    value["derived_from"] = ["method-1"]
    return value

def run_windows(root, state):
    calls, assignments = 0, []
    while (window := orchestration._identity_window(root, state)) is not None:
        rows, context = window
        assignments.append(copy.deepcopy(context))
        ids = context["identity_ids"]
        orchestration._accept_groups(root, {"child": f"review-{calls}", "context": context}, {
            "schema_version": GROUP_SCHEMA, "candidate_ids": ids,
            "groups": [{"candidate_ids": [identity], "reason": "保守保留独立方法"} for identity in ids],
        })
        state["identity_cursor"] = context["after"]
        calls += 1
        assert calls < 256
    return calls, assignments
