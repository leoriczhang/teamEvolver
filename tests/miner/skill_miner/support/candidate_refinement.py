# ruff: noqa: F401
"""Explicit later-round revisions correct business fields without replacing routing identity."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as identities
from team_miner.skill_miner import candidate_refinement as refinement
from team_miner.skill_miner.candidate_identity import candidate_id
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _sha
from tests.miner.skill_miner.support.candidate_identity import candidate


def assignment(tmp_path):
    old = candidate("request-review", task="核验请求并返回处理决定")
    identity = candidate_id("job", old["name"])
    authority = {**copy.deepcopy(old), "candidate_id": identity, "refinement_allowed": True,
                 "refinement_item_ids": ["method-1"], "base_metadata_sha256": refinement.metadata_sha256(old)}
    proposed = {key: copy.deepcopy(old[key]) for key in refinement.BUSINESS_FIELDS}
    proposed.update(inputs=["请求", "请求类型"], boundary="按照原文分别处理普通和特殊请求",
                    revise_candidate_id=identity, base_metadata_sha256=authority["base_metadata_sha256"],
                    reason="修正上一轮遗漏的分支输入", derived_from=["method-1"])
    item = {"item_id": "method-1", "kind": "method", "task": old["task"],
            "boundary": proposed["boundary"], "source_refs": ["alias-1"]}
    record = {"item": item, "ordinals": [0], "leaf_items": ["leaf-1"],
              "bindings": {"alias-1": [0]}, "unit_id": "unit-1"}
    value = {"schema_version": planning.SELECTION_SCHEMA_V3, "input_ids": ["unit-1"],
             "skills": [proposed], "knowledge": [], "deferred": [], "resolved": []}
    packet = {"derived_controls": {"known_candidates": [authority], "items": [item], "input_ids": ["unit-1"],
                                  "selection_schema": planning.SELECTION_SCHEMA_V3}}
    request = {"from": ["viking://frozen/packet"], "instruction": "据原文复核相同工作流"}
    child = "plan-" + "a" * 24
    _save(tmp_path / "children" / child / "packet.json", packet)
    _save(tmp_path / "children" / child / "request.json", request)
    _save(tmp_path / "children" / child / "result.txt", value)
    _save(tmp_path / "chunk-metadata/00000000.json", {"source_uri": "viking://current/original/input.md"})
    current = {"child": child, "packet_file": f"children/{child}/packet.json",
               "request_file": f"children/{child}/request.json", "packet_sha256": _sha(_json(packet)),
               "request_sha256": _sha(_json(request))}
    return {"job_id": "job", "current_round": 2}, current, value, [record], [authority]

def checked(value, records, known):
    return planning.validate_selection(value, items=[row["item"] for row in records], input_ids=["unit-1"],
                                       known_candidates={row["candidate_id"]: row for row in known},
                                       refinement_enabled=True)

def typed_assignment(tmp_path):
    job, current, value, records, known = assignment(tmp_path)
    current["context"] = {"typed_planning": "selection", "record_indexes": [0], "input_ids": ["unit-1"], "after": 1}
    _save(tmp_path / "proposals/00000000.json", records[0])
    uri = _read(tmp_path / "chunk-metadata/00000000.json")["source_uri"]
    _save(tmp_path / "source-files" / (_sha(uri) + ".json"), {"uri": uri, "ordinals": [0]})
    state = {"candidate_refinement_protocol": refinement.REFINEMENT_PROTOCOL, "selection_count": 0}
    return job, current, value, records, known, state
