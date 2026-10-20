# ruff: noqa: F401
"""Reviewed IDs are preserved lineage, not a reason to split an already complete parent workflow."""

import copy

import pytest

from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import (
    CONSOLIDATING_PARTITION_PROTOCOL,
    render_identity_partition_compiler,
)
from team_miner.skill_miner.identity_components import controls, digest
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.candidate_identity import fixture, row
from tests.miner.skill_miner.support.identity_partition import output, state


def assignment(root, rows):
    fixture(root, rows)
    value = state()
    value["identity_partition_protocol"] = CONSOLIDATING_PARTITION_PROTOCOL
    _, context = orchestration._identity_window(root, value)
    return {"child": "consolidated-group", "context": context}
