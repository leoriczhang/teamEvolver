# ruff: noqa: F401
"""Exact units run inside the existing native file Map, with isolated bounded repairs."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_extract_batch_orchestration import _verified
from team_miner.skill_miner.ov_staged_orchestration import _read
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from team_miner.skill_miner.source_unit_extraction import (
    bind_source_unit_index,
    build_source_unit_packet,
    next_source_unit_assignment,
    render_source_unit_extract_compiler,
    verify_source_unit_context,
)
from team_miner.skill_miner.source_units import PROTOCOL, SourceUnitCapacityError, load_source_units
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner, begin, finish_extract
from tests.miner.skill_miner.support.source_units import frozen

POLICY = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}

def assignment(root, text):
    _, manifest = frozen(root, [text], max_chars=12000)
    context = next_source_unit_assignment(root, cursor=0, count=manifest["unit_count"],
                                          unit_index=0, language_policy=POLICY)
    return manifest, context, build_source_unit_packet(root, context, POLICY)

def output_for(packet):
    refs = packet["derived_controls"]["allowed_source_refs"]
    rows = [ref for ref, unit in zip(refs, packet["evidence"], strict=True) if unit["kind"] == "table_row"]
    other = [ref for ref in refs if ref not in rows]
    return {
        "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": ["native-container"],
        "methods": [{"task": "完整独立复核方法", "boundary": "仅按该行原始条件", "source_refs": [ref]}
                    for ref in rows],
        "facts": [{"text": "原始结构与背景", "source_refs": other}] if other else [],
        "deferred": [], "context_refs": [], "context_uncertain": False,
    }

class ExactExtractFake(ExtractFake):
    pass

class ExactExtractOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = ExactExtractFake()

    def new(self, count=3):
        job, entries = super().new(count)
        job = self.repo.update_job(job["job_id"], staged_source_scope_protocol=SOURCE_SCOPE_PROTOCOL_V3,
                                   staged_workflow_protocol="te-direct-workflow-planning.v1",
                                   staged_source_unit_protocol=PROTOCOL)
        for index, entry in enumerate(entries):
            text = f"# 方法 {index}\n\n| 条件 | 完整方法 |\n| --- | --- |\n" + "".join(
                f"| 条件 {row} | " + "首先核实输入，获得确认后执行；" * 80 + "完成。 |\n" for row in range(24)
            )
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries
