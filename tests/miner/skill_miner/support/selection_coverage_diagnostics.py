# ruff: noqa: F401
"""Exact planning diagnostics repair protocol defects without assigning missing methods."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.selection_conflicts import collect_selection_conflicts, repair_selection_controls
from tests.miner.skill_miner.support.bounded_compile_plan import candidate

JOB = {"job_id": "coverage-test", "current_round": 1}

LANGUAGE = {"language": "en", "version": 1, "basis": "frozen_original_source"}

def assignment(count=24):
    items = [{"item_id": f"item-{index:024d}", "kind": "method", "task": f"Original procedure {index}",
              "boundary": "Preserve supplied conditions", "source_refs": [f"scope-{index}"]}
             for index in range(count)]
    controls = {"items": items, "input_ids": ["unit-original"],
                "allowed_source_refs": [item["source_refs"][0] for item in items],
                "selection_schema": planning.SELECTION_SCHEMA_V2, "known_candidates": []}
    value = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": controls["input_ids"],
             "skills": [{**{key: value for key, value in candidate([]).items() if key != "source_refs"},
                         "derived_from": [item["item_id"] for item in items]}],
             "knowledge": [], "deferred": [], "resolved": []}
    return controls, value

def validate(controls, value):
    return planning.validate_selection(value, items=controls["items"], input_ids=controls["input_ids"],
                                       known_candidates={row["candidate_id"]: row
                                                         for row in controls["known_candidates"]},
                                       max_input_items=24)

def diagnostics(controls, value):
    return planning.planning_coverage_diagnostics(value, [item["item_id"] for item in controls["items"]])

def packet_chars(controls):
    packet = planning.build_stage_packet("plan", "selection.json", controls=controls, language_policy=LANGUAGE)
    return len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
