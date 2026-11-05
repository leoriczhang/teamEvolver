# ruff: noqa: F401
"""Pure TE invariants for staging, full provenance and deterministic assembly."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner.bounded_compile_plan import (
    assemble_dataset,
    build_stage_packet,
    compose_skill,
    is_deferred,
    iter_skill_chunks,
    planning_items,
    selection_plan,
    source_chunks,
    validate_chunks,
    validate_deferred,
    validate_index,
    validate_merge,
    validate_plan,
    validate_section,
    validate_section_skip,
    validate_selection,
)
from team_skills.library.frontmatter import parse_skill_md_text, render_skill_md, validate_skill_md
from tests.miner.skill_miner.fixtures import build_document

URI = "viking://resources/team/source/退货.md"

def candidate(refs):
    return {
        "name": "return-check",
        "task": "核验退货申请",
        "trigger": "收到申请",
        "inputs": ["申请及原始条款"],
        "outputs": ["核验结果和缺口"],
        "boundary": "仅根据提供的条款",
        "rationale": "同一独立任务",
        "source_refs": refs,
    }

def large_valid_index(alias):
    """Represent a real compact locator with a 252-character, source-specific boundary."""
    return {
        "schema_version": "skill-miner.index.v1",
        "input_ids": [alias],
        "methods": [
            {
                "task": "核对给定地区、单位、阈值和例外。" * 6,
                "boundary": ("只适用原始条款明确规定的条件，不能推断未提供的业务规则。" * 12)[:252],
                "source_refs": [alias],
            }
            for _ in range(4)
        ],
        "facts": [{"text": "逐项保留原文条件及其适用范围。" * 50, "source_refs": [alias]} for _ in range(5)],
        "deferred": [{"reason": "未说明的事实仍然需要补充原始 Evidence。" * 25, "source_refs": [alias]}],
    }

def planning_catalog(method_count=38):
    units = [{
        "id": f"unit-{index}",
        "value": {
            "methods": [{
                "task": f"执行操作流程 {index}", "boundary": "按本条款核验条件", "source_refs": [f"scope-{index}"]
            }],
            "facts": [], "deferred": [],
        },
    } for index in range(method_count)]
    return units, planning_items(units)

def _custom_identity_cases():
    return build_document(
        "return-check",
        [
            {"case_id": f"reviewed-return-{index:03d}", "query": f"独立合成申请场景 {index}",
             "source_session_ids": [URI]}
            for index in range(12)
        ],
    )["cases"]
