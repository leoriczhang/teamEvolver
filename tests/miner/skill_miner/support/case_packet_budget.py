# ruff: noqa: F401
"""Case history projection preserves original source, exact identity and strict duplicate checks."""

from __future__ import annotations

import asyncio
import copy

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_case_batch_orchestration import _bounded_case_controls, case_source_roles
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.skill_miner.source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL
from tests.miner.skill_miner.support.candidate_identity import candidate
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchOwner


def assignment():
    chunks = [planning.source_chunks(chr(97 + i) * size, "viking://source/" + str(i) + "x" * 150)[0]
              for i, size in enumerate([11000, 11000, 2710])]
    controls = {
        "candidate": {"name": "review-request", "task": "t" * 400, "trigger": "t" * 400, "boundary": "b" * 400},
        "skill_name": "review-request", "case_id": "review-request-case-12",
        "allowed_source_refs": [chunk["source_uri"] for chunk in chunks],
        "input_ids": [chunk["id"] for chunk in chunks], "coverage_dimension": planning.CASE_DIMENSIONS[11],
        "previous_queries": ["q" * 300 for _ in range(11)], "previous_queries_excerpted": False,
        "source_scope_rule": "Only assigned Evidence supports this Case.", "case_batch_size": 4,
        "repair_error": "e" * 1000,
    }
    return chunks, controls
