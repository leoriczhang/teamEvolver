# ruff: noqa: F401
"""Metadata retrieval may reorder methods, never decide semantic equivalence or alter Evidence."""

from __future__ import annotations

import copy
import sqlite3

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as identities
from team_miner.skill_miner import candidate_refinement as refinement
from team_miner.skill_miner import workflow_planning as workflow
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.skill_miner.source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL
from tests.miner.skill_miner.support.candidate_identity import candidate, fixture, group_current, row


def method(root, index, task, *, boundary="Use supplied runtime inputs and preserve the original constraints"):
    value = {
        "item": {"item_id": f"method-{index}", "kind": "method", "task": task, "boundary": boundary,
                 "source_refs": [f"scope-{index}"], "source_files": [f"viking://original/{index}.md"]},
        "ordinals": [index], "leaf_items": [f"leaf-{index}"], "unit_id": f"unit-{index}",
        "bindings": {f"scope-{index}": [index]},
    }
    _save(root / "catalog/method" / f"{index:08d}.json", value)
    return value

def previous_scope(tmp_path):
    root = tmp_path / "round_inputs/round-2/staged"
    previous = tmp_path / "round_inputs/round-1/staged"
    relative = "original/requests.md"
    snapshot = f"{RUNS_ROOT}/job/rounds/round-1/source"
    job = {"job_id": "job", "current_round": 2,
           "source_snapshot": f"{RUNS_ROOT}/job/rounds/round-2/source",
           "staged_efficiency_protocol": EFFICIENCY_PROTOCOL}
    _save(tmp_path / "rounds/round-1/input.json", {"source_snapshot": snapshot})
    scope = {"scope_protocol": SOURCE_SCOPE_PROTOCOL, "ordinals": [1, 2], "method_ordinals": [2],
             "context_ordinals": [1], "full_ordinals": [0, 1, 2, 3]}
    _save(previous / "scopes/refund-review.json", scope)
    for ordinal in range(4):
        _save(previous / "chunk-metadata" / f"{ordinal:08d}.json", {
            "source_uri": snapshot + "/" + relative, "source_sha256": "exact-source-sha",
            "start_char": ordinal * 50, "end_char": (ordinal + 1) * 50,
        })
    current_uri = job["source_snapshot"] + "/" + relative
    _save(root / "source-files" / (_sha(current_uri) + ".json"), {"uri": current_uri, "ordinals": [3, 4, 5, 6, 7]})
    for ordinal, start in enumerate(range(0, 200, 40), 3):
        _save(root / "chunk-metadata" / f"{ordinal:08d}.json", {
            "source_uri": current_uri, "source_sha256": "exact-source-sha",
            "start_char": start, "end_char": start + 40,
        })
    return root, previous, job, relative

def reuse_response():
    authority = candidate("refund-review")
    authority["candidate_id"] = "known-candidate"
    item = {"item_id": "method-1", "kind": "method", "task": "Review refund request",
            "boundary": "Respect original constraints", "source_refs": ["scope-1"]}
    row = {"reuse_candidate_id": authority["candidate_id"], "derived_from": [item["item_id"]],  # noqa: F811
           "reason": "Same independently reusable task"}
    value = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": ["unit-1"], "skills": [row],
             "knowledge": [], "deferred": [], "resolved": []}
    return value, authority, item

def validate_reuse(value, authority, item):
    return planning.validate_selection(value, items=[item], input_ids=["unit-1"],
                                       known_candidates={authority["candidate_id"]: authority})
