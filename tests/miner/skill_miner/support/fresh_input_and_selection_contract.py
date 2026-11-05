# ruff: noqa: F401
"""Fresh compiler hints retain the real schemas, public operations."""

import ast
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy as strategy
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import _compiler_package
from team_miner.skill_miner.ov_staged_orchestration import _canonical_extract_references
from tests.miner.skill_miner.fixtures import build_document
from tests.miner.skill_miner.support.bounded_compile_plan import candidate, planning_catalog
from tests.miner.skill_miner.support.compile_strategy import embedded_contract


def _substitute(value, replacements):
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    return replacements.get(value, value)

def _selection_example(*, refinement=False):
    units, _ = planning_catalog(2)
    items = planning.planning_items([{
        "id": "shared-control-unit", "value": {
            "methods": [unit["value"]["methods"][0] for unit in units], "facts": [], "deferred": [],
        },
    }])
    authority = {key: item for key, item in candidate([]).items() if key != "source_refs"}
    authority["name"] = "existing-method"
    identity = "candidate-existing-method"
    replacements = {
        "<exact assigned control unit ID>": "shared-control-unit",
        "<new-safe-kebab-name>": "new-method",
        "<supported complete task>": items[0]["task"],
        "<supported applicability>": "A request invokes this supported method.",
        "<required runtime input>": "The request and its provided Evidence.",
        "<terminal result>": "A recorded result.",
        "<supported conditions and limits>": items[0]["boundary"],
        "<source-supported selection reason>": "The assigned source describes this independent method.",
        "<assigned item ID A>": items[0]["item_id"],
        "<assigned item ID B>": items[1]["item_id"],
        "<exact assigned known candidate ID>": identity,
        "<why this method belongs to that same complete workflow>": "The assigned detail belongs to this workflow.",
    }
    template = (strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE_V3 if refinement
                else strategy.LOCAL_SELECTION_CONTRACT_EXAMPLE)
    value = _substitute(json.loads(template), replacements)
    if refinement:
        authority.update(refinement_allowed=True, refinement_item_ids=[items[1]["item_id"]])
    return items, value, {identity: authority}
