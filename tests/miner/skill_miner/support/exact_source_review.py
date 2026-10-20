# ruff: noqa: F401
"""Deferred review retains exact rows, complete quote coverage and method/context roles."""

import copy

import pytest

from team_miner.skill_miner import deferred_evidence_review as review
from team_miner.skill_miner import evidence_quote_aliases as aliases
from team_miner.skill_miner import evidence_quote_units as quotes
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.skill_miner.source_unit_review import quote_parent, validate_positive_support
from team_miner.skill_miner.source_units import load_source_units
from tests.miner.skill_miner.support.deferred_evidence_review import candidate
from tests.miner.skill_miner.support.source_unit_scope import route_fixture
from tests.miner.skill_miner.support.source_units import frozen


def prepared(root):
    _, _, state = route_fixture(root, row_count=10, max_units=4)
    state.update(efficiency_protocol="te-mining-efficient.v2", source_scope_protocol="te-source-scope.v3")
    _save(root / "state.json", state)
    files = {}
    for path in (root / "chunks").glob("*.json"):
        chunk = _read(path)
        files.setdefault(chunk["source_uri"], []).append(chunk["ordinal"])
        _save(root / "chunk-metadata" / path.name, {k: v for k, v in chunk.items() if k != "content"})
    for uri, ordinals in files.items():
        _save(root / "source-files" / (_sha(uri) + ".json"), {"uri": uri, "ordinals": sorted(ordinals)})
    for index in range(10):
        record = _read(root / "catalog/method" / f"{index:08d}.json")
        ids = {identity for ref in record["item"]["source_refs"] for identity in record["source_unit_bindings"][ref]}
        rows = load_source_units(root, list(ids))
        record["item"].update(kind="method", source_files=sorted({row["source_uri"] for row in rows}))
        record.update(leaf_items=[record["item"]["item_id"]],
                      ordinals=sorted({ordinal for row in rows for ordinal in row["ordinals"]}))
        _save(root / "proposals" / f"{index:08d}.json", record)
        if index == 0:
            gap = {"reason": "当前索引尚需按原文确认完整步骤", "source_refs": record["item"]["source_files"]}
            _save(root / "method-decisions" / (record["item"]["item_id"] + ".json"),
                  {"target": "deferred", "name": "", "reason": gap["reason"]})
    plan = _read(root / "plan.json")
    plan["deferred"] = [gap]
    _save(root / "plan.json", plan)
    manifest = review._prepare_review(root, 10)
    return manifest, _read(root / "evidence-review/methods/00000000.json")
