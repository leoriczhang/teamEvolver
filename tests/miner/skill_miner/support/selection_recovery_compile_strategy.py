# ruff: noqa: F401
"""Fresh recovery instructions clarify ledgers without loosening public Compile or canonical authority."""

import ast
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from tests.miner.skill_miner.support.bounded_compile_plan import candidate, planning_catalog
from tests.miner.skill_miner.support.compile_strategy import embedded_contract


def selection_with_reuse(count=3):
    units, items = planning_catalog(count)
    authority = {k: v for k, v in candidate([]).items() if k != "source_refs"}
    authority["task"] = "Inspect the request, investigate the relevant conditions and record a complete decision."
    identity = "candidate-complete-method"
    value = {
        "schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": [u["id"] for u in units],
        "skills": [{"derived_from": [i["item_id"] for i in items], "reuse_candidate_id": identity,
                    "reason": "The current inspection detail belongs to the assigned complete method."}],
        "knowledge": [], "deferred": [], "resolved": [],
    }
    return items, value, {identity: authority}
