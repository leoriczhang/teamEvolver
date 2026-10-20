# ruff: noqa: F401
"""Identity control is global and bounded; grouping never rewrites factual business fields."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import (
    GROUP_SCHEMA,
    IDENTITY_PROTOCOL,
    IdentityCatalog,
    candidate_id,
    identity_brief,
    validate_identity_groups,
)
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.ov_staged_orchestration import Owner, drive, start


def candidate(name, task="独立退款方法", uri="viking://source/doc.md"):
    return {"name": name, "task": task, "trigger": "收到请求", "inputs": ["请求"],
            "outputs": ["结论"], "boundary": "按真实规则", "rationale": "可复用流程", "source_refs": [uri]}

def row(index, *, name=None, task=None, prior=False):
    result = candidate(name or f"candidate-{index}", task or f"独立退款方法 {index}")
    return {"candidate_id": candidate_id("job", result["name"]), "candidate": result, "ordinal": index,
            "prior": prior, "selected": True, "scope": {"ordinals": [index], "method_ordinals": [index]}}

def fixture(root, rows):
    _save(root / "plan.json", {"schema_version": planning.PLAN_SCHEMA,
                              "skills": [item["candidate"] for item in rows], "deferred": []})
    db = IdentityCatalog(root / "identity" / "catalog.sqlite3")
    for item in rows:
        db.put(item)
        _save(root / "scopes" / (item["candidate"]["name"] + ".json"), item["scope"])
    db.close()
    _save(root / "identity" / "ready.json", {"count": len(rows), "protocol": IDENTITY_PROTOCOL})

def group_current(rows, child="group-1", *, after=1, ordinal=0):
    ids = [item["candidate_id"] for item in rows]
    return ({"child": child, "context": {"identity_ids": ids, "after": after,
                                         "identity_pass": 0, "anchor_ordinal": ordinal}},
            {"schema_version": GROUP_SCHEMA, "candidate_ids": list(ids),
             "groups": [{"candidate_ids": list(ids), "reason": "同一完整独立流程"}]})
